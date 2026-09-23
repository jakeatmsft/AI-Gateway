import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import function_app
import relay
from test_streaming import DELTA, END, SEARCH, frame


class Chunks(httpx.AsyncByteStream):
    def __init__(self, raw):
        self.raw = raw
        self.closed = False

    async def __aiter__(self):
        for index in range(0, len(self.raw), 7):
            yield self.raw[index:index + 7]

    async def aclose(self):
        self.closed = True


class Request:
    def __init__(self, body):
        self.body = body
        self.headers = {"x-response-request-id": "request-1"}

    async def stream(self):
        yield self.body


@pytest.mark.parametrize("used", [False, True])
async def test_relay_uses_separate_identity_tokens_and_calls_redactor_only_after_search(monkeypatch, used):
    for key, value in {
        "ORGANIZATION_BLOCKED_DOMAINS": '["youtube.com"]',
        "FOUNDRY_DEPLOYMENT": "model", "FOUNDRY_RESPONSES_URL": "https://foundry.test/responses",
        "REDACTION_URL": "https://function.test/api/redact", "REDACTION_AUDIENCE": "api://function",
    }.items():
        monkeypatch.setenv(key, value)
    terminal = END if used else {"type": "response.completed", "response": {"output": []}}
    wire = b"".join(frame(event) for event in ([SEARCH] if used else []) + [{**DELTA, "delta": "Read https://you"}, {**DELTA, "delta": "tube.com/a after"}, terminal])
    upstream = Chunks(wire)
    calls = []
    redactor = function_app.redact.build().get_user_function()

    async def transport(request):
        calls.append(request.url.path)
        if request.url.host == "foundry.test":
            assert request.headers["authorization"] == "Bearer foundry-token"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=upstream)
        assert request.headers["authorization"] == "Bearer function-token"
        result = await redactor(Request(request.content))
        return httpx.Response(result.status_code, headers=result.headers, content=result.body)

    credential = AsyncMock()
    credential.get_token.side_effect = lambda scope: SimpleNamespace(
        token="foundry-token" if scope == "https://ai.azure.com/.default" else "function-token")
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(relay, "ManagedIdentityCredential", lambda: credential)
    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda **kwargs: client)
    result = await relay.responses(Request(json.dumps({"stream": True, "tools": [{"type": "web_search"}]}).encode()))
    assert result.status_code == 200 and calls == ["/responses"]
    assert result.headers["x-lab-buffered"] == "false"
    assert result.headers["x-response-redaction-window"] == "2"
    body = b"".join([part async for part in result.body_iterator])
    if used:
        assert b"[BLOCKED LINK]" in body and b"youtube.com" not in body
        assert calls == ["/responses", "/api/redact", "/api/redact", "/api/redact"]
    else:
        assert body == wire and calls == ["/responses"]
        credential.get_token.assert_awaited_once_with("https://ai.azure.com/.default")
    assert upstream.closed and client.is_closed
    credential.close.assert_awaited_once()


async def test_relay_rejects_nonstream_request_before_inference():
    result = await relay.responses(Request(b'{"stream":false,"tools":[{"type":"web_search"}]}'))
    assert result.status_code == 400
