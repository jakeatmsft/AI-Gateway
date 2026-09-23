import asyncio
import copy
import json

import pytest

import function_app
from test_processing import document


class Request:
    def __init__(self, body, split=None):
        self.body = body
        self.split = split

    async def stream(self):
        if self.split is None:
            yield self.body
        else:
            for index in range(0, len(self.body), self.split):
                yield self.body[index:index + self.split]


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv('ORGANIZATION_BLOCKED_DOMAINS', '["youtube.com"]')
    return function_app.redact.build().get_user_function()


def request(stream=False, response=None, **fields):
    return Request(json.dumps({'response': document() if response is None else response,
                              'blocked_domains': [], 'stream': stream, **fields}).encode())


@pytest.mark.parametrize('stream', [False, True])
async def test_regex_function_redacts_only_domains_without_a_model_call(setup, stream):
    initial = document()
    original = copy.deepcopy(initial)
    result = await setup(request(stream, initial))
    assert result.status_code == 200
    wire = b''.join([chunk async for chunk in result.body_iterator]) if stream else result.body
    assert b'[BLOCKED LINK]' in wire and b'youtube.com' not in wire
    assert b'resp_original' in wire and b'ws_original' in wire
    assert initial == original
    assert result.headers['x-lab-filter'] == 'regex-redaction'
    assert json.loads(result.headers['x-response-metrics']) == {'web_search_count': 2, 'total_tokens': 15}


async def test_union_of_server_and_caller_domains(setup):
    initial = document()
    initial['output'][1]['content'][0]['text'] += ' https://caller.example/a'
    result = await setup(request(response=initial, blocked_domains=['caller.example']))
    assert result.body.count(b'[BLOCKED LINK]') >= 2
    assert b'youtube.com' not in result.body and b'caller.example' not in result.body


async def test_split_request_chunks_are_reassembled_before_any_sse_is_emitted(setup):
    entered, release = asyncio.Event(), asyncio.Event()
    raw = request(True).body

    class GatedRequest:
        async def stream(self):
            cut = raw.index(b'youtube.com') + 4
            yield raw[:cut]
            entered.set()
            await release.wait()
            yield raw[cut:]

    task = asyncio.create_task(setup(GatedRequest()))
    await asyncio.wait_for(entered.wait(), 1)
    assert not task.done()
    release.set()
    result = await asyncio.wait_for(task, 1)
    wire = b''.join([chunk async for chunk in result.body_iterator])
    assert b'youtube.com' not in wire and b'response.completed' in wire


async def test_json_and_streaming_terminal_responses_are_identical(setup):
    from lab_helpers import iter_sse
    json_result = await setup(request(False))
    streamed = await setup(request(True))
    wire = b''.join([chunk async for chunk in streamed.body_iterator]).decode()
    # Arbitrarily splitting the wire, including in URLs and frame delimiters,
    # must not alter what the client receives after reassembly.
    fragmented = ''.join(wire[index:index + 7] for index in range(0, len(wire), 7))
    events = list(iter_sse(fragmented.splitlines()))
    terminal = [event['response'] for event in events if event['type'] == 'response.completed']
    assert terminal == [json.loads(json_result.body)]
    text = ''.join(event['delta'] for event in events if event['type'] == 'response.output_text.delta')
    assert text == terminal[0]['output'][1]['content'][0]['text']


async def test_large_answer_redacts_url_crossing_output_delta_boundary(setup):
    initial = document()
    initial['output'][1]['content'][0]['text'] = 'a' * 250 + ' https://youtube.com/watch?q=x ' + 'b' * 400
    result = await setup(request(True, initial))
    events = [json.loads(chunk.decode().split('data: ', 1)[1]) async for chunk in result.body_iterator]
    deltas = [event['delta'] for event in events if event['type'] == 'response.output_text.delta']
    assert len(deltas) > 1
    assert '[BLOCKED LINK]' in ''.join(deltas)
    assert 'youtube.com' not in ''.join(deltas)
    assert events[-1]['response']['output'][1]['content'][0]['text'] == ''.join(deltas)


@pytest.mark.parametrize('stream', [False, True])
async def test_defensive_rejection_if_gateway_calls_function_without_actual_search(setup, stream):
    initial = document()
    initial['output'] = initial['output'][1:]
    initial['tools'] = [{'type': 'web_search'}]
    result = await setup(request(stream, initial))
    assert result.status_code == 400 and b'youtube.com' not in result.body


@pytest.mark.parametrize('status', ['in_progress', 'failed', 'incomplete'])
async def test_noncompleted_search_results_fail_closed(setup, status):
    initial = document()
    initial['status'] = status
    result = await setup(request(True, initial))
    assert result.status_code == 502 and b'youtube.com' not in result.body


@pytest.mark.parametrize('fields', [{'stream': 'true'}, {'blocked_domains': 'youtube.com'},
                                    {'blocked_domains': ['https://youtube.com']}])
async def test_invalid_envelopes(setup, fields):
    result = await setup(request(**fields))
    assert result.status_code == 400


async def test_request_size_limit(setup):
    result = await setup(Request(b'x' * (function_app.MAX_BODY_BYTES + 1)))
    assert result.status_code == 413


async def test_missing_server_blocklist_is_an_error(setup, monkeypatch):
    monkeypatch.setenv('ORGANIZATION_BLOCKED_DOMAINS', '[]')
    assert (await setup(request())).status_code == 503


async def test_http_redactor_joins_a_two_delta_batch(setup):
    first = {"type": "response.output_text.delta", "item_id": "msg_pair", "output_index": 1,
             "content_index": 0, "sequence_number": 10, "delta": "Before https://you"}
    second = {**first, "sequence_number": 11, "delta": "tube.com/private?q=secret#tail after"}
    envelope = {"events": [first, second], "search_event": {"type": "response.web_search_call.searching"},
                "blocked_domains": []}
    result = await setup(Request(json.dumps(envelope).encode()))
    assert result.status_code == 200
    assert json.loads(result.body)["events"] == [
        {**first, "delta": "Before [BLOCKED LINK]"}, {**second, "delta": " after"}]
