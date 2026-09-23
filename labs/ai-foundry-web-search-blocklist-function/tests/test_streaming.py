import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from filtering import DomainRedactor, InvalidRequest
from streaming import EventFilter, SSEFrames, frame_data, redact_event, redact_events, search_evidence, call_redactor


def frame(event, ending="\n"):
    return ("event: " + event["type"] + ending + "data: " + json.dumps(event, ensure_ascii=False) + ending * 2).encode()


SEARCH = {"type": "response.output_item.added", "output_index": 0,
          "item": {"type": "web_search_call", "id": "ws_1", "status": "in_progress"}}
DELTA = {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 1,
         "content_index": 0, "delta": "Read https://youtube.com/a?q=1#x  文档.\n"}
END = {"type": "response.completed", "response": {"id": "resp_1", "status": "completed",
       "output": [{"type": "web_search_call", "id": "ws_1"}], "usage": {"total_tokens": 15}}}


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("split", [1, 7, 65536])
async def test_complete_sse_events_are_filtered_even_when_transport_splits_json_utf8_and_urls(ending, split):
    callback = AsyncMock(side_effect=lambda events, proof: redact_events(
        {"events": events, "search_event": proof}, DomainRedactor(["youtube.com"])))
    filtering = EventFilter(callback)
    wire = b"".join(frame(event, ending) for event in [SEARCH, DELTA, END])
    output = []
    for start in range(0, len(wire), split):
        output.extend([chunk async for chunk in filtering.feed(wire[start:start + split])])
    output.extend([chunk async for chunk in filtering.feed(b"", final=True)])
    events = [json.loads(frame_data(chunk)[1]) for chunk in output]
    assert events[1] == {**DELTA, "delta": "Read [BLOCKED LINK]  文档.\n"}
    assert events[-1] == END
    assert callback.await_count == 3


async def test_deltas_are_delivered_without_waiting_for_the_terminal_event():
    callback = AsyncMock(side_effect=lambda events, proof: redact_events({"events": events, "search_event": proof}, DomainRedactor(["youtube.com"])))
    filtering = EventFilter(callback)
    release = asyncio.Event()

    async def upstream():
        yield frame(SEARCH)
        yield frame({**DELTA, "delta": "Read https://you"})
        yield frame({**DELTA, "delta": "tube.com/a after"})
        await release.wait()
        yield frame(END)

    async def response():
        async for chunk in upstream():
            async for result in filtering.feed(chunk):
                yield result

    output = response()
    await asyncio.wait_for(anext(output), 1)
    delta = await asyncio.wait_for(anext(output), 1)
    assert b"[BLOCKED LINK]" in delta and not release.is_set()
    second = await asyncio.wait_for(anext(output), 1)
    assert json.loads(frame_data(second)[1])["delta"] == " after"
    await output.aclose()


async def test_unused_tools_usage_and_presearch_text_do_not_trigger_redaction():
    callback = AsyncMock()
    filtering = EventFilter(callback)
    terminal = {"type": "response.completed", "response": {
        "output": [], "tools": [{"type": "web_search"}], "tool_usage": {"web_search": {"num_requests": 3}}}}
    wire = frame(DELTA) + frame(terminal)
    output = b"".join([part async for part in filtering.feed(wire)])
    assert output == wire
    callback.assert_not_awaited()
    assert [part async for part in filtering.feed(b"", final=True)] == []


def pair_callback():
    return AsyncMock(side_effect=lambda events, proof: redact_events(
        {"events": events, "search_event": proof}, DomainRedactor(["youtube.com"])))


async def test_one_delta_waits_until_its_partner_arrives_then_both_are_forwarded():
    callback = pair_callback()
    filtering = EventFilter(callback)
    assert len([part async for part in filtering.feed(frame(SEARCH))]) == 1
    first = {**DELTA, "delta": "Before https://you", "sequence_number": 3}
    second = {**DELTA, "delta": "tube.com/a?q=1#x after", "sequence_number": 4}
    assert [part async for part in filtering.feed(frame(first))] == []
    assert callback.await_count == 1
    output = [json.loads(frame_data(part)[1]) async for part in filtering.feed(frame(second))]
    assert output == [{**first, "delta": "Before [BLOCKED LINK]"}, {**second, "delta": " after"}]
    assert callback.await_args.args[0] == [first, second]


@pytest.mark.parametrize("split", range(1, len("Before https://youtube.com/a?q=1#x after")))
def test_url_is_removed_at_every_split_within_a_pair(split):
    text = "Before https://youtube.com/a?q=1#x after"
    events = [{**DELTA, "delta": text[:split]}, {**DELTA, "delta": text[split:]}]
    result = redact_events({"events": events, "search_event": SEARCH}, DomainRedactor(["youtube.com"]))
    assert "".join(event["delta"] for event in result) == "Before [BLOCKED LINK] after"
    assert events[0]["delta"] + events[1]["delta"] == text


def test_allowed_hostname_completed_by_second_delta_is_preserved_exactly():
    events = [{**DELTA, "delta": "https://youtube.com"}, {**DELTA, "delta": ".good.example/a"}]
    assert redact_events({"events": events, "search_event": SEARCH}, DomainRedactor(["youtube.com"])) == events


async def test_pair_boundary_still_limits_split_url_detection():
    filtering = EventFilter(pair_callback())
    events = [SEARCH, *[{**DELTA, "delta": text} for text in ("Before ", "https://you", "tube.com/a", " after")], END]
    output = [json.loads(frame_data(part)[1]) async for part in filtering.feed(b"".join(map(frame, events)))]
    assert "".join(event["delta"] for event in output if "delta" in event) == "Before https://youtube.com/a after"


