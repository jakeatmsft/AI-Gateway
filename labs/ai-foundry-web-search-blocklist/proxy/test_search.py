"""Exercise the proxy-to-redactor HTTP boundary without a model or Azure account."""

import asyncio
import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import Request

from proxy import app, redaction, search
from proxy.processing import response_events
from proxy.test_filtering import document


class Chunks(httpx.AsyncByteStream):
    def __init__(self, body, *, split=7, gate=None):
        self.body, self.split, self.gate = body, split, gate

    async def __aiter__(self):
        for index in range(0, len(self.body), self.split):
            yield self.body[index:index + self.split]
            if index == 0 and self.gate:
                self.gate[0].set()
                await self.gate[1].wait()


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('used', [False, True])
async def test_actual_search_is_the_only_trigger_and_json_sse_preserve_the_response(monkeypatch, streaming, used):
    original = document()
    original['output'][1]['content'][0]['text'] += ' https://caller.example/path?q=keep'
    if not used:
        original['output'] = original['output'][1:]
    wire = b''.join([frame async for frame in response_events(original)]) if streaming else json.dumps(original).encode()
    calls = []

    async def transport(request):
        calls.append(request.url.path)
        if request.url.host == 'foundry.test':
            assert request.headers['api-key'] == 'foundry-secret'
            return httpx.Response(200, headers={'content-type': 'text/event-stream' if streaming else 'application/json'},
                                  stream=Chunks(wire))
        assert request.url == 'https://function.test/api/redact'
        assert request.headers['x-proxy-key'] == 'proxy-secret'
        assert 'api-key' not in request.headers
        received = AsyncMock(return_value={'type': 'http.request', 'body': request.content})
        handler_request = Request({'type': 'http', 'headers': request.headers.raw}, received)
        result = await redaction.redact(handler_request)
        body = b''.join([chunk async for chunk in result.body_iterator]) if hasattr(result, 'body_iterator') else result.body
        return httpx.Response(result.status_code, headers=result.headers, content=body)

    monkeypatch.setenv('PROXY_API_KEY', 'proxy-secret')
    monkeypatch.setenv('FOUNDRY_API_KEY', 'foundry-secret')
    monkeypatch.setenv('FOUNDRY_RESPONSES_URL', 'https://foundry.test/responses')
    monkeypatch.setenv('REDACTION_URL', 'https://function.test/api/redact')
    monkeypatch.setenv('ORGANIZATION_BLOCKED_DOMAINS', '["youtube.com"]')
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    request_id = str(uuid4())
    payload = {'stream': streaming, 'tools': [{'type': 'web_search', 'filters': {'blocked_domains': ['caller.example']}}]}
    req = Request({'type': 'http', 'query_string': b'', 'headers': [
        (b'x-proxy-key', b'proxy-secret'), (b'x-response-request-id', request_id.encode()),
    ]}, AsyncMock(return_value={'type': 'http.request', 'body': json.dumps(payload).encode()}))
    with patch.object(app.httpx, 'AsyncClient', return_value=client), \
         patch.object(app, 'report_metrics', new_callable=AsyncMock) as report:
        result = await app.responses(req)
        body = b''.join([chunk async for chunk in result.body_iterator]) if streaming else result.body
    assert result.status_code == 200
    assert calls[0] == '/responses'
    assert calls.count('/api/redact') >= 1 if used else calls == ['/responses']
    assert result.headers['x-response-redaction'] == ('conditional' if streaming else 'regex' if used else 'skipped')
    if streaming:
        assert result.headers['x-response-buffered'] == 'false'
        assert result.headers['x-response-redaction-window'] == '2'
    if used:
        assert b'youtube.com' not in body and b'[BLOCKED LINK]' in body
        assert b'caller.example' not in body and b'[BLOCKED LINK]' in body
        returned = search.stream_snapshot(body) if streaming else json.loads(body)
        assert returned['id'] == original['id'] and returned['usage'] == original['usage']
    else:
        assert body == wire  # No formatting or text changes when the tool was unused.
    if streaming:
        report.assert_awaited_once_with(request_id, {'web_search_count': 2, 'total_tokens': 15})
    else:
        report.assert_not_awaited()


