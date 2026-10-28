import copy
import json

import pytest

from processing import InvalidRequest, build_request, parse_urls_csv


@pytest.fixture
def envelope():
    return {
        "urls_csv": 'url\n"https://learn.microsoft.com/azure/","https://developers.openai.com/"\n',
        "original_request": {"input": "Explain APIM streaming", "model": "caller-override"},
        "initial_response": {"id": "resp_first", "status": "completed", "output": [
            {"type": "web_search_call", "id": "ws_first", "status": "completed"},
            {"type": "message", "content": [{"type": "output_text", "text": "Untrusted draft"}]},
        ]},
        "stream": True,
    }


def test_csv_normalizes_deduplicates_and_preserves_quoted_commas():
    urls, domains = parse_urls_csv('url\r\n"https://LEARN.microsoft.com/a,b"\r\nhttps://learn.microsoft.com/c\r\n')
    assert urls[0].endswith("a,b")
    assert domains == ["learn.microsoft.com"]


@pytest.mark.parametrize("csv", [None, "", "url", "http://example.com", "https://127.0.0.1",
    "https://[::1]", "https://169.254.169.254", "https://localhost", "https://host.internal",
    "https://user:secret@example.com", "https://example.com:9000", "file:///etc/passwd",
    "https://example.com\\@evil.com", '"https://example.com', "https://ex ample.com",
    "https://bad-.example.com", "https://example.com\n" * 101, "x" * 16001])
def test_csv_rejects_invalid_inputs(csv):
    with pytest.raises(InvalidRequest):
        parse_urls_csv(csv)


@pytest.mark.parametrize("stream", [False, True])
def test_build_request_uses_no_tools_and_configured_model(envelope, stream):
    envelope["stream"] = stream
    envelope["original_request"].update({
        "tools": [{"type": "web_search"}],
        "tool_choice": "required",
        "include": ["web_search_call.action.sources"],
    })
    original = copy.deepcopy(envelope)
    payload = build_request(envelope, "gpt-5.6-luna")
    assert envelope == original
    assert payload["model"] == "gpt-5.6-luna"
    assert payload["stream"] is stream and payload["store"] is False
    assert "tools" not in payload
    assert "tool_choice" not in payload
    assert "include" not in payload
    assert 'Blocked domains: ["learn.microsoft.com", "developers.openai.com"]' in payload["instructions"]
    assert json.loads(payload["input"])["previous_answer_and_search"] == envelope["initial_response"]["output"]
    assert "previous_response_id" not in payload


@pytest.mark.parametrize("mutation", [
    {"stream": "true"}, {"original_request": {}},
    {"initial_response": {"status": "incomplete", "output": []}},
    {"initial_response": {"status": "completed", "output": [{"type": "function_call", "name": "web_search"}]}},
    {"initial_response": {"status": "completed", "output": [{"type": "web_search_call", "status": "failed"}]}},
])
def test_build_request_rejects_invalid_envelopes(envelope, mutation):
    envelope.update(mutation)
    with pytest.raises(InvalidRequest):
        build_request(envelope, "gpt-5.6-luna")
