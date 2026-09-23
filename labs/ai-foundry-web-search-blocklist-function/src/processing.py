"""Validate response envelopes, detect hosted search, and reconstruct redacted SSE."""

import json

from metrics import count_searches

MAX_BODY_BYTES = 2_000_000


class InvalidResponse(ValueError):
    pass


def answer_text(response):
    if not isinstance(response, dict) or response.get("status") != "completed":
        raise InvalidResponse("A completed response is required")
    output = response.get("output")
    if not isinstance(output, list):
        raise InvalidResponse("Response output must be an array")
    texts = []
    for item in output:
        if not isinstance(item, dict):
            raise InvalidResponse("Invalid output item")
        if item.get("type") != "message":
            continue
        if not isinstance(item.get("id"), str) or not item["id"]:
            raise InvalidResponse("Message ID is missing")
        if not isinstance(item.get("content"), list):
            raise InvalidResponse("Invalid message content")
        for content in item["content"]:
            if not isinstance(content, dict):
                raise InvalidResponse("Invalid content block")
            key = {"output_text": "text", "refusal": "refusal"}.get(content.get("type"))
            if key is None or not isinstance(content.get(key), str):
                raise InvalidResponse("Only text and refusal content are supported")
            texts.append(content[key])
    if not texts:
        raise InvalidResponse("Response contains no answer text")
    return "\n".join(texts)


def invoked_web_search(response):
    """Tool declarations and usage counters are not proof of an invocation."""
    return isinstance(response, dict) and isinstance(response.get("output"), list) and any(
        isinstance(item, dict) and item.get("type") == "web_search_call"
        for item in response["output"]
    )


def metric_headers(response):
    usage = response.get("usage")
    tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    if type(tokens) is not int or not 0 <= tokens <= 10**18:
        tokens = None
    metrics = {"web_search_count": count_searches(response), "total_tokens": tokens}
    return {
        "x-response-metrics": json.dumps(metrics, separators=(",", ":")),
        "x-response-metrics-status": "reported" if all(v is not None for v in metrics.values())
        else "partial" if any(v is not None for v in metrics.values()) else "unavailable",
    }


async def response_events(response):
    """Reconstruct SSE from the redacted response, including every output item."""
    sequence = 0

    def event(kind, **values):
        nonlocal sequence
        data = {"type": kind, "sequence_number": sequence, **values}
        sequence += 1
        return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

    pending = {**response, "status": "in_progress", "output": [], "usage": None}
    yield event("response.created", response=pending)
    yield event("response.in_progress", response=pending)
    for output_index, item in enumerate(response["output"]):
        added = {**item}
        if "status" in added:
            added["status"] = "in_progress"
        if item.get("type") == "message":
            added["content"] = []
        yield event("response.output_item.added", output_index=output_index, item=added)
        if item.get("type") == "message":
            for content_index, part in enumerate(item["content"]):
                key = "text" if part["type"] == "output_text" else "refusal"
                kind = "output_text" if key == "text" else "refusal"
                location = {"item_id": item["id"], "output_index": output_index,
                            "content_index": content_index}
                empty_part = {**part, key: ""}
                if key == "text":
                    empty_part["annotations"] = []
                yield event("response.content_part.added", **location, part=empty_part)
                for index in range(0, len(part[key]), 256):
                    yield event(f"response.{kind}.delta", **location, delta=part[key][index:index + 256])
                if key == "text":
                    for annotation_index, annotation in enumerate(part.get("annotations", [])):
                        yield event("response.output_text.annotation.added", **location,
                                    annotation_index=annotation_index, annotation=annotation)
                yield event(f"response.{kind}.done", **location, **{key: part[key]})
                yield event("response.content_part.done", **location, part=part)
        yield event("response.output_item.done", output_index=output_index, item=item)
    yield event("response.completed", response=response)