async def test_search_stream_waits_for_terminal_before_calling_redactor():
    doc = document()
    wire = b''.join([chunk async for chunk in response_events(doc)])
    entered, release = asyncio.Event(), asyncio.Event()
    upstream = httpx.Response(200, stream=Chunks(wire, gate=(entered, release)))
    client = AsyncMock()
    from proxy.accounting import ResponseMetrics
    task = asyncio.create_task(search.buffered_response(upstream, client, {'stream': True}, 'id', ResponseMetrics()))
    await asyncio.wait_for(entered.wait(), 1)
    assert not task.done()
    client.post.assert_not_awaited()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize('suffix', [b'', b'\r\n', b'\r'])
def test_truncated_search_stream_cannot_release_raw_links(suffix):
    wire = b'data: {"type":"response.output_text.delta","delta":"https://youtube.com/x"}' + suffix
    with pytest.raises(ValueError):
        search.stream_snapshot(wire)


@pytest.mark.parametrize('line_ending', ['\n', '\r\n', '\r'])
def test_sse_line_endings_and_search_proof(line_ending):
    doc = document()
    wire = 'data: ' + json.dumps({'type': 'response.completed', 'response': doc}) + '\n\n'
    assert search.stream_snapshot(wire.replace('\n', line_ending).encode()) == doc
    doc['output'] = doc['output'][1:]
    missing = 'data: {"type":"response.web_search_call.searching"}\n\n'
    missing += 'data: ' + json.dumps({'type': 'response.completed', 'response': doc}) + '\n\n'
    with pytest.raises(ValueError):
        search.stream_snapshot(missing.encode())


async def test_redactor_rejects_unauthenticated_requests_before_reading_body(monkeypatch):
    monkeypatch.setenv('PROXY_API_KEY', 'secret')
    receive = AsyncMock()
    result = await redaction.redact(Request({'type': 'http', 'headers': []}, receive))
    assert result.status_code == 401
    receive.assert_not_awaited()


@pytest.mark.parametrize('streaming', [False, True])
async def test_redaction_failure_never_returns_raw_foundry_text(monkeypatch, streaming):
    doc = document()
    wire = b''.join([chunk async for chunk in response_events(doc)]) if streaming else json.dumps(doc).encode()
    calls = []

    def transport(request):
        calls.append(request.url.path)
        if request.url.path == '/api/redact':
            return httpx.Response(503, json={'error': 'redactor unavailable'})
        return httpx.Response(200, headers={'content-type': 'text/event-stream' if streaming else 'application/json'},
                              stream=Chunks(wire))

    for name, value in {
        'PROXY_API_KEY': 'secret', 'FOUNDRY_API_KEY': 'key',
        'FOUNDRY_RESPONSES_URL': 'https://foundry.test/responses',
        'REDACTION_URL': 'https://function.test/api/redact',
    }.items():
        monkeypatch.setenv(name, value)
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    payload = {'stream': streaming, 'tools': [{'type': 'web_search'}]}
    req = Request({'type': 'http', 'query_string': b'', 'headers': [
        (b'x-proxy-key', b'secret'), (b'x-response-request-id', str(uuid4()).encode()),
    ]}, AsyncMock(return_value={'type': 'http.request', 'body': json.dumps(payload).encode()}))
    with patch.object(app.httpx, 'AsyncClient', return_value=client), \
         patch.object(app, 'report_metrics', new_callable=AsyncMock):
        result = await app.responses(req)
        body = b''.join([chunk async for chunk in result.body_iterator]) if streaming else result.body
    assert result.status_code == (200 if streaming else 502)
    assert b'youtube.com' not in body
    if streaming:
        assert b'event: error' in body and b'response.completed' not in body
    else:
        assert b'resp_original' not in body
    assert calls == ['/responses', '/api/redact']
