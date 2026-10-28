"""Incremental SSE framing and bounded, two-delta URL redaction.

Consecutive text deltas for the same content part are filtered as a pair.
Only one complete delta waits for its partner; no text crosses pair boundaries.
"""

import json

try:
    from .filtering import InvalidRequest
    from .processing import invoked_web_search, MAX_BODY_BYTES
except ImportError:
    from filtering import InvalidRequest
    from processing import invoked_web_search, MAX_BODY_BYTES

TERMINAL_EVENTS = {"response.completed", "response.failed", "response.incomplete"}
SEARCH_EVENTS = {"response.web_search_call.in_progress", "response.web_search_call.searching",
                 "response.web_search_call.completed"}


def search_evidence(event):
    """Recognize an actual streamed invocation, never request tools or usage."""
    if not isinstance(event, dict):
        return None
    if event.get("type") in SEARCH_EVENTS:
        return {"type": event["type"], "item_id": event.get("item_id")}
    item = event.get("item")
    if (event.get("type") in {"response.output_item.added", "response.output_item.done"}
            and isinstance(item, dict) and item.get("type") == "web_search_call"):
        return {"type": event["type"], "item": {"type": "web_search_call", "id": item.get("id")}}
    if event.get("type") in TERMINAL_EVENTS and invoked_web_search(event.get("response")):
        return {"type": event["type"], "response": {"output": [{"type": "web_search_call"}]}}
    return None


def delta_key(event):
    if (event.get("type") == "response.output_text.delta"
            and isinstance(event.get("delta"), str)
            and isinstance(event.get("item_id"), str)
            and type(event.get("output_index")) is int
            and type(event.get("content_index")) is int):
        return event["item_id"], event["output_index"], event["content_index"]
    return None


def redact_events(envelope, redactor):
    if search_evidence(envelope.get("search_event")) is None:
        raise InvalidRequest("SSE redaction requires an observed web_search_call event")
    events = envelope.get("events")
    if (not isinstance(events, list) or not 1 <= len(events) <= 2
            or any(not isinstance(event, dict) or not isinstance(event.get("type"), str)
                   for event in events)):
        raise InvalidRequest("events must contain one or two Responses SSE events")
    if len(events) == 2 and (delta_key(events[0]) is None or delta_key(events[0]) != delta_key(events[1])):
        raise InvalidRequest("Paired events must be text deltas for the same content part")
    result = redactor.response(events)
    if len(events) == 2:
        text = events[0]["delta"] + events[1]["delta"]
        spans = redactor.replacements(text)
        filtered = redactor.text(text)
        # A URL spanning both deltas puts its placeholder in the first delta;
        # the second retains only text after that URL. Keep both event records.
        boundary = redactor.remap_index(len(events[0]["delta"]), spans, is_end=True)
        result[0]["delta"], result[1]["delta"] = filtered[:boundary], filtered[boundary:]
    return result


def redact_event(envelope, redactor):
    """Retain the single-event API for existing callers."""
    return redact_events({**envelope, "events": [envelope.get("event")]}, redactor)[0]


class SSEFrames:
    """Parse LF, CRLF and CR frames, including split UTF-8 and delimiters."""

    def __init__(self):
        self.pending = bytearray()
        self.scan = 0
        self.line_start = 0

    def feed(self, chunk, *, final=False):
        self.pending.extend(chunk)
        while self.scan < len(self.pending):
            char = self.pending[self.scan]
            if char not in (10, 13):
                self.scan += 1
                if self.scan > MAX_BODY_BYTES:
                    raise ValueError("SSE frame exceeds 2 MB")
                continue
            end = self.scan + 1
            if char == 13:
                if end == len(self.pending) and not final:
                    break
                if end < len(self.pending) and self.pending[end] == 10:
                    end += 1
            if end > MAX_BODY_BYTES:
                raise ValueError("SSE frame exceeds 2 MB")
            if self.scan == self.line_start:
                raw = bytes(self.pending[:end])
                del self.pending[:end]
                self.scan = self.line_start = 0
                yield raw
            else:
                self.scan = self.line_start = end
        if len(self.pending) > MAX_BODY_BYTES:
            raise ValueError("SSE frame exceeds 2 MB")
        if final and self.pending:
            raise ValueError("SSE ended inside a frame")


def frame_data(raw):
    lines = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    data = [line[5:].removeprefix(" ") for line in lines if line.startswith("data:")]
    return lines, "\n".join(data) if data else None


def replace_data(lines, event):
    result = []
    inserted = False
    for line in lines:
        if line.startswith("data:"):
            if not inserted:
                result.append("data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                inserted = True
        else:
            result.append(line)
    return "\n".join(result).encode()


class EventFilter:
    def __init__(self, redact):
        self.frames = SSEFrames()
        self.redact = redact
        self.proof = None
        self.terminal = False
        self.pending = None

    async def filtered(self, records):
        events = [event for _, event in records]
        result = await self.redact(events, self.proof)
        if not isinstance(result, list) or len(result) != len(events):
            raise ValueError("Invalid redactor response")
        keys = ("type", "sequence_number", "item_id", "output_index", "content_index")
        for original, event in zip(events, result):
            if not isinstance(event, dict) or any(event.get(key) != original.get(key) for key in keys):
                raise ValueError("Invalid redactor event order or metadata")
        return [replace_data(lines, event) for (lines, _), event in zip(records, result)]

    async def flush(self):
        if self.pending is None:
            return []
        pending, self.pending = self.pending, None
        return await self.filtered([pending])

    async def feed(self, chunk, *, final=False):
        for raw in self.frames.feed(chunk, final=final):
            lines, data = frame_data(raw)
            if data is None:
                for frame in await self.flush():
                    yield frame
                yield raw
                continue
            if data.strip() == "[DONE]":
                if not self.terminal:
                    raise ValueError("SSE ended without a terminal response")
                yield raw
                continue
            event = json.loads(data)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str) or self.terminal:
                raise ValueError("Invalid SSE event sequence")
            if event["type"] == "error":
                raise ValueError("Upstream SSE error")
            self.proof = self.proof or search_evidence(event)
            if self.proof is not None:
                record = (lines, event)
                key = delta_key(event)
                if self.pending is not None and key is not None and delta_key(self.pending[1]) == key:
                    pending, self.pending = self.pending, None
                    for frame in await self.filtered([pending, record]):
                        yield frame
                    continue
                for frame in await self.flush():
                    yield frame
                if key is not None:
                    self.pending = record
                    continue
                raw, = await self.filtered([record])
            self.terminal = event["type"] in TERMINAL_EVENTS
            yield raw
        if final and not self.terminal:
            raise ValueError("Missing terminal SSE response")


async def call_redactor(client, url, headers, domains, events, proof):
    envelope = {"events": events, "search_event": proof, "blocked_domains": domains}
    raw = json.dumps(envelope, ensure_ascii=False).encode()
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("Redaction envelope exceeds 2 MB")
    result = await client.post(url, content=raw, headers={"Content-Type": "application/json", **headers})
    result.raise_for_status()
    return result.json()["events"]
