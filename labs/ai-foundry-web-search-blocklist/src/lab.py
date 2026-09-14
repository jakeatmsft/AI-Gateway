"""Configuration, deployment preparation, and requests for the web-search lab."""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import requests
from dotenv import dotenv_values

LAB_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = LAB_DIR.parents[1]
OPENAI_USER_ROLE = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"


def load_config(env_file=None):
    """Load an explicit .env, or root then lab .env; process variables win."""
    selected = env_file or os.getenv("LAB_ENV_FILE")
    paths = [Path(selected).expanduser().resolve()] if selected else [REPO_DIR / ".env", LAB_DIR / ".env"]
    if selected and not paths[0].is_file():
        raise FileNotFoundError(f"Environment file not found: {paths[0]}")
    config = {}
    for path in paths:
        if path.is_file():
            config.update({key: value for key, value in dotenv_values(path).items() if value is not None})
    config.update(os.environ)
    return config


def azure_cli_command():
    """Resolve Azure CLI without relying on Windows batch-file execution."""
    executable = shutil.which("az")
    if executable:
        launcher = Path(executable)
        if launcher.suffix.lower() not in (".cmd", ".bat"):
            return [executable]
        # Windows MSI: CLI2/wbin/az.cmd invokes CLI2/python.exe.
        # Python environment installs can use Scripts/az.bat with the same layout.
        cli_python = launcher.parent.parent / "python.exe"
        if cli_python.is_file():
            return [str(cli_python), "-IBm", "azure.cli"]
    try:
        if importlib.util.find_spec("azure.cli") is not None:
            return [sys.executable, "-m", "azure.cli"]
    except ModuleNotFoundError:
        pass
    raise FileNotFoundError(
        "Azure CLI is unavailable to this notebook kernel. Install Azure CLI and "
        "restart VS Code/Jupyter so it inherits the updated PATH, or install "
        "azure-cli in the kernel environment with %pip install azure-cli. "
        "Then run az login before retrying."
    )


