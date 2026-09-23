"""Notebook helpers. Azure mutations occur only when explicitly called by a cell."""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


def az(*args):
    # Windows CreateProcess does not resolve PATHEXT like a terminal does.
    # which() finds az.cmd as well as Unix/Windows executable launchers.
    launcher = shutil.which("az")
    if launcher:
        command = [launcher]
    else:
        # The root uv environment can have Azure CLI installed as a Python
        # package even when the notebook process cannot find its launcher.
        try:
            cli_installed = importlib.util.find_spec("azure.cli") is not None
        except ModuleNotFoundError:
            cli_installed = False
        if not cli_installed:
            raise RuntimeError(
                "Azure CLI is unavailable to this notebook kernel. Install Azure CLI "
                "(on Windows: winget install --exact --id Microsoft.AzureCLI), then "
                "fully close and reopen VS Code/Jupyter so it inherits the updated PATH. "
                "Check `az --version` and run `az login` in that environment before retrying. "
                f"Current kernel: {sys.executable}"
            )
        command = [sys.executable, "-m", "azure.cli"]
    result = subprocess.run(
        [*command, *args, "--only-show-errors", "--output", "json"],
        check=False, capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError(f"Azure CLI failed: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def graph_patch(object_id, body):
    # A file preserves JSON exactly on Bash, PowerShell, and Windows cmd.
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "body.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        return az("rest", "--method", "PATCH", "--url",
                  f"https://graph.microsoft.com/v1.0/applications/{object_id}",
                  "--body", f"@{path}", "--headers", "Content-Type=application/json")


def preauthorize_notebook(object_id, notebook_client_id, scope_id):
    """Reference a scope only after Graph has persisted it.

    Scope publication and client preauthorization must be separate PATCHes.
    Graph replicas can lag even after a successful read, so retry only the
    specific missing-permission validation error, never permission denials.
    """
    last_error = None
    for attempt in range(6):
        application = az("rest", "--method", "GET", "--url",
                         f"https://graph.microsoft.com/v1.0/applications/{object_id}?$select=api")
        scopes = (application.get("api") or {}).get("oauth2PermissionScopes") or []
        if any(scope.get("id") == scope_id and scope.get("isEnabled") for scope in scopes):
            try:
                graph_patch(object_id, {"api": {"preAuthorizedApplications": [{
                    "appId": notebook_client_id, "delegatedPermissionIds": [scope_id],
                }]}})
                return
            except RuntimeError as exc:
                message = str(exc)
                if ("api.preAuthorizedApplications.delegatedPermissionIds" not in message
                        or "Permission Id that cannot be found" not in message):
                    raise
                last_error = exc
        if attempt < 5:
            time.sleep(2 ** attempt)
    raise RuntimeError(
        "The published Entra scope is not yet available for preauthorization. "
        "Rerun the registration cell; saved application and scope IDs will be reused."
    ) from last_error


def create_lab_apps(state_path, tenant_id, prefix):
    """Create three single-tenant apps, without passwords or certificates.

    State is saved after each creation so retries reuse apps and cleanup can
    remove registrations even when a later Graph operation fails.
    """
    state_path = Path(state_path)
    state = json.loads(state_path.read_text()) if state_path.exists() else {"tenant_id": tenant_id}
    if state["tenant_id"] != tenant_id:
        raise ValueError("Saved lab state belongs to a different tenant")

    def save():
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    for kind in ("gateway", "notebook", "function"):
        key = f"{kind}_app"
        if key not in state:
            app = az("ad", "app", "create", "--display-name", f"{prefix}-{kind}",
                     "--sign-in-audience", "AzureADMyOrg")
            state[key] = {"id": app["id"], "appId": app["appId"]}
            save()
        # Only create a service principal when one does not already exist.
        client_id = state[key]["appId"]
        if not az("ad", "sp", "list", "--filter", f"appId eq '{client_id}'"):
            az("ad", "sp", "create", "--id", client_id)

    state.setdefault("scope_id", str(uuid.uuid4()))
    state.setdefault("function_scope_id", str(uuid.uuid4()))
    save()
    gateway, notebook, function = (state[f"{name}_app"] for name in ("gateway", "notebook", "function"))
    graph_patch(gateway["id"], {
        "identifierUris": [f"api://{gateway['appId']}"],
        "api": {
            "requestedAccessTokenVersion": 2,
            "oauth2PermissionScopes": [{
                "id": state["scope_id"], "isEnabled": True, "type": "User", "value": "access_as_user",
                "adminConsentDisplayName": "Call the web search lab",
                "adminConsentDescription": "Call this lab as the signed-in user.",
                "userConsentDisplayName": "Call the web search lab",
                "userConsentDescription": "Call this lab as you.",
            }],
        },
    })
    # This scope lets the notebook obtain a correctly-audienced token for the
    # negative authorization test. Easy Auth still rejects its user object ID.
    graph_patch(function["id"], {"identifierUris": [f"api://{function['appId']}"],
        "api": {
            "requestedAccessTokenVersion": 2,
            "oauth2PermissionScopes": [{
                "id": state["function_scope_id"], "isEnabled": True, "type": "User", "value": "access_as_user",
                "adminConsentDisplayName": "Test Function access denial",
                "adminConsentDescription": "Obtain a token to verify that this lab Function rejects direct user access.",
                "userConsentDisplayName": "Test Function access denial",
                "userConsentDescription": "Obtain a token to test that direct user access is denied.",
            }],
        }})
    # Graph validates references against already-persisted permission sets, not
    # against scopes supplied alongside preauthorization in the same PATCH.
    preauthorize_notebook(gateway["id"], notebook["appId"], state["scope_id"])
    preauthorize_notebook(function["id"], notebook["appId"], state["function_scope_id"])
    graph_patch(notebook["id"], {
        "isFallbackPublicClient": True,
        "publicClient": {"redirectUris": ["http://localhost"]},
        "requiredResourceAccess": [
            {"resourceAppId": gateway["appId"], "resourceAccess": [{"id": state["scope_id"], "type": "Scope"}]},
            {"resourceAppId": function["appId"], "resourceAccess": [{"id": state["function_scope_id"], "type": "Scope"}]},
        ],
    })
    return state


def iter_sse(lines):
    """Parse SSE data frames, ignoring heartbeats and joining multiline data."""
    data = []
    for line in lines:
        if line == "":
            if data:
                raw = "\n".join(data)
                data = []
                if raw != "[DONE]":
                    yield json.loads(raw)
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        raise ValueError("SSE ended in the middle of a frame")


def citations(response):
    return [annotation
            for item in response.get("output", []) if item.get("type") == "message"
            for content in item.get("content", []) if content.get("type") == "output_text"
            for annotation in content.get("annotations", [])
            if annotation.get("type") == "url_citation"]


def output_text(response):
    return "\n".join(content["text"]
                     for item in response.get("output", []) if item.get("type") == "message"
                     for content in item.get("content", []) if content.get("type") == "output_text")
