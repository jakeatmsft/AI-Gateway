"""Bounded, incremental SSE accounting. No response bytes are rewritten."""

import json


def count_searches(response):
    if not isinstance(response, dict) or response.get("status") not in ("completed", "incomplete", "failed"):
        return None
    output = response.get("output")
    if not isinstance(output, list):
        return None
    total = 0
    for item in output:
        if not isinstance(item, dict):
            return None
        if item.get("type") != "web_search_call":
            continue
        if item.get("status") != "completed" or not isinstance(item.get("action"), dict):
            return None
        action = item["action"]
        if action.get("type") in ("open_page", "find_in_page"):
            continue
        if action.get("type") != "search":
            return None
        queries = action.get("queries")
        if queries is not None:
            if not isinstance(queries, list) or any(not isinstance(q, str) or not q.strip() for q in queries):
                return None
            total += len(queries)
        elif isinstance(action.get("query"), str) and action["query"].strip():
            total += 1
        else:
            return None
    return total


class SearchMeter:
    """Retain at most one SSE event; count only a terminal response snapshot."""

    def __init__(self, max_event_bytes=8 * 1024 * 1024):
        self.limit = max_event_bytes
        self.pending = bytearray()
        self.data = []
        self.data_size = 0
        self.first_line = True
        self.invalid = False
        self.terminal = False
        self.count = None

    def feed(self, chunk):
        if self.invalid:
            return
        self.pending.extend(chunk)
        offset = 0
        while offset < len(self.pending):
            lf = self.pending.find(b"\n", offset)
            cr = self.pending.find(b"\r", offset)
            ends = [position for position in (lf, cr) if position >= 0]
            if not ends:
                break
            end = min(ends)
            if self.pending[end] == 13 and end + 1 == len(self.pending):
                break  # CRLF may straddle chunks.
            line = bytes(self.pending[offset:end])
            offset = end + (2 if self.pending[end:end + 2] == b"\r\n" else 1)
            self._line(line)
            if self.invalid:
                break
        del self.pending[:offset]
        if len(self.pending) + self.data_size > self.limit:
            self.invalid = True
        if self.invalid:
            self.pending.clear()
            self.data.clear()

    def _line(self, line):
        if self.first_line:
            line = line.removeprefix(b"\xef\xbb\xbf")
            self.first_line = False
        if not line:
            if self.data:
                payload = b"\n".join(self.data)
                self.data.clear()
                self.data_size = 0
                if payload.strip() == b"[DONE]":
                    return
                try:
                    event = json.loads(payload)
                    if event.get("type") in ("response.completed", "response.incomplete", "response.failed"):
                        self.terminal = True
                        self.count = count_searches(event.get("response"))
                except (ValueError, AttributeError):
                    self.invalid = True
        elif line.startswith(b"data:"):
            value = line[5:].removeprefix(b" ")
            self.data.append(value)
            self.data_size += len(value) + 1
            if self.data_size > self.limit:
                self.invalid = True

    def finish(self):
        # A final bare CR is a line ending; unterminated data is not an event.
        if self.pending.endswith(b"\r"):
            self.feed(b"\n")
        if self.pending or self.data or self.invalid or not self.terminal:
            return None
        return self.count