def az_json(*args):
    """Pass arguments directly to Azure CLI; never interpolate a shell command."""
    result = subprocess.run(
        [*azure_cli_command(), *args, "--only-show-errors", "--output", "json"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def service_parts(config):
    service_id = config.get("APIM_SERVICE_ID", "").rstrip("/")
    match = re.fullmatch(
        r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/Microsoft\.ApiManagement/service/([^/]+)",
        service_id, re.IGNORECASE,
    )
    if not match:
        raise ValueError("Set APIM_SERVICE_ID to the full resource ID of an existing APIM service.")
    return service_id, *match.groups()


def responses_path(backend_url):
    """The APIM rewrite path is relative to the backend's configured base URL."""
    path = urlsplit(backend_url).path.rstrip("/")
    choices = {"": "/openai/v1/responses", "/openai": "/v1/responses", "/openai/v1": "/responses"}
    if path not in choices:
        raise ValueError("Unrecognized backend URL path; set BACKEND_RESPONSES_PATH explicitly.")
    return choices[path]


def prepare_deployment(config):
    """Read existing Azure resources and build parameters; make no Azure changes."""
    service_id, subscription, resource_group, service_name = service_parts(config)
    management_url = f"https://management.azure.com{service_id}"
    backend_id = config.get("APIM_BACKEND_ID") or config.get("BACKEND_ID", "")
    if backend_id.startswith("/"):
        expected_prefix = f"{service_id}/backends/"
        if not backend_id.lower().startswith(expected_prefix.lower()):
            raise ValueError("The existing backend must belong to APIM_SERVICE_ID.")
        backend_id = backend_id[len(expected_prefix):]
    if backend_id and not re.fullmatch(r"[\w.-]+", backend_id):
        raise ValueError("BACKEND_ID must be an APIM backend name or its full resource ID.")

    service = az_json("rest", "--method", "get", "--url", f"{management_url}?api-version=2024-05-01")
    base_url = ""
    if backend_id:
        backend = az_json("rest", "--method", "get", "--url", f"{management_url}/backends/{backend_id}?api-version=2024-05-01")
        backend_url = backend["properties"].get("url", "")
        if not backend_url and not config.get("BACKEND_RESPONSES_PATH"):
            raise ValueError("Set BACKEND_RESPONSES_PATH for a backend pool.")
    else:
        endpoint = (config.get("AZURE_OPENAI_ENDPOINT") or config.get("AZURE_AI_FOUNDRY_ENDPOINT", "")).rstrip("/")
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("Set BACKEND_ID or an HTTPS AZURE_OPENAI_ENDPOINT for an existing Foundry resource.")
        path = responses_path(endpoint)
        base_url = endpoint + path.removesuffix("/responses")
        backend_url = base_url

    api_name = config.get("APIM_API_NAME", "web-search-blocklist")
    api_path = config.get("APIM_API_PATH", "web-search/openai").strip("/")
    rewrite_path = config.get("BACKEND_RESPONSES_PATH") or responses_path(backend_url)
    if not re.fullmatch(r"/[A-Za-z0-9/_.-]+", rewrite_path):
        raise ValueError("BACKEND_RESPONSES_PATH must be an absolute path without query parameters.")
    if not re.fullmatch(r"[\w-]+", api_name) or not re.fullmatch(r"[\w/-]+", api_path):
        raise ValueError("Use letters, digits, underscores and hyphens in API names and path segments.")
    parameters = {
        "apimServiceName": service_name,
        "backendId": backend_id,
        "foundryBaseUrl": base_url,
        "backendResponsesPath": rewrite_path,
        "apiName": api_name,
        "apiPath": api_path,
    }
    gateway = config.get("APIM_RESOURCE_GATEWAY_URL") or service["properties"]["gatewayUrl"]
    return {
        "subscription": subscription,
        "resource_group": resource_group,
        "parameters": parameters,
        "backend_url": backend_url,
        "responses_url": f"{gateway.rstrip('/')}/{api_path}/v1/responses",
    }


def foundry_resource_ids(config, prepared):
    """Resolve account-level RBAC scopes without granting subscription-wide access."""
    configured = config.get("AZURE_OPENAI_RESOURCE_IDS") or config.get("AZURE_OPENAI_RESOURCE_ID", "")
    if configured:
        scopes = [scope.strip().rstrip("/") for scope in configured.split(",") if scope.strip()]
    else:
        host = urlsplit(prepared["backend_url"]).hostname
        if not host:
            raise ValueError("Set AZURE_OPENAI_RESOURCE_IDS to the Foundry account IDs used by the backend pool.")
        accounts = az_json("cognitiveservices", "account", "list", "--subscription", prepared["subscription"])
        scopes = []
        for account in accounts:
            properties = account.get("properties") or {}
            hosts = {urlsplit(properties.get("endpoint") or "").hostname}
            custom_name = (properties.get("customSubDomainName") or "").lower()
            if custom_name:
                hosts.update(f"{custom_name}.{suffix}" for suffix in (
                    "openai.azure.com", "services.ai.azure.com", "cognitiveservices.azure.com",
                ))
            if host in hosts:
                scopes.append(account["id"])
        if len(scopes) != 1:
            raise ValueError(
                "Could not uniquely resolve the backend Foundry account in the APIM subscription. "
                "Set AZURE_OPENAI_RESOURCE_ID to its full account resource ID "
                "(or AZURE_OPENAI_RESOURCE_IDS for multiple accounts)."
            )
    account_id_pattern = (
        r"/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.CognitiveServices/accounts/[^/]+"
    )
    if not scopes or any(not re.fullmatch(account_id_pattern, scope, re.IGNORECASE) for scope in scopes):
        raise ValueError("RBAC scopes must be full Microsoft.CognitiveServices/accounts resource IDs.")
    return list({scope.lower(): scope for scope in scopes}.values())


def grant_backend_access(config, prepared):
    """Enable APIM's system identity if needed and grant OpenAI inference access."""
    if not prepared["parameters"]["backendId"] and config.get("AZURE_OPENAI_API_KEY"):
        return {"authentication": "api_key", "assignments": []}

    # Resolve and validate every scope before changing Azure resources.
    scopes = foundry_resource_ids(config, prepared)
    service_id, subscription, resource_group, service_name = service_parts(config)
    service = az_json(
        "rest", "--method", "get", "--url",
        f"https://management.azure.com{service_id}?api-version=2024-05-01",
    )
    identity = service.get("identity") or {}
    if not identity.get("principalId"):
        identity_type = "SystemAssigned"
        if "UserAssigned" in identity.get("type", ""):
            identity_type = "SystemAssigned, UserAssigned"
        service = az_json(
            "apim", "update", "--subscription", subscription,
            "--resource-group", resource_group, "--name", service_name,
            "--set", f"identity.type={identity_type}",
        )
        identity = service.get("identity") or {}
    principal_id = identity.get("principalId")
    if not principal_id:
        raise RuntimeError("APIM's system-assigned identity is not ready. Retry after its update completes.")

    assignments = []
    for scope in scopes:
        scope_subscription = scope.split("/")[2]
        existing = az_json(
            "role", "assignment", "list", "--subscription", scope_subscription,
            "--assignee-object-id", principal_id, "--scope", scope,
            "--role", OPENAI_USER_ROLE, "--include-inherited",
            "--fill-principal-name", "false", "--fill-role-definition-name", "false",
        )
        if not existing:
            assignment_name = str(uuid5(NAMESPACE_URL, f"{scope}/{principal_id}/{OPENAI_USER_ROLE}".lower()))
            az_json(
                "role", "assignment", "create", "--subscription", scope_subscription,
                "--name", assignment_name, "--assignee-object-id", principal_id,
                "--assignee-principal-type", "ServicePrincipal",
                "--role", OPENAI_USER_ROLE, "--scope", scope,
            )
        assignments.append({"scope": scope, "created": not bool(existing)})
    return {"authentication": "managed_identity", "principal_id": principal_id, "assignments": assignments}


def get_deployed_backend(config):
    """Read the lab API's current backend, which can differ from the local .env."""
    service_id, subscription, _, _ = service_parts(config)
    api_name = config.get("APIM_API_NAME", "web-search-blocklist")
    token = az_json("account", "get-access-token", "--resource", "https://management.azure.com/")["accessToken"]
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get(resource_id):
        response = requests.get(
            f"https://management.azure.com{resource_id}?api-version=2024-05-01",
            headers=headers, timeout=60,
        )
        response.raise_for_status()
        # APIM can return a BOM; decoding explicitly also avoids Windows CLI encoding errors.
        return json.loads(response.content.decode("utf-8-sig"))

    policy = get(f"{service_id}/apis/{api_name}/policies/policy")
    root = ET.fromstring(policy["properties"]["value"])
    targets = root.findall("./inbound//set-backend-service")
    backend_ids = {target.get("backend-id") for target in targets}
    if len(backend_ids) != 1 or not re.fullmatch(r"[\w.-]+", next(iter(backend_ids)) or ""):
        raise ValueError("This helper requires one static backend-id in the lab API's inbound policy.")
    backend_id = next(iter(backend_ids))
    backend = get(f"{service_id}/backends/{backend_id}")["properties"]
    return {"subscription": subscription, "backend_id": backend_id, "backend_url": backend.get("url") or ""}


def grant_deployed_backend_access(config):
    """Grant access to the backend APIM is calling, without changing its routing."""
    backend = get_deployed_backend(config)
    prepared = {
        "subscription": backend["subscription"], "backend_url": backend["backend_url"],
        "parameters": {"backendId": backend["backend_id"]},
    }
    return grant_backend_access(config, prepared)


def deploy(config, prepared):
    """Grant backend access, then deploy the lab API and optional backend on APIM."""
    grant_backend_access(config, prepared)
    parameters = dict(prepared["parameters"])
    if not parameters["backendId"]:
        parameters["foundryApiKey"] = config.get("AZURE_OPENAI_API_KEY", "")
    document = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {key: {"value": value} for key, value in parameters.items()},
    }
    # A private temporary directory keeps keys out of command arguments and the repo.
    with tempfile.TemporaryDirectory(prefix="web-search-deployment-") as directory:
        parameter_file = Path(directory) / "params.json"
        parameter_file.write_text(json.dumps(document, indent=2))
        return az_json(
            "deployment", "group", "create", "--subscription", prepared["subscription"],
            "--resource-group", prepared["resource_group"], "--name", parameters["apiName"],
            "--template-file", str(LAB_DIR / "main.bicep"), "--parameters", f"@{parameter_file}",
        )


def send_response(config, prepared, payload):
    key = config.get("APIM_SUBSCRIPTION_KEY")
    if not key:
        raise ValueError("Set APIM_SUBSCRIPTION_KEY to an all-APIs or lab-API subscription key.")
    response = requests.post(
        prepared["responses_url"], headers={"api-key": key}, json=payload, timeout=180,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        try:
            detail = json.dumps(response.json(), ensure_ascii=False)
        except ValueError:
            detail = response.text
        for secret in (key, config.get("AZURE_OPENAI_API_KEY")):
            if secret:
                detail = detail.replace(secret, "<redacted>")
        message = f"HTTP {response.status_code} {response.reason}: {detail[:2000]}"
        request_id = response.headers.get("apim-request-id") or response.headers.get("x-ms-request-id")
        if request_id:
            message += f"\nRequest ID: {request_id}"
        if response.status_code == 401 and "PermissionDenied" in detail:
            message += (
                "\nCheck APIM's identity permissions on the deployed backend, which may differ from .env. "
                "Use get_deployed_backend(config) to inspect it and grant_deployed_backend_access(config) "
                "to grant access to that account."
            )
        raise requests.HTTPError(message, response=response, request=response.request) from error
    return response.json()
