"""Live Foundry SSE relay; invoke the separate regex route only after search."""

import asyncio
import json
import logging
import os
from contextlib import suppress

import anyio
import httpx
from azure.identity.aio import ManagedIdentityCredential
from azurefunctions.extensions.http.fastapi import JSONResponse, StreamingResponse

from filtering import domain_list
from processing import metric_headers
from streaming import EventFilter, call_redactor

logger = logging.getLogger(__name__)


class RelayResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


async def relay(upstream, client, credential, domains, request_id):
    pending = None

    async def redact(events, proof):
        token = await credential.get_token(os.environ["REDACTION_AUDIENCE"] + "/.default")
        result = await call_redactor(client, os.environ["REDACTION_URL"],
                                     {"Authorization": "Bearer " + token.token,
                                      "x-response-request-id": request_id}, domains, events, proof)
        event = events[-1]
        if event["type"] in {"response.completed", "response.failed", "response.incomplete"}:
            logger.info("SSE metrics request_id=%s response_id=%s metrics=%s", request_id,
                        event["response"].get("id"), metric_headers(event["response"])["x-response-metrics"])
        return result

    filtering = EventFilter(redact)
    try:
        chunks = upstream.aiter_bytes().__aiter__()
        while True:
            pending = asyncio.create_task(anext(chunks))
            while not pending.done():
                done, _ = await asyncio.wait({pending}, timeout=15)
                if not done:
                    yield b": keep-alive\n\n"
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                break
            pending = None
            async for frame in filtering.feed(chunk):
                yield frame
        async for frame in filtering.feed(b"", final=True):
            yield frame
    except Exception as exc:
        logger.warning("SSE relay failed request_id=%s error=%s", request_id, type(exc).__name__)
        yield b'event: error\ndata: {"type":"error","message":"Response stream could not be filtered safely"}\n\n'
    finally:
        with anyio.CancelScope(shield=True):
            if pending is not None:
                pending.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration, httpx.HTTPError):
                    await pending
            await upstream.aclose()
            await client.aclose()
            await credential.close()


async def responses(req):
    raw = bytearray()
    async for chunk in req.stream():
        raw.extend(chunk)
        if len(raw) > 65536:
            return JSONResponse({"error": "Request exceeds 64 KiB"}, status_code=413)
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("stream") is not True:
            raise ValueError()
        tools = payload.get("tools")
        if not isinstance(tools, list) or not tools:
            raise ValueError()
        domains = []
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") != "web_search":
                raise ValueError()
            domains.extend(domain_list((tool.get("filters") or {}).get("blocked_domains") or []))
        domains = domain_list(list(dict.fromkeys(domains)))
        payload["model"] = os.environ["FOUNDRY_DEPLOYMENT"]
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"error": "Expected a streaming web_search Responses request"}, status_code=400)

    client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10), follow_redirects=False)
    credential = ManagedIdentityCredential()
    upstream = None
    try:
        token = await credential.get_token("https://ai.azure.com/.default")
        request = client.build_request("POST", os.environ["FOUNDRY_RESPONSES_URL"], json=payload,
                                       headers={"Authorization": "Bearer " + token.token,
                                                "Accept": "text/event-stream", "Accept-Encoding": "identity"})
        upstream = await client.send(request, stream=True)
        if upstream.status_code >= 400:
            await upstream.aclose()
            await client.aclose()
            await credential.close()
            return JSONResponse({"error": "Foundry rejected the streaming request"}, status_code=upstream.status_code)
        if upstream.headers.get("content-type", "").split(";")[0].strip().lower() != "text/event-stream":
            raise ValueError("Unexpected response type")
        return RelayResponse(relay(upstream, client, credential, domains, req.headers.get("x-response-request-id", "")),
                             media_type="text/event-stream", headers={
                                 "Cache-Control": "no-cache, no-transform", "x-lab-buffered": "false",
                                 "x-response-redaction-window": "2",
                                 "x-lab-filter": "conditional-regex", "x-response-metrics-status": "streamed",
                             })
    except BaseException as exc:
        with anyio.CancelScope(shield=True):
            if upstream is not None:
                await upstream.aclose()
            await client.aclose()
            await credential.close()
        if not isinstance(exc, Exception):
            raise
        logger.warning("SSE upstream connection failed: %s", type(exc).__name__)
        return JSONResponse({"error": "Unable to connect to Foundry"}, status_code=502)
