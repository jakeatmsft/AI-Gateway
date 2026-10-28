"""Bounded, incremental SSE accounting. No response bytes are rewritten."""

import json
import math
import re

try:
    from .metrics import METRIC_EXTRACTORS, TERMINAL_EVENTS
except ImportError:  # Function deployment places these files at its root.
    from metrics import METRIC_EXTRACTORS, TERMINAL_EVENTS


def valid_value(value):
    return type(value) in (int, float) and abs(value) <= 10**18 and math.isfinite(value)


def serialize_metrics(metrics):
    """Match the numeric map accepted by usage-policy.xml; never emit NaN/Infinity."""
    if not isinstance(metrics, dict) or not 1 <= len(metrics) <= 32:
        raise ValueError("A report requires 1–32 metrics.")
    for name, value in metrics.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            raise ValueError("Metric names must match [a-z][a-z0-9_]{0,63}.")
        if value is not None and not valid_value(value):
            raise ValueError("Metric values must be finite numbers within ±1e18, or null.")
    encoded = json.dumps(metrics, separators=(",", ":"), allow_nan=False)
    if len(encoded) > 4096:
        raise ValueError("Metrics header exceeds 4096 bytes.")
    return encoded


class ResponseMetrics:
    """Retain at most one SSE event and extract a named set of terminal metrics."""

    def __init__(self, extractors=None, terminal_events=None, max_event_bytes=8 * 1024 * 1024):
        self.extractors = dict(METRIC_EXTRACTORS if extractors is None else extractors)
        self.terminal_events = TERMINAL_EVENTS if terminal_events is None else frozenset(terminal_events)
        serialize_metrics(self.unavailable())  # Validate the registry before processing events.
        if not all(callable(extract) for extract in self.extractors.values()):
            raise ValueError("Metric extractors must be callable.")
        self.limit = max_event_bytes
        self.pending = bytearray()
        self.data = []
        self.data_size = 0
        self.first_line = True
        self.invalid = False
        self.terminal = False
        self.values = self.unavailable()

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
                    if event.get("type") in self.terminal_events:
                        self.terminal = True
                        self.values = self.extract(event)
                except (ValueError, AttributeError, TypeError, RecursionError):
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
            return self.unavailable()
        return dict(self.values)

    def unavailable(self):
        return dict.fromkeys(self.extractors)

    def extract(self, event):
        """A failed extractor invalidates only its own metric, never the stream."""
        values = self.unavailable()
        for name, extract in self.extractors.items():
            try:
                value = extract(event)
                if valid_value(value):
                    values[name] = value
            except Exception:
                pass  # Do not log event data or potentially sensitive exception details.
        return values
