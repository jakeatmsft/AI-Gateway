"""Buffer search-capable responses and call the separate redaction Function only after search."""

import json
import os

from fastapi.responses import Response, StreamingResponse

try:
    from .filtering import domain_list
    from .processing import invoked_web_search, MAX_BODY_BYTES
except ImportError:
    from filtering import domain_list
    from processing import invoked_web_search, MAX_BODY_BYTES

MAX_WIRE_BYTES = 16 * 1024 * 1024
SEARCH_TOOLS = frozenset({"web_search", "web_search_preview", "web_search_preview_2025_03_11"})
TERMINAL_EVENTS = frozenset({"response.completed", "response.failed", "response.incomplete"})


def inspect_search(payload):
    if any(payload.get(key) is not None for key in ("previous_response_id", "conversation", "prompt")):
        return True
    tools = payload.get("tools")
    return isinstance(tools, list) and any(
        isinstance(tool, dict) and tool.get("type") in SEARCH_TOOLS for tool in tools
    )


def blocked_domains(payload):
    domains = []
    for tool in payload.get("tools", []):
        if isinstance(tool, dict) and tool.get("type") in SEARCH_TOOLS:
            domains.extend(domain_list((tool.get("filters") or {}).get("blocked_domains") or []))
    return domain_list(list(dict.fromkeys(domains)))


def stream_snapshot(raw):
    """Require complete SSE frames and a terminal snapshot before releasing any bytes."""
    wire = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    frames = wire.split("\n\n")
    if frames.pop().strip():
        raise ValueError("Unterminated SSE frame")
    terminal = None
    saw_search = False
    for frame in frames:
        data = [line[5:].removeprefix(" ") for line in frame.split("\n") if line.startswith("data:")]
        if not data:
            continue
        payload = "\n".join(data)
        if payload.strip() == "[DONE]":
            continue
        event = json.loads(payload)
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError("Invalid SSE event")
        kind = event["type"]
        item = event.get("item")
        saw_search |= kind.startswith("response.web_search_call.") or (
            isinstance(item, dict) and item.get("type") == "web_search_call"
        )
        if kind == "error" or terminal is not None:
            raise ValueError("Invalid terminal SSE sequence")
        if kind in TERMINAL_EVENTS:
            terminal = event.get("response")
            if not isinstance(terminal, dict) or not isinstance(terminal.get("output"), list):
                raise ValueError("Invalid terminal response")
    if terminal is None or (saw_search and not invoked_web_search(terminal)):
        raise ValueError("Missing terminal search snapshot")
    return terminal


async def buffered_response(upstream, client, payload, request_id, meter):
    streaming = payload.get("stream") is True
    raw = bytearray()
    async for chunk in upstream.aiter_bytes():
        if len(raw) + len(chunk) > MAX_WIRE_BYTES:
            raise ValueError("Search response exceeds buffer limit")
        raw.extend(chunk)
        if streaming:
            meter.feed(chunk)
    document = stream_snapshot(raw) if streaming else json.loads(raw)
    if not isinstance(document, dict) or not isinstance(document.get("output"), list):
        raise ValueError("Invalid Responses document")
    headers = {"Cache-Control": "no-store, no-transform", "x-response-buffered": "true",
               "x-response-redaction": "skipped"}
    body = bytes(raw)
    media_type = "text/event-stream" if streaming else "application/json"
    if invoked_web_search(document):
        envelope = json.dumps({"response": document, "blocked_domains": blocked_domains(payload),
                               "stream": streaming}, ensure_ascii=False).encode()
        if len(envelope) > MAX_BODY_BYTES:
            raise ValueError("Redaction envelope exceeds limit")
        # No call for tool declarations, usage counters, or unused web_search tools.
        redacted = await client.post(os.environ["REDACTION_URL"], content=envelope, headers={
            "Content-Type": "application/json", "x-proxy-key": os.environ["PROXY_API_KEY"],
            "x-response-request-id": request_id,
        })
        if redacted.status_code != 200 or redacted.headers.get("content-type", "").split(";")[0] != media_type:
            raise ValueError("Redaction Function failed")
        if redacted.headers.get("x-response-redaction") != "regex":
            raise ValueError("Redaction Function did not confirm filtering")
        body = redacted.content
        headers["x-response-redaction"] = "regex"
    if streaming:
        async def chunks():
            for offset in range(0, len(body), 4096):
                yield body[offset:offset + 4096]
        return StreamingResponse(chunks(), media_type=media_type, headers=headers)
    return Response(body, media_type=media_type, headers=headers)
