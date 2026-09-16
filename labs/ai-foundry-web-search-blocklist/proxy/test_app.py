"""Run with: python -m unittest proxy.test_app -v"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import anyio
import httpx
from fastapi import Request

from proxy import app as proxy


def terminal():
    return b"data: " + json.dumps({
        "type": "response.completed",
        "response": {"status": "completed", "output": [
            {"type": "web_search_call", "status": "completed",
             "action": {"type": "search", "queries": ["one", "two"], "query": "one"}},
            {"type": "web_search_call", "status": "completed",
             "action": {"type": "search", "query": "three"}},
            {"type": "web_search_call", "status": "completed",
             "action": {"type": "open_page", "url": "https://example.com"}},
        ]},
    }).encode() + b"\n\n"


class Upstream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.aclose = AsyncMock()

    def aiter_bytes(self):
        return self.chunks


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_relays_before_generation_and_reporting_finish(self):
        generated = asyncio.Event()
        report_started = asyncio.Event()
        reported = asyncio.Event()

        async def chunks():
            yield b"data: {\"type\":\"response.created\"}\n\n"
            await generated.wait()
            yield terminal()

        async def report(*args):
            report_started.set()
            await reported.wait()

        upstream, client = Upstream(chunks()), AsyncMock()
        with patch.object(proxy, "report_usage", side_effect=report) as submit:
            stream = proxy.relay(upstream, client, "original-id")
            self.assertIn(b"response.created", await asyncio.wait_for(anext(stream), 1))
            self.assertFalse(generated.is_set())
            submit.assert_not_called()
            generated.set()
            self.assertEqual(await anext(stream), terminal())
            tail = asyncio.create_task(anext(stream, None))
            await asyncio.wait_for(report_started.wait(), 1)
            self.assertFalse(tail.done())
            reported.set()
            self.assertIsNone(await tail)
            submit.assert_awaited_once_with("original-id", 3)
        upstream.aclose.assert_awaited_once()
        client.aclose.assert_awaited_once()

    async def test_disconnect_after_terminal_keeps_count(self):
        async def chunks():
            yield terminal()
            await asyncio.Event().wait()

        upstream, client = Upstream(chunks()), AsyncMock()
        with patch.object(proxy, "report_usage", new_callable=AsyncMock) as submit:
            stream = proxy.relay(upstream, client, "original-id")
            await anext(stream)
            await stream.aclose()
            submit.assert_awaited_once_with("original-id", 3)

    async def test_cancellation_before_terminal_reports_unavailable(self):
        waiting = asyncio.Event()

        async def chunks():
            yield b": connected\n\n"
            waiting.set()
            await asyncio.Event().wait()

        async def consume():
            async for _ in proxy.relay(Upstream(chunks()), AsyncMock(), "original-id"):
                pass

        async def report(*args):
            await anyio.sleep(0)  # Verify reporting is shielded against cancellation.

        with patch.object(proxy, "report_usage", side_effect=report) as submit:
            async with anyio.create_task_group() as group:
                group.start_soon(consume)
                await waiting.wait()
                group.cancel_scope.cancel()
            submit.assert_awaited_once_with("original-id", None)

    async def test_asgi_send_disconnect_finalizes_suspended_generator(self):
        async def chunks():
            yield terminal()

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        with patch.object(proxy, "report_usage", new_callable=AsyncMock) as submit:
            response = proxy.RelayResponse(proxy.relay(Upstream(chunks()), AsyncMock(), "original-id"))
            from starlette.requests import ClientDisconnect
            with self.assertRaises(ClientDisconnect):
                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), send)
            submit.assert_awaited_once_with("original-id", 3)

    async def test_proxy_requires_credential_before_body_or_model_access(self):
        with patch.dict("os.environ", {"PROXY_API_KEY": "server-secret"}):
            for key, expected in (("", 401), ("wrong", 401), ("server-secret", 400)):
                receive = AsyncMock(return_value={"type": "http.request", "body": b'{"stream": false}'})
                request = Request({
                    "type": "http",
                    "headers": [(b"x-proxy-key", key.encode()), (b"x-web-search-request-id", str(uuid4()).encode())],
                }, receive)
                self.assertEqual((await proxy.responses(request)).status_code, expected)
                if expected == 401:
                    receive.assert_not_awaited()

    async def check_report(self, statuses, count):
        requests = []

        def respond(request):
            requests.append(request)
            status = statuses[len(requests) - 1]
            if status == "timeout":
                raise httpx.ReadTimeout("test", request=request)
            return httpx.Response(status)

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.dict("os.environ", {"USAGE_REPORT_KEY": "report-secret", "USAGE_REPORT_URL": "https://test/reports"}), \
             patch.object(proxy.httpx, "AsyncClient", return_value=client), \
             patch.object(proxy.anyio, "sleep", new_callable=AsyncMock):
            result = await proxy.report_usage("original-id", count)
        for request in requests:
            self.assertEqual(request.headers["x-web-search-request-id"], "original-id")
            self.assertEqual(request.headers["api-key"], "report-secret")
            self.assertEqual(request.headers.get("x-web-search-count"), str(count) if count is not None else None)
            self.assertEqual(request.headers["x-web-search-count-status"], "unavailable" if count is None else "proxy-reported")
        return result, len(requests)

    async def test_retries_transient_failures_using_same_report(self):
        self.assertEqual(await self.check_report(["timeout", 503, 204], 3), (True, 3))

    async def test_reports_unavailable_without_inventing_zero(self):
        self.assertEqual(await self.check_report([204], None), (True, 1))

    async def test_permanent_failure_does_not_retry(self):
        with self.assertLogs(proxy.logger, level="ERROR"):
            self.assertEqual(await self.check_report([403], 3), (False, 1))

    async def test_retry_exhaustion_does_not_break_stream(self):
        with self.assertLogs(proxy.logger, level="ERROR"):
            self.assertEqual(await self.check_report([429, 500, 503], 3), (False, 3))
