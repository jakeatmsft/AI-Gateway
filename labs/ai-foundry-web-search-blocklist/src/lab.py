"""Configuration, deployment preparation, and requests for the web-search lab."""

import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

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


def az_json(*args, redact_values=(), timeout=None):
    """Pass arguments directly to Azure CLI; never interpolate a shell command."""
    try:
        result = subprocess.run(
            [*azure_cli_command(), *args, "--only-show-errors", "--output", "json"],
            capture_output=True, text=True, check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Azure CLI request timed out after {timeout} seconds.") from None
    if result.returncode:
        detail = (result.stderr or result.stdout or "Azure CLI returned no error details.").strip()
        for secret in redact_values:
            if secret:
                detail = detail.replace(secret, "<redacted>")
        # CalledProcessError.__str__ hides stderr and displays command arguments.
        # Report the Azure error without echoing arguments that may contain keys.
        raise RuntimeError(f"Azure CLI failed (exit {result.returncode}):\n{detail}")
    return json.loads(result.stdout.lstrip("\ufeff")) if result.stdout.strip() else None


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
        "logAnalyticsWorkspaceId": config.get("APIM_LOG_ANALYTICS_WORKSPACE_ID", ""),
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
    backend_ids = {target.get("backend-id") for target in targets
                   if target.get("backend-id") != f"{api_name}-streaming-proxy"}
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


def _deployment_connection_error(error):
    detail = str(error).lower()
    return any(marker in detail for marker in (
        "connectionreseterror", "connection aborted", "connection reset by peer",
        "forcibly closed", "read timed out", "readtimeout", "connecttimeout",
        "remotedisconnected", "azure cli request timed out", "timeout reached by the command",
        "status code '502'", "status code '503'", "status code '504'",
    ))


def _function_deployment_records(scope):
    for attempt in range(3):
        try:
            return az_json("functionapp", "log", "deployment", "list", *scope, timeout=45) or []
        except RuntimeError as error:
            if attempt == 2 or not _deployment_connection_error(error):
                raise
            time.sleep(5 * (attempt + 1))


def _wait_for_function_deployment(scope, *, deployment_id=None, previous_ids=None, timeout=1200):
    """Follow one Kudu deployment; never infer success from an older publication."""
    deadline = time.monotonic() + timeout
    identify_deadline = min(deadline, time.monotonic() + 60)
    last_status = None
    while time.monotonic() < deadline:
        try:
            records = _function_deployment_records(scope)
        except RuntimeError as error:
            if not _deployment_connection_error(error):
                raise
            print("Deployment status connection interrupted; retrying the status check...", flush=True)
            time.sleep(10)
            continue
        if previous_ids is not None:
            candidates = [row for row in records if row.get("id") and row["id"] not in previous_ids
                          and not row["id"].startswith("temp")]
            if len(candidates) > 1:
                raise RuntimeError("Multiple new Function deployments found. Inspect deployment logs and resume a specific deployment ID.")
            if candidates:
                if deployment_id and candidates[0]["id"] != deployment_id:
                    raise RuntimeError("The new Function deployment changed during polling. Inspect deployment logs before continuing.")
                deployment_id = candidates[0]["id"]
        record = next((row for row in records if row.get("id") == deployment_id), None)
        if record:
            status = record.get("status")
            if status == 3:
                raise RuntimeError(f"Function deployment {deployment_id} failed. Inspect its Function deployment logs before retrying.")
            if status == 4 and record.get("complete"):
                if not record.get("active"):
                    raise RuntimeError(f"Function deployment {deployment_id} was superseded; it is not the active deployment.")
                print(f"Function deployment {deployment_id} succeeded.", flush=True)
                return record
            if status != last_status:
                print(f"Waiting for Function deployment {deployment_id} (status {status})...", flush=True)
                last_status = status
        elif time.monotonic() >= identify_deadline:
            raise RuntimeError("Could not identify the Function publication. Inspect deployment logs before uploading again.")
        time.sleep(10)
    target = deployment_id or "the new publication"
    raise RuntimeError(f"Timed out checking Function deployment {target}; its server-side build may still be running. Resume that deployment ID after checking its logs.")


def _publish_function_package(package_path, scope, *, redact_values=()):
    previous_ids = {row["id"] for row in _function_deployment_records(scope) if row.get("id")}
    try:
        az_json("functionapp", "deployment", "source", "config-zip", *scope,
                "--src", str(package_path), "--timeout", "1200", "--build-remote", "true",
                redact_values=redact_values, timeout=1260)
    except RuntimeError as error:
        if not _deployment_connection_error(error):
            raise
        print("Azure CLI lost the deployment connection; checking the accepted publication without uploading again...", flush=True)
    # Even a CLI success must correspond to a new, completed, active publication.
    return _wait_for_function_deployment(scope, previous_ids=previous_ids)


def _streaming_proxy_ready(url, key):
    """Check proxy and regex routes without calling Foundry."""
    try:
        ready = requests.post(
            url + "/api/responses",
            headers={"x-proxy-key": key, "x-response-request-id": str(uuid4())},
            json={}, timeout=10,
        )
        proxy_ready = ready.status_code == 400 and ready.json() == {
            "error": "Expected a streaming or web_search Responses request with valid blocked domains",
        }
        return proxy_ready and _redactor_ready(url, key)
    except (requests.RequestException, ValueError, IndexError, KeyError, TypeError, AttributeError):
        return False


def _redactor_ready(url, key):
    first = {"type": "response.output_text.delta", "item_id": "readiness", "output_index": 1,
             "content_index": 0, "delta": "https://you"}
    second = {**first, "delta": "tube.com/watch?v=1 https://example.com"}
    response = requests.post(
        url + "/api/redact", headers={"x-proxy-key": key}, timeout=10,
        json={"blocked_domains": ["youtube.com"], "events": [first, second],
              "search_event": {"type": "response.web_search_call.searching", "item_id": "search"}},
    )
    if response.status_code != 200 or response.headers.get("x-response-redaction") != "regex":
        return False
    return response.json().get("events") == [
        {**first, "delta": "[BLOCKED LINK]"}, {**second, "delta": " https://example.com"}]


def deploy_streaming_proxy(config, prepared, *, resume_deployment_id=None, reuse_deployment=False):
    """Provision/publish the HTTP Function and its direct APIM reporting path."""
    parameters = prepared["parameters"]
    upstream = config.get("STREAMING_PROXY_FOUNDRY_RESPONSES_URL")
    if not upstream:
        if not prepared["backend_url"]:
            raise ValueError("Set STREAMING_PROXY_FOUNDRY_RESPONSES_URL for a backend pool.")
        upstream = prepared["backend_url"].rstrip("/") + parameters["backendResponsesPath"]
    parsed = urlsplit(upstream)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("The streaming proxy upstream must be an HTTPS Responses URL without credentials or query parameters.")
    foundry_key = config.get("AZURE_OPENAI_API_KEY", "") if not parameters["backendId"] else ""
    deployment_name = parameters["apiName"] + "-proxy-function"
    proxy_key = None
    previous = None
    try:
        previous = az_json("deployment", "group", "show", "--subscription", prepared["subscription"],
                           "--resource-group", prepared["resource_group"], "--name", deployment_name)
        app_name = (previous["properties"].get("outputs") or {}).get("proxyAppName", {}).get("value")
        if not app_name:
            # Failed deployments can omit outputs even though the app exists.
            # Recover its name from the operations so retries retain its key.
            app_name = az_json("deployment", "operation", "group", "list",
                               "--subscription", prepared["subscription"], "--resource-group", prepared["resource_group"],
                               "--name", deployment_name, "--query",
                               "[?properties.targetResource.resourceType=='Microsoft.Web/sites'].properties.targetResource.resourceName | [0]")
        if app_name:
            settings = az_json("functionapp", "config", "appsettings", "list", "--subscription", prepared["subscription"],
                               "--resource-group", prepared["resource_group"], "--name", app_name)
            proxy_key = next((item["value"] for item in settings if item["name"] == "PROXY_API_KEY"), None)
    except RuntimeError as error:
        if not any(code in str(error) for code in ("DeploymentNotFound", "ResourceNotFound")):
            raise
    if reuse_deployment:
        outputs = {key: value["value"] for key, value in
                   ((previous or {}).get("properties", {}).get("outputs") or {}).items()}
        if not proxy_key or not outputs.get("proxyUrl") or not outputs.get("proxyAppName"):
            raise RuntimeError("No existing streaming proxy is available. Run deploy(config, prepared) first.")
        if not _streaming_proxy_ready(outputs["proxyUrl"], proxy_key):
            raise RuntimeError(
                "The existing Function is not ready for x-response-request-id and regex redaction. "
                "Run deploy(config, prepared) to publish the current proxy before updating APIM."
            )
        print("Reusing the ready streaming Function; updating APIM routing...", flush=True)
        return outputs, proxy_key
    proxy_key = proxy_key or secrets.token_urlsafe(32)
    proxy_parameters = {
        "apimServiceName": parameters["apimServiceName"], "apiName": parameters["apiName"],
        "apiPath": parameters["apiPath"], "foundryResponsesUrl": upstream, "foundryApiKey": foundry_key,
        "proxyApiKey": proxy_key,
    }
    if config.get("STREAMING_PROXY_LOCATION"):
        proxy_parameters["location"] = config["STREAMING_PROXY_LOCATION"]
    with tempfile.TemporaryDirectory(prefix="web-search-proxy-") as directory:
        parameter_file = Path(directory) / "params.json"
        parameter_file.write_text(json.dumps({"parameters": {k: {"value": v} for k, v in proxy_parameters.items()}}))
        print("Provisioning streaming Function and protected usage API...", flush=True)
        deployed = az_json(
            "deployment", "group", "create", "--subscription", prepared["subscription"],
            "--resource-group", prepared["resource_group"], "--name", deployment_name,
            "--template-file", str(LAB_DIR / "streaming-proxy.bicep"), "--parameters", "@" + str(parameter_file),
            redact_values=(foundry_key, proxy_key),
        )
        outputs = {key: value["value"] for key, value in deployed["properties"]["outputs"].items()}
        if not foundry_key:
            proxy_prepared = {**prepared, "backend_url": upstream}
            for scope in foundry_resource_ids(config, proxy_prepared):
                role_subscription = scope.split("/")[2]
                principal = outputs["proxyPrincipalId"]
                existing = az_json("role", "assignment", "list", "--subscription", role_subscription,
                                   "--assignee-object-id", principal, "--scope", scope, "--role", OPENAI_USER_ROLE,
                                   "--include-inherited", "--fill-principal-name", "false", "--fill-role-definition-name", "false")
                if not existing:
                    az_json("role", "assignment", "create", "--subscription", role_subscription,
                            "--name", str(uuid5(NAMESPACE_URL, f"{scope}/{principal}/{OPENAI_USER_ROLE}".lower())),
                            "--assignee-object-id", principal, "--assignee-principal-type", "ServicePrincipal",
                            "--scope", scope, "--role", OPENAI_USER_ROLE)
        scope = ("--subscription", prepared["subscription"], "--resource-group", prepared["resource_group"],
                 "--name", outputs["proxyAppName"])
        if resume_deployment_id:
            _wait_for_function_deployment(scope, deployment_id=resume_deployment_id)
        else:
            package_path = Path(directory) / "proxy.zip"
            with zipfile.ZipFile(package_path, "w", zipfile.ZIP_DEFLATED) as package:
                for filename in ("function_app.py", "host.json", "app.py", "accounting.py", "metrics.py", "requirements.txt",
                                 "filtering.py", "processing.py", "redaction.py", "search.py", "streaming.py"):
                    package.write(LAB_DIR / "proxy" / filename, filename)
            print("Publishing the streaming Function (remote Python build)...", flush=True)
            _publish_function_package(package_path, scope, redact_values=(foundry_key, proxy_key))
    # Reload before probing so an older host cannot answer readiness during a recycle.
    print("Loading the published package in a fresh Function host...", flush=True)
    for action in ("stop", "start"):
        az_json("functionapp", action, "--subscription", prepared["subscription"],
                "--resource-group", prepared["resource_group"], "--name", outputs["proxyAppName"])
    for attempt in range(30):
        if _streaming_proxy_ready(outputs["proxyUrl"], proxy_key):
            return outputs, proxy_key
        time.sleep(10)
    raise RuntimeError("Streaming app is not ready. Inspect App Service deployment/startup logs and retry.")


def deploy(config, prepared, *, resume_proxy_deployment_id=None, reuse_streaming_proxy=False):
    """Deploy APIM after publishing the proxy, or reuse its existing ready deployment."""
    if reuse_streaming_proxy and resume_proxy_deployment_id is not None:
        raise ValueError("Choose reuse_streaming_proxy or resume_proxy_deployment_id, not both.")
    streaming_enabled = config.get("ENABLE_STREAMING_PROXY", "true").lower() not in ("false", "0", "no")
    if reuse_streaming_proxy and not streaming_enabled:
        raise ValueError("reuse_streaming_proxy requires ENABLE_STREAMING_PROXY=true.")
    grant_backend_access(config, prepared)
    parameters = dict(prepared["parameters"])
    if not parameters["backendId"]:
        parameters["foundryApiKey"] = config.get("AZURE_OPENAI_API_KEY", "")
    if streaming_enabled:
        proxy_outputs, proxy_key = deploy_streaming_proxy(
            config, prepared, resume_deployment_id=resume_proxy_deployment_id,
            reuse_deployment=reuse_streaming_proxy,
        )
        parameters["streamingProxyUrl"] = proxy_outputs["proxyUrl"]
        parameters["streamingProxyKey"] = proxy_key
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
            redact_values=(config.get("AZURE_OPENAI_API_KEY"), config.get("APIM_SUBSCRIPTION_KEY"), parameters.get("streamingProxyKey")),
        )


def show_response_metrics(headers):
    """Display the same gateway metric headers for JSON and streaming responses."""
    for name in ("x-response-metrics-status", "x-response-request-id", "x-response-metrics",
                 "x-response-redaction", "x-response-buffered"):
        if name in headers:
            print(f"{name}: {headers[name]}")


def send_response(config, prepared, payload, *, return_headers=False):
    """Send a JSON request, optionally retaining headers for metric inspection."""
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
        request_id = (response.headers.get("x-response-request-id") or response.headers.get("apim-request-id")
                      or response.headers.get("x-ms-request-id"))
        if request_id:
            message += f"\nRequest ID: {request_id}"
        if response.status_code == 401 and "PermissionDenied" in detail:
            message += (
                "\nCheck APIM's identity permissions on the deployed backend, which may differ from .env. "
                "Use get_deployed_backend(config) to inspect it and grant_deployed_backend_access(config) "
                "to grant access to that account."
            )
        raise requests.HTTPError(message, response=response, request=response.request) from error
    body = response.json()
    return (body, response.headers) if return_headers else body
