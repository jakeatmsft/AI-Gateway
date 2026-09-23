"""Edit this registry to select metrics from terminal JSON SSE events.

Extractors receive the complete terminal event and return a number or None.
Keep them fast and local: no I/O, response buffering, or metric-specific policy.
"""

TERMINAL_EVENTS = frozenset({"response.completed", "response.incomplete", "response.failed"})


def count_searches(response):
    """Read the service-reported count; missing usage is unavailable, not zero."""
    if not isinstance(response, dict):
        return None
    tool_usage = response.get("tool_usage")
    if not isinstance(tool_usage, dict):
        return None
    web_search = tool_usage.get("web_search")
    if not isinstance(web_search, dict):
        return None
    value = web_search.get("num_requests")
    return value if type(value) is int and 0 <= value <= 10**18 else None


def usage_tokens(event, name):
    value = event.get("response", {}).get("usage", {}).get(name)
    return value if type(value) is int and value >= 0 else None


METRIC_EXTRACTORS = {
    "web_search_count": lambda event: count_searches(event.get("response")),
    # "input_tokens": lambda event: usage_tokens(event, "input_tokens"),
    # "output_tokens": lambda event: usage_tokens(event, "output_tokens"),
    "total_tokens": lambda event: usage_tokens(event, "total_tokens"),
}
