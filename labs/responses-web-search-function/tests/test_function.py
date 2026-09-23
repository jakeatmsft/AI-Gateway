import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

import function_app


class Request:
    def __init__(self, body):
        self.body = body

    async def stream(self):
        yield self.body


def body(stream=False):
    return json.dumps({
        "original_request": {"input": "Question"},
        "urls_csv": "https://learn.microsoft.com/azure/",
        "initial_response": {"status": "completed", "output": [
            {"type": "web_search_call", "status": "completed"}]},
        "stream": stream,
    }).encode()


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("FOUNDRY_DEPLOYMENT", "gpt-5.6-luna")
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://example.openai.azure.com/openai/v1")

    class Credential:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get_token(self, scope):
            assert scope == "https://ai.azure.com/.default"
            return SimpleNamespace(token="test-entra-token")

    monkeypatch.setattr(function_app, "ManagedIdentityCredential", Credential)
    return function_app.process.build().get_user_function()


def mock_backend(monkeypatch, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(function_app.httpx, "AsyncClient", lambda **kwargs: client)
    return client


async def test_json_uses_bearer_and_preserves_response(setup, monkeypatch):
    expected = {"id": "resp_final", "status": "completed", "output": []}

    def handler(request):
        assert str(request.url) == "https://example.openai.azure.com/openai/v1/responses"
        assert request.headers["authorization"] == "Bearer test-entra-token"
        assert "api-key" not in request.headers
        assert json.loads(request.content)["model"] == "gpt-5.6-luna"
        return httpx.Response(200, json=expected)

    client = mock_backend(monkeypatch, handler)
    response = await setup(Request(body()))
    assert response.status_code == 200
    assert json.loads(response.body) == expected
    assert client.is_closed


class GatedStream(httpx.AsyncByteStream):
    def __init__(self):
        self.release = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
        await self.release.wait()
        yield b'data: {"type":"response.completed","response":{"id":"resp_final","status":"completed"}}\n\n'

    async def aclose(self): self.closed = True


async def test_first_sse_event_arrives_before_upstream_finishes(setup, monkeypatch):
    stream = GatedStream()
    client = mock_backend(monkeypatch, lambda _: httpx.Response(200, stream=stream,
                          headers={"Content-Type": "text/event-stream"}))
    response = await setup(Request(body(stream=True)))
    chunks = response.body_iterator
    first = await asyncio.wait_for(anext(chunks), timeout=1)
    assert b"Hello" in first
    assert not stream.release.is_set()  # Upstream completion is still blocked.
    stream.release.set()
    remaining = b"".join([chunk async for chunk in chunks])
    assert b"response.completed" in remaining
    assert stream.closed and client.is_closed


async def test_client_disconnect_closes_upstream(setup, monkeypatch):
    stream = GatedStream()
    client = mock_backend(monkeypatch, lambda _: httpx.Response(200, stream=stream,
                          headers={"Content-Type": "text/event-stream"}))
    response = await setup(Request(body(stream=True)))
    await anext(response.body_iterator)
    await response.body_iterator.aclose()
    assert stream.closed and client.is_closed


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
async def test_upstream_errors_are_not_masked_as_success(setup, monkeypatch, status):
    client = mock_backend(monkeypatch, lambda _: httpx.Response(status, json={"secret": "do not expose"}))
    response = await setup(Request(body()))
    assert response.status_code == status
    assert b"do not expose" not in response.body
    assert client.is_closed


async def test_invalid_csv_does_not_call_foundry(setup, monkeypatch):
    client = mock_backend(monkeypatch, lambda _: pytest.fail("Must reject before calling Foundry"))
    payload = json.loads(body())
    payload["urls_csv"] = "https://127.0.0.1"
    response = await setup(Request(json.dumps(payload).encode()))
    assert response.status_code == 400
    await client.aclose()


async def test_stream_error_emits_error_and_closes(setup, monkeypatch):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.created"}\n\n'
            raise httpx.ReadError("Do not expose internal exception details")

    client = mock_backend(monkeypatch, lambda _: httpx.Response(200, stream=BrokenStream(),
                          headers={"Content-Type": "text/event-stream"}))
    response = await setup(Request(body(stream=True)))
    content = b"".join([chunk async for chunk in response.body_iterator])
    assert b"upstream_stream_error" in content
    assert b"response.completed" not in content
    assert b"internal exception" not in content
    assert client.is_closed


async def test_timeout_returns_504(setup, monkeypatch):
    def handler(_): raise httpx.ReadTimeout("timeout")
    client = mock_backend(monkeypatch, handler)
    response = await setup(Request(body()))
    assert response.status_code == 504
    assert client.is_closed


async def test_oversized_body_returns_413(setup):
    response = await setup(Request(b"x" * (function_app.MAX_BODY_BYTES + 1)))
    assert response.status_code == 413


async def test_cancel_before_headers_closes_client(setup, monkeypatch):
    entered = asyncio.Event()

    async def handler(_):
        entered.set()
        await asyncio.Event().wait()

    client = mock_backend(monkeypatch, handler)
    task = asyncio.create_task(setup(Request(body())))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.is_closed
