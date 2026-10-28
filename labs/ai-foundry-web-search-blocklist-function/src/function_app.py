"""Live SSE relay and a separate regex route triggered by actual hosted search.

Easy Auth permits APIM and this app's managed identity. The regex handler makes
no network requests. ANONYMOUS disables keys, not platform token authentication.
"""

import json
import logging
import os

import azure.functions as func
from azurefunctions.extensions.http.fastapi import JSONResponse, Request, StreamingResponse

from filtering import DomainRedactor, InvalidRequest, domain_list
from streaming import redact_event, redact_events
from relay import responses as relay_responses
from processing import (
    MAX_BODY_BYTES, InvalidResponse, answer_text, invoked_web_search,
    metric_headers, response_events,
)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
logger = logging.getLogger(__name__)


@app.route(route="responses", methods=[func.HttpMethod.POST])
async def responses(req: Request):
    return await relay_responses(req)


def error(status, code, message):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


@app.route(route="redact", methods=[func.HttpMethod.POST])
async def redact(req: Request):
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
                                headers={"Cache-Control": "no-store", "x-lab-filter": "regex-redaction"})
        if "event" in envelope:
            return JSONResponse({"event": redact_event(envelope, DomainRedactor(domains))},
                                headers={"Cache-Control": "no-store", "x-lab-filter": "regex-redaction"})
        response = envelope.get("response")
        if not invoked_web_search(response):
            raise InvalidRequest("Redaction requires an actual web_search_call in response.output")
        answer_text(response)  # Never turn failed/incomplete search results into success.
        redactor = DomainRedactor(domains)
        document = redactor.response(response)
        headers = metric_headers(response)
        headers.update({"Cache-Control": "no-store, no-transform", "x-lab-buffered": "true",
                        "x-lab-filter": "regex-redaction"})
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
