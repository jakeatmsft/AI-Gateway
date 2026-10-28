"""Metric extension, parser, and report contract tests (no Azure calls)."""

import json
import unittest

from proxy.accounting import ResponseMetrics, serialize_metrics
from proxy.metrics import count_searches


def event(payload):
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


class MetricsTests(unittest.TestCase):
    def test_default_metrics_from_terminal_snapshot_only(self):
        meter = ResponseMetrics()
        meter.feed(event({"type": "response.output_item.done", "item": {
            "type": "web_search_call", "action": {"queries": ["duplicate"]},
        }}))
        self.assertTrue(all(v is None for v in meter.finish().values()))
        meter.feed(event({"type": "response.completed", "response": {
            "status": "completed", "output": [],
            "tool_usage": {"web_search": {"num_requests": 0}},
            "usage": {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
        }}))
        self.assertEqual(meter.finish(), {
            "web_search_count": 0, "total_tokens": 12,
        })

    def test_new_metric_and_terminal_event_need_no_relay_changes(self):
        meter = ResponseMetrics({"duration_ms": lambda e: e["summary"]["elapsed"]}, {"job.finished"})
        meter.feed(event({"type": "job.progress", "summary": {"elapsed": 1}}))
        meter.feed(event({"type": "job.finished", "summary": {"elapsed": 123.5}}))
        self.assertEqual(meter.finish(), {"duration_ms": 123.5})

    def test_extractor_failure_does_not_discard_other_metrics(self):
        meter = ResponseMetrics({
            "missing": lambda e: e["missing"], "fraction": lambda e: 0.25,
            "bad_type": lambda e: True, "not_finite": lambda e: float("nan"),
            "too_large": lambda e: 10**30,
        })
        meter.feed(event({"type": "response.completed"}))
        self.assertEqual(meter.finish(), {
            "missing": None, "fraction": 0.25, "bad_type": None, "not_finite": None, "too_large": None,
        })

    def test_missing_usage_does_not_invent_token_counts(self):
        meter = ResponseMetrics()
        meter.feed(event({"type": "response.completed", "response": {
            "status": "completed", "output": [],
            "tool_usage": {"web_search": {"num_requests": 0}},
        }}))
        self.assertEqual(meter.finish(), {
            "web_search_count": 0, "total_tokens": None,
        })

    def test_fragmented_bom_crlf_and_multiline_event(self):
        meter = ResponseMetrics({"result": lambda e: e["value"]})
        wire = b'\xef\xbb\xbf: comment\r\nevent: response.completed\r\ndata: {"type":"response.completed",\r\ndata: "value":2}\r\n\r\n'
        for byte in wire:
            meter.feed(bytes([byte]))
        self.assertEqual(meter.finish(), {"result": 2})

    def test_invalid_event_or_unterminated_terminal_is_unavailable(self):
        for wire in (b'data: {"type":[]}\n\n', b'data: []\n\n', b'data: invalid\n\n',
                     b'data: ' + b'[' * 1100 + b']' * 1100 + b'\n\n',
                     b'data: {"type":"response.completed"}\n'):
            with self.subTest(wire=wire):
                meter = ResponseMetrics({"result": lambda e: 2})
                meter.feed(wire)
                self.assertEqual(meter.finish(), {"result": None})

    def test_event_size_limit_drops_accounting_state(self):
        meter = ResponseMetrics({"result": lambda e: 2}, max_event_bytes=64)
        meter.feed(b'data: ' + b'x' * 65)
        meter.feed(event({"type": "response.completed"}))
        self.assertEqual(meter.finish(), {"result": None})
        self.assertEqual(meter.pending, b'')
        self.assertEqual(meter.data, [])

    def test_failed_response_can_have_independently_available_usage(self):
        meter = ResponseMetrics()
        meter.feed(event({"type": "response.failed", "response": {
            "status": "failed", "output": [{"type": "web_search_call", "status": "in_progress"}],
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        }}))
        self.assertEqual(meter.finish(), {
            "web_search_count": None, "total_tokens": 12,
        })

    def test_service_count_takes_precedence_over_output_queries(self):
        self.assertEqual(count_searches({
            "tool_usage": {"web_search": {"num_requests": 6}},
            "status": "completed", "output": [
            {"type": "web_search_call", "status": "completed", "action": {
                "type": "search", "queries": ["same", "same"], "query": "same"}},
            {"type": "web_search_call", "status": "completed", "action": {"type": "search", "query": "one"}},
            {"type": "web_search_call", "status": "completed", "action": {"type": "find_in_page"}},
        ]}), 6)

    def test_service_count_does_not_require_output_items(self):
        for status in ("completed", "incomplete", "failed"):
            with self.subTest(status=status):
                meter = ResponseMetrics()
                meter.feed(event({"type": f"response.{status}", "response": {
                    "status": status, "tool_usage": {"web_search": {"num_requests": 6}},
                }}))
                self.assertEqual(meter.finish(), {"web_search_count": 6, "total_tokens": None})

    def test_missing_or_invalid_service_count_is_unavailable(self):
        invalid = [None, True, False, -1, 1.5, "6", 10**18 + 1, 10**30, [], {}]
        responses = [None, {}, {"status": "completed", "output": []}]
        responses += [{"tool_usage": value} for value in (None, [], 1, {})]
        responses += [{"tool_usage": {"web_search": value}} for value in (None, [], 1, {})]
        responses += [{"tool_usage": {"web_search": {"num_requests": value}}} for value in invalid]
        responses.append({"output": [{"type": "web_search_call", "status": "completed",
                                      "action": {"type": "search", "query": "do not infer usage"}}]})
        for response in responses:
            with self.subTest(response=response):
                self.assertIsNone(count_searches(response))
        for value in (0, 6, 10**18):
            self.assertEqual(count_searches({"tool_usage": {"web_search": {"num_requests": value}}}), value)

    def test_report_contract_rejects_unsupported_values_and_names(self):
        self.assertEqual(json.loads(serialize_metrics({"gauge": -1.5, "missing": None, "zero": 0})),
                         {"gauge": -1.5, "missing": None, "zero": 0})
        for metrics in ({}, {"invalid-name": 1}, {"UPPER": 1}, {"x": True}, {"x": "1"},
                        {"x": float("inf")}, {"x": float("nan")}, {"x": 10**18 + 1}, {"x\n": 1},
                        {f"metric_{i}": i for i in range(33)}):
            with self.subTest(metrics=metrics), self.assertRaises(ValueError):
                serialize_metrics(metrics)
