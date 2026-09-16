"""HTTP-only Azure Functions adapter; application authentication stays in app.py."""

import azure.functions as func
from azurefunctions.extensions.http.fastapi import JSONResponse, Request

from app import responses as proxy_responses

# The response route validates APIM's x-proxy-key before accepting any work.
# Anonymous host auth avoids coupling that credential to host key persistence.
app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.route(route="responses", methods=[func.HttpMethod.POST])
async def responses(req: Request):
    return await proxy_responses(req)


@app.route(route="healthz", methods=[func.HttpMethod.GET])
async def health(req: Request):
    return JSONResponse({"status": "ok", "hosting": "azure-functions"})
