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
    from .accounting import SearchMeter
except ImportError:  # Azure Functions publishes proxy/ at the app root.
    from accounting import SearchMeter

logger = logging.getLogger(__name__)


async def report_usage(request_id, count):
    """Retry transient failures within 15 seconds; no persisted queue or background task."""
    headers = {
        "api-key": os.environ["USAGE_REPORT_KEY"],
        "x-web-search-request-id": request_id,
        "x-web-search-count-status": "proxy-reported" if count is not None else "unavailable",
    }
    if count is not None:
        headers["x-web-search-count"] = str(count)
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
    logger.error("Usage report delivery failed: request_id=%s count=%s", request_id, count)
    return False


class RelayResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Also finalize when ASGI send fails while the generator is suspended
            # at a yield, so a known terminal count survives a client disconnect.
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


async def relay(upstream, client, request_id):
    meter = SearchMeter()
    pending = None
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
            yield chunk
    except httpx.HTTPError:
        logger.warning("Upstream stream interrupted for %s", request_id)
        yield b'event: error\ndata: {"type":"error","message":"Foundry stream interrupted"}\n\n'
    finally:
        # Shield cleanup/reporting from downstream cancellation; never drain the
        # remaining model response after a disconnect or invent a complete count.
        with anyio.CancelScope(shield=True):
            if pending is not None:
                pending.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration, httpx.HTTPError):
                    await pending
            try:
                await upstream.aclose()
                await client.aclose()
            finally:
                # All received SSE bytes have already been yielded. Reporting can
                # delay HTTP EOF, but never delivery of model events/text.
                await report_usage(request_id, meter.finish())


async def responses(req: Request):
    key = os.getenv("PROXY_API_KEY", "")
    if not key or not hmac.compare_digest(key.encode(), req.headers.get("x-proxy-key", "").encode()):
        return JSONResponse({"error": "Invalid proxy credential"}, status_code=401)
    try:
        request_id = str(UUID(req.headers.get("x-web-search-request-id", "")))
    except ValueError:
        return JSONResponse({"error": "Missing gateway request ID"}, status_code=400)
    raw = bytearray()
    async for chunk in req.stream():
        raw.extend(chunk)
        if len(raw) > 2 * 1024 * 1024:
            return JSONResponse({"error": "Request exceeds 2 MiB"}, status_code=413)
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("stream") is not True:
            raise ValueError()
    except (ValueError, UnicodeError):
        return JSONResponse({"error": "Expected a Responses request with stream=true"}, status_code=400)

    client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10), follow_redirects=False)
    upstream = None
    try:
        headers = {"Accept": "text/event-stream", "Accept-Encoding": "identity"}
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
            with anyio.CancelScope(shield=True):
                await report_usage(request_id, None)
            return JSONResponse({"error": "Foundry rejected the request; check model, tools and proxy RBAC"}, status_code=status)
        if upstream.headers.get("content-type", "").split(";")[0].strip().lower() != "text/event-stream":
            raise ValueError("Expected SSE from Foundry")
        return RelayResponse(relay(upstream, client, request_id), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache, no-transform"})
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            if upstream is not None:
                await upstream.aclose()
            await client.aclose()
            await report_usage(request_id, None)
        raise
    except Exception as exc:
        if upstream is not None:
            await upstream.aclose()
        await client.aclose()
        # Import errors contain package names/paths, not request headers or tokens.
        detail = str(exc) if isinstance(exc, ImportError) else type(exc).__name__
        logger.warning("Proxy request failed for %s: %s", request_id, detail)
        with anyio.CancelScope(shield=True):
            await report_usage(request_id, None)
        return JSONResponse({"error": "Streaming proxy could not reach Foundry"}, status_code=502)
