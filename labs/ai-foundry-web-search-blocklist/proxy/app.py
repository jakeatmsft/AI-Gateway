"""SSE relay with bounded, direct APIM usage reporting."""

import asyncio
import hmac
import json
import logging
import os
from contextlib import suppress
from uuid import UUID

import anyio
import httpx
from azure.identity.aio import ManagedIdentityCredential
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

try:
    from .accounting import ResponseMetrics, serialize_metrics
    from .search import inspect_search, blocked_domains, buffered_response
    from .streaming import EventFilter, call_redactor
except ImportError:  # Azure Functions publishes proxy/ at the app root.
    from accounting import ResponseMetrics, serialize_metrics
    from search import inspect_search, blocked_domains, buffered_response
    from streaming import EventFilter, call_redactor

logger = logging.getLogger(__name__)


async def report_metrics(request_id, metrics):
    """Retry transient failures within 15 seconds; no persisted queue or background task."""
    headers = {
        "api-key": os.environ["USAGE_REPORT_KEY"],
        "x-response-request-id": request_id,
        "x-response-metrics": serialize_metrics(metrics),
    }
    try:
        with anyio.fail_after(15):
            async with httpx.AsyncClient(timeout=3, follow_redirects=False) as client:
                for attempt in range(3):
                    try:
                        response = await client.post(os.environ["USAGE_REPORT_URL"], headers=headers)
                        if response.status_code == 204:
                            return True
                        if response.status_code not in (408, 429) and response.status_code < 500:
                            break
                    except httpx.HTTPError:
                        pass
                    if attempt < 2:
                        await anyio.sleep(0.5 * (2 ** attempt))
    except Exception:
        # Do not log HTTP exceptions: their request details can include credentials.
        pass
    logger.error("Metrics report delivery failed: request_id=%s metrics=%s", request_id, metrics)
    return False


class RelayResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Also finalize when ASGI send fails while the generator is suspended
            # at a yield, so known terminal metrics survives a client disconnect.
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


async def relay(upstream, client, request_id, domains=None):
    meter = ResponseMetrics()
    pending = None
    async def redact(events, proof):
        return await call_redactor(client, os.environ["REDACTION_URL"],
                                   {"x-proxy-key": os.environ["PROXY_API_KEY"],
                                    "x-response-request-id": request_id}, domains, events, proof)
    filtering = EventFilter(redact) if domains is not None else None
    try:
        chunks = upstream.aiter_bytes().__aiter__()
        while True:
            pending = asyncio.create_task(anext(chunks))
            while not pending.done():
                done, _ = await asyncio.wait({pending}, timeout=15)
                if not done:
                    yield b": keep-alive\n\n"
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                break
            pending = None
            meter.feed(chunk)
            if filtering is None:
                yield chunk
            else:
                async for frame in filtering.feed(chunk):
                    yield frame
        if filtering is not None:
            async for frame in filtering.feed(b"", final=True):
                yield frame
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        logger.warning("Upstream stream interrupted for %s", request_id)
        yield b'event: error\ndata: {"type":"error","message":"Response stream could not be filtered safely"}\n\n'
    finally:
        # Shield cleanup/reporting from downstream cancellation; never drain the
        # remaining model response after a disconnect or invent complete metrics.
        with anyio.CancelScope(shield=True):
            if pending is not None:
                pending.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration, httpx.HTTPError):
                    await pending
            try:
                await upstream.aclose()
                await client.aclose()
            finally:
                # Report after delivery finishes or fails, without draining upstream.
                # An interrupted stream can leave one delta undispatched.
                await report_metrics(request_id, meter.finish())


async def responses(req: Request):
    key = os.getenv("PROXY_API_KEY", "")
    if not key or not hmac.compare_digest(key.encode(), req.headers.get("x-proxy-key", "").encode()):
        return JSONResponse({"error": "Invalid proxy credential"}, status_code=401)
    try:
        request_id = str(UUID(req.headers.get("x-response-request-id", "")))
    except ValueError:
        return JSONResponse({"error": "Missing gateway request ID"}, status_code=400)
    raw = bytearray()
    async for chunk in req.stream():
        raw.extend(chunk)
        if len(raw) > 2 * 1024 * 1024:
            return JSONResponse({"error": "Request exceeds 2 MiB"}, status_code=413)
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or type(payload.get("stream", False)) is not bool:
            raise ValueError()
        inspect = inspect_search(payload)
        if payload.get("stream") is not True and not inspect:
            raise ValueError()
        if inspect:
            blocked_domains(payload)  # Validate before any Foundry request.
    except (ValueError, UnicodeError, TypeError, AttributeError):
        return JSONResponse({"error": "Expected a streaming or web_search Responses request with valid blocked domains"}, status_code=400)

    client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10), follow_redirects=False)
    upstream = None
    streaming = payload.get("stream") is True
    meter = ResponseMetrics()
    try:
        headers = {"Accept": "text/event-stream" if streaming else "application/json", "Accept-Encoding": "identity"}
        key = os.getenv("FOUNDRY_API_KEY")
        if key:
            headers["api-key"] = key
        else:
            async with ManagedIdentityCredential() as credential:
                token = await credential.get_token("https://ai.azure.com/.default")
            headers["Authorization"] = f"Bearer {token.token}"
        request = client.build_request("POST", os.environ["FOUNDRY_RESPONSES_URL"],
                                       params=dict(req.query_params), json=payload, headers=headers)
        upstream = await client.send(request, stream=True)
        if upstream.status_code >= 400:
            status = upstream.status_code
            await upstream.aclose()
            await client.aclose()
            if streaming:
                with anyio.CancelScope(shield=True):
                    await report_metrics(request_id, meter.unavailable())
            return JSONResponse({"error": "Foundry rejected the request; check model, tools and proxy RBAC"}, status_code=status)
        expected_type = "text/event-stream" if streaming else "application/json"
        if upstream.headers.get("content-type", "").split(";")[0].strip().lower() != expected_type:
            raise ValueError("Unexpected Foundry response type")
        if inspect and not streaming:
            result = await buffered_response(upstream, client, payload, request_id, meter)
            await upstream.aclose()
            await client.aclose()
            if streaming:
                with anyio.CancelScope(shield=True):
                    await report_metrics(request_id, meter.finish())
            return result
        return RelayResponse(relay(upstream, client, request_id, blocked_domains(payload) if inspect else None),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "x-response-buffered": "false",
                                      "x-response-redaction-window": "2" if inspect else "0",
                                      "x-response-redaction": "conditional" if inspect else "skipped"})
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            if upstream is not None:
                await upstream.aclose()
            await client.aclose()
            if streaming:
                await report_metrics(request_id, meter.finish())
        raise
    except Exception as exc:
        if upstream is not None:
            await upstream.aclose()
        await client.aclose()
        # Import errors contain package names/paths, not request headers or tokens.
        detail = str(exc) if isinstance(exc, ImportError) else type(exc).__name__
        logger.warning("Proxy request failed for %s: %s", request_id, detail)
        if streaming:
            with anyio.CancelScope(shield=True):
                await report_metrics(request_id, meter.finish())
        return JSONResponse({"error": "Response proxy could not complete the request safely"}, status_code=502)
