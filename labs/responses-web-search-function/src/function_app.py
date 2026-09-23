"""Entra-protected HTTP Function with an unbuffered Foundry SSE relay.

Easy Auth validates bearer tokens and permits only APIM's managed identity.
ANONYMOUS disables Function *keys*; it does not disable platform authentication.
"""

import asyncio
import json
import logging
import os
from contextlib import suppress
from urllib.parse import urlsplit

import azure.functions as func
import httpx
from azure.identity.aio import ManagedIdentityCredential
from azurefunctions.extensions.http.fastapi import JSONResponse, Request, StreamingResponse

from processing import InvalidRequest, MAX_BODY_BYTES, build_request

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
logger = logging.getLogger(__name__)


def error(status: int, code: str, message: str):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def responses_url() -> str:
    endpoint = os.environ["FOUNDRY_ENDPOINT"].rstrip("/")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.port not in (None, 443)
            or not (parsed.hostname or "").endswith((".openai.azure.com", ".services.ai.azure.com"))
            or parsed.path not in ("", "/openai/v1")):
        raise RuntimeError("FOUNDRY_ENDPOINT must be an Azure public-cloud OpenAI v1 endpoint")
    return endpoint + ("/responses" if parsed.path else "/openai/v1/responses")


async def relay(upstream, client):
    """Relay original SSE bytes, preserve citations, and close on disconnect.

    Comment heartbeats cover long model-response pauses without inventing model
    deltas or completed events. A pending read survives each heartbeat timeout.
    """
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
                yield pending.result()
            except StopAsyncIteration:
                break
            pending = None
    except httpx.HTTPError:
        logger.warning("Foundry stream interrupted")
        yield b'event: error\ndata: {"type":"error","code":"upstream_stream_error","message":"Foundry stream interrupted"}\n\n'
    finally:
        if pending is not None:
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration, httpx.HTTPError):
                await pending
        await upstream.aclose()
        await client.aclose()


@app.route(route="process", methods=[func.HttpMethod.POST])
async def process(req: Request):
    raw = bytearray()
    async for chunk in req.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY_BYTES:
            return error(413, "request_too_large", "Function body exceeds 2 MB")
    try:
        payload = build_request(json.loads(raw), os.environ["FOUNDRY_DEPLOYMENT"])
    except (ValueError, UnicodeError) as exc:
        message = str(exc) if isinstance(exc, InvalidRequest) else "The body must be valid JSON"
        return error(400, "invalid_request", message)

    client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10), follow_redirects=False)
    upstream = None
    try:
        async with ManagedIdentityCredential() as credential:
            token = await credential.get_token("https://ai.azure.com/.default")
        request = client.build_request(
            "POST", responses_url(), json=payload,
            headers={"Authorization": f"Bearer {token.token}",
                     "Accept": "text/event-stream" if payload["stream"] else "application/json"},
        )
        upstream = await client.send(request, stream=True)
        if upstream.status_code >= 400:
            status = upstream.status_code
            # Avoid leaking backend details, identity information or input text.
            await upstream.aclose()
            await client.aclose()
            return error(status, "foundry_error", "Foundry rejected the Function request; check deployment and RBAC")
        if payload["stream"]:
            if not upstream.headers.get("content-type", "").startswith("text/event-stream"):
                await upstream.aclose()
                await client.aclose()
                return error(502, "invalid_upstream_stream", "Foundry did not return SSE")
            return StreamingResponse(relay(upstream, client), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache, no-transform"})
        data = json.loads(await upstream.aread())
        await upstream.aclose()
        await client.aclose()
        return JSONResponse(data)
    except asyncio.CancelledError:
        if upstream is not None:
            await upstream.aclose()
        await client.aclose()
        raise
    except Exception as exc:
        if upstream is not None:
            await upstream.aclose()
        await client.aclose()
        # Exception text can contain service URLs or request data. Log only type.
        logger.warning("Function upstream failure: %s", type(exc).__name__)
        status = 504 if isinstance(exc, httpx.TimeoutException) else 502
        return error(status, "upstream_unavailable", "Unable to complete the Foundry request")
