"""Authenticated regex Azure Function; no model calls or network requests."""

import hmac
import json
import logging
import os

from azurefunctions.extensions.http.fastapi import JSONResponse, Request, StreamingResponse

try:
    from .filtering import DomainRedactor, InvalidRequest, domain_list
    from .processing import MAX_BODY_BYTES, InvalidResponse, answer_text, invoked_web_search, metric_headers, response_events
    from .streaming import redact_event, redact_events
except ImportError:  # Azure Functions publishes proxy/ at the app root.
    from filtering import DomainRedactor, InvalidRequest, domain_list
    from processing import MAX_BODY_BYTES, InvalidResponse, answer_text, invoked_web_search, metric_headers, response_events
    from streaming import redact_event, redact_events

logger = logging.getLogger(__name__)


def error(status, code, message):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


async def redact(req: Request):
    key = os.getenv("PROXY_API_KEY", "")
    if not key or not hmac.compare_digest(key.encode(), req.headers.get("x-proxy-key", "").encode()):
        return error(401, "unauthorized", "Invalid proxy credential")
    raw = bytearray()
    async for chunk in req.stream():
        if len(raw) + len(chunk) > MAX_BODY_BYTES:
            return error(413, "request_too_large", "Redaction envelope exceeds 2 MB")
        raw.extend(chunk)
    try:
        required = domain_list(json.loads(os.environ["ORGANIZATION_BLOCKED_DOMAINS"]))
        if not required:
            raise ValueError("Organization blocklist is empty")
    except (KeyError, ValueError, UnicodeError):
        return error(503, "configuration_error", "Function blocklist configuration is invalid")
    try:
        envelope = json.loads(raw)
        if not isinstance(envelope, dict):
            raise InvalidRequest("The redaction envelope must be an object")
        stream = envelope.get("stream", False)
        if type(stream) is not bool:
            raise InvalidRequest("stream must be boolean")
        domains = domain_list(list(dict.fromkeys([*required, *domain_list(envelope.get("blocked_domains", []))])))
        if "events" in envelope:
            return JSONResponse({"events": redact_events(envelope, DomainRedactor(domains))},
                                headers={"Cache-Control": "no-store", "x-response-redaction": "regex"})
        if "event" in envelope:
            return JSONResponse({"event": redact_event(envelope, DomainRedactor(domains))},
                                headers={"Cache-Control": "no-store", "x-response-redaction": "regex"})
        response = envelope.get("response")
        if not invoked_web_search(response):
            raise InvalidRequest("Redaction requires an actual web_search_call in response.output")
        answer_text(response)  # Never turn failed/incomplete search results into success.
        redactor = DomainRedactor(domains)
        document = redactor.response(response)
        headers = metric_headers(response)
        headers.update({"Cache-Control": "no-store, no-transform", "x-response-buffered": "true",
                        "x-response-redaction": "regex"})
        if stream:
            return StreamingResponse(response_events(document), media_type="text/event-stream", headers=headers)
        return JSONResponse(document, headers=headers)
    except InvalidResponse:
        return error(502, "invalid_search_response", "Foundry did not produce a completed text response")
    except (ValueError, UnicodeError) as exc:
        message = str(exc) if isinstance(exc, InvalidRequest) else "Body must be valid JSON"
        return error(400, "invalid_request", message)
    except Exception as exc:
        logger.warning("Response redaction failed: %s", type(exc).__name__)
        return error(502, "redaction_failed", "Unable to redact the search response")
