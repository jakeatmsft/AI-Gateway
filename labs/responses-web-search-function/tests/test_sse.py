import pytest

from lab_helpers import iter_sse


def test_sse_handles_heartbeats_crlf_decoded_lines_and_multiline_data():
    lines = ': heartbeat\r\n\r\nevent: response.completed\r\ndata: {"type":\r\ndata: "response.completed"}\r\n\r\ndata: [DONE]\r\n\r\n'.splitlines()
    assert list(iter_sse(lines)) == [{"type": "response.completed"}]


def test_truncated_frame_is_not_success():
    with pytest.raises(ValueError, match="middle of a frame"):
        list(iter_sse(['data: {"type":"response.completed"}']))