@pytest.mark.parametrize("boundary", [END, {"type": "response.output_text.done", "text": "https://youtube.com/a"}])
async def test_nontext_event_flushes_odd_delta_in_order(boundary):
    filtering = EventFilter(pair_callback())
    events = [SEARCH, DELTA, boundary]
    output = [json.loads(frame_data(part)[1]) async for part in filtering.feed(b"".join(map(frame, events)))]
    assert [event["type"] for event in output] == [event["type"] for event in events]
    assert output[1]["delta"] == "Read [BLOCKED LINK]  文档.\n"


@pytest.mark.parametrize("change", [{"item_id": "msg_other"}, {"content_index": 1}, {"output_index": 2}])
async def test_different_items_and_content_parts_are_never_joined(change):
    callback = pair_callback()
    filtering = EventFilter(callback)
    events = [SEARCH, {**DELTA, "delta": "https://you"}, {**DELTA, **change, "delta": "tube.com/a"}, END]
    output = [json.loads(frame_data(part)[1]) async for part in filtering.feed(b"".join(map(frame, events)))]
    assert output == events
    assert all(len(call.args[0]) == 1 for call in callback.await_args_list)


async def test_comments_flush_pending_delta_and_preserve_sse_fields():
    filtering = EventFilter(pair_callback())
    first = b"id: first\nretry: 1000\n" + frame(DELTA)
    wire = frame(SEARCH) + first + b": heartbeat\n\n" + frame(END) + b"data: [DONE]\n\n"
    output = [part async for part in filtering.feed(wire)]
    assert output[1].startswith(b"id: first\nretry: 1000\n")
    assert b"[BLOCKED LINK]" in output[1]
    assert output[2] == b": heartbeat\n\n"
    assert output[-1] == b"data: [DONE]\n\n"


@pytest.mark.parametrize("ending", [b"", b'data: [DONE]\n\n', b'data: {"type":"error"}\n\n'])
async def test_interrupted_stream_does_not_release_a_pending_unfiltered_delta(ending):
    filtering = EventFilter(pair_callback())
    assert len([part async for part in filtering.feed(frame(SEARCH) + frame(DELTA))]) == 1
    with pytest.raises(ValueError):
        _ = [part async for part in filtering.feed(ending, final=True)]


@pytest.mark.parametrize("events", [[], [DELTA] * 3, [DELTA, END], [DELTA, {**DELTA, "content_index": 1}], [None]])
def test_invalid_event_batches_are_rejected(events):
    with pytest.raises(InvalidRequest):
        redact_events({"events": events, "search_event": SEARCH}, DomainRedactor(["youtube.com"]))


async def test_invalid_batch_result_never_releases_a_pending_pair():
    callback = pair_callback()
    filtering = EventFilter(callback)
    _ = [part async for part in filtering.feed(frame(SEARCH) + frame(DELTA))]
    callback.side_effect = lambda events, proof: [{**events[0], "item_id": "wrong"}, events[1]]
    with pytest.raises(ValueError, match="metadata"):
        _ = [part async for part in filtering.feed(frame(DELTA))]


async def test_two_event_envelope_obeys_the_total_request_size_limit(monkeypatch):
    import streaming as module
    monkeypatch.setattr(module, "MAX_BODY_BYTES", 300)
    client = AsyncMock()
    with pytest.raises(ValueError, match="2 MB"):
        await call_redactor(client, "https://function.test/redact", {}, [], [DELTA, DELTA], SEARCH)
    client.post.assert_not_awaited()


@pytest.mark.parametrize("proof", [{"tools": [{"type": "web_search"}]}, {}, {"type": "response.created"}])
def test_redactor_rejects_event_envelopes_without_actual_search_proof(proof):
    with pytest.raises(InvalidRequest):
        redact_event({"event": DELTA, "search_event": proof}, DomainRedactor(["youtube.com"]))


def test_search_progress_and_output_items_count_as_actual_invocation():
    assert search_evidence(SEARCH)
    assert search_evidence({"type": "response.web_search_call.searching", "item_id": "ws_1"})
    assert search_evidence(END)
    assert not search_evidence({"type": "response.output_item.added", "item": {"type": "message"}})


async def test_redactor_failure_never_returns_unfiltered_event():
    filtering = EventFilter(AsyncMock(side_effect=RuntimeError("redactor unavailable")))
    with pytest.raises(RuntimeError):
        _ = [chunk async for chunk in filtering.feed(frame(SEARCH) + frame(DELTA))]


async def test_truncated_stream_is_detected_after_previous_events_were_delivered():
    filtering = EventFilter(AsyncMock())
    assert [chunk async for chunk in filtering.feed(frame(DELTA))] == [frame(DELTA)]
    with pytest.raises(ValueError, match="terminal"):
        _ = [chunk async for chunk in filtering.feed(b"", final=True)]
    with pytest.raises(ValueError, match="inside a frame"):
        list(SSEFrames().feed(b'data: {"type":"response.completed"}', final=True))


def test_frame_limit_is_per_event_not_entire_stream(monkeypatch):
    import streaming as module
    monkeypatch.setattr(module, "MAX_BODY_BYTES", 64)
    decoder = SSEFrames()
    raw = b": comment\n\n"
    assert list(decoder.feed(raw * 100)) == [raw] * 100
    with pytest.raises(ValueError, match="exceeds"):
        list(decoder.feed(b"a" * 65))
