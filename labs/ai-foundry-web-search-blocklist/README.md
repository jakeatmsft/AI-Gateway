# Foundry web-search domain blocklist with API Management

This lab uses an APIM inbound policy to append organization domains to
`tools[].filters.blocked_domains` on Azure OpenAI Responses API requests. The
merge runs only for tools whose `type` is exactly `web_search`.

The policy adds these 11 domains:

```text
youtube.com
tiktok.com
huggingface.co
facebook.com
x.ai
spotify.com
pinterest.com
perplexity.ai
netflix.com
weebly.com
repo.maven.apache.org
```

Existing blocked domains keep their values and order. The policy appends only
missing domains, comparing names without case sensitivity. It preserves
`allowed_domains`, other filter settings, other tools, and the rest of the JSON
request. Reapplying the policy does not append another copy of the domains.

Requests without `web_search` bypass blocklist validation and body rewriting.
Missing or null `filters` / `blocked_domains` are initialized when needed. A
web-search tool with malformed filters or a blocklist that is not an array of
strings gets HTTP 400; the policy does not discard and replace malformed values.

Microsoft documents domain filtering for **`web_search` in the Responses API**.
The policy leaves `web_search_preview` and dated preview types unchanged because
they do not support this filtering contract. Use `web_search` for this sample.
This controls search sources; it does not filter arbitrary domain mentions in
model output or implement a network firewall.

## Prerequisites

- Python 3.12+, VS Code with Jupyter, and dependencies installed with `uv sync`
  at the repository root.
- Azure CLI signed in with permission to read APIM and deploy APIs in its resource
  group, plus permission to create role assignments on the Foundry account
  (for example, Owner or Role Based Access Control Administrator).
- An existing APIM service and an existing Foundry model deployment that supports
  `web_search`. Web search must be enabled in the Azure subscription.
- An APIM subscription key scoped to all APIs or to this lab's API. A key scoped
  to another lab API or a product that does not contain this API will not work.

On Windows, the helper resolves `az.cmd` from `PATH` and invokes Azure CLI's
bundled Python directly. It can also use `azure-cli` installed in the notebook's
Python environment. If neither is available, install Azure CLI and restart
VS Code/Jupyter to refresh its `PATH`, or run `%pip install azure-cli` in the
notebook environment. Authenticate with `az login` before preparing deployment.

## Run the sample

1. Copy [.env.example](.env.example) to `.env` in this directory and supply your
   existing resources. Alternatively select an existing file in the notebook,
   or set `LAB_ENV_FILE` to its path. For example:

   ```python
   config = load_config(env_file="../secure-responses-api/.env")
   ```

   Without an explicit path, the loader reads the repository-root `.env`, then
   this lab's `.env`. Process environment variables take precedence. An explicit
   file replaces that file search. Other labs' environment files are not loaded
   automatically.

2. Set `APIM_SERVICE_ID`, `APIM_SUBSCRIPTION_KEY`, and
   `AZURE_OPENAI_DEPLOYMENT`. To reuse the existing APIM Foundry backend, set
   `BACKEND_ID` (or `APIM_BACKEND_ID`). Its existing URL is reused, and this lab's
   API uses APIM's system-assigned managed identity to obtain a Microsoft Entra
   token for `https://ai.azure.com`, as documented for the Responses API. The policy sets the backend
   `Authorization: Bearer ...` header. A reused backend must not override that
   header with different credentials.

3. If no backend ID is configured, set `AZURE_OPENAI_ENDPOINT` (or
   `AZURE_AI_FOUNDRY_ENDPOINT`) to an existing Foundry resource endpoint. This
   creates one backend on the existing APIM service. An optional
   `AZURE_OPENAI_API_KEY` supplies backend authentication; otherwise the sample
   enables APIM's system-assigned identity if needed and grants it **Cognitive
   Services OpenAI User** on the Foundry account. Existing user-assigned
   identities and role assignments are preserved. The sample does not create
   Foundry accounts or model deployments.

4. Open [ai-foundry-web-search-blocklist.ipynb](ai-foundry-web-search-blocklist.ipynb)
   and run its cells in order. The preparation cell reads the backend URL and
   resolves the Responses path. The deployment cell grants backend access and
   creates a dedicated API,
   defaulting to `POST https://<apim>/web-search/openai/v1/responses`. Choose an
   unused `APIM_API_NAME` / `APIM_API_PATH` for the first run; subsequent runs
   update that same lab API.

The deployment helper discovers the Foundry account from the backend hostname
in the APIM subscription. Set `AZURE_OPENAI_RESOURCE_ID` explicitly if the
account is in another subscription or uses a custom hostname. For a backend
pool, set `AZURE_OPENAI_RESOURCE_IDS` to a comma-separated list of its Foundry
account resource IDs. Grants are scoped to those accounts. Existing grants,
including inherited grants of the same role, are reused.

To grant access to the backend of an already deployed API, run:

```python
from src.lab import get_deployed_backend, grant_deployed_backend_access
print(get_deployed_backend(config))
backend_access = grant_deployed_backend_access(config)
```

This reads the deployed policy and backend URL. The endpoint in `.env` controls
the next deployment; editing `.env` does not change an API that is already
running. `deploy(config, prepared)` grants access to the configured destination
before applying it. The standalone helper above grants access to the current
destination and leaves routing unchanged. If `AZURE_OPENAI_RESOURCE_ID(S)` is set
explicitly, ensure it identifies the accounts used by that destination.

The role change can take a few minutes to propagate. The CLI account must have
the role-assignment permissions described above; assigning this role does not
require Microsoft Graph access. Running `main.bicep` directly does not run this
Python helper, so grant the identity access separately in that case.

The existing APIM backend takes priority over a configured Foundry endpoint or
key. Preparation fails if the configured backend no longer exists instead of
silently provisioning a replacement.

Backend routing is resolved as follows. Set `BACKEND_RESPONSES_PATH` explicitly
for a custom backend path or a backend pool, whose members should use the same
base path.

| Backend URL ends in | Relative Responses path |
| --- | --- |
| Resource hostname only | `/openai/v1/responses` |
| `/openai` | `/v1/responses` |
| `/openai/v1` | `/responses` |

## Before and after

Client request:

```json
{
  "model": "gpt-4.1",
  "input": "Find recent Azure API Management announcements.",
  "tools": [{
    "type": "web_search",
    "filters": {
      "allowed_domains": ["learn.microsoft.com", "azure.microsoft.com"],
      "blocked_domains": ["example.com", "youtube.com"]
    }
  }]
}
```

The backend receives the same request with this blocklist. `example.com` is
preserved, `youtube.com` occurs once, and `allowed_domains` remains intact:

```json
"blocked_domains": [
  "example.com", "youtube.com", "tiktok.com", "huggingface.co", "facebook.com",
  "x.ai", "spotify.com", "pinterest.com", "perplexity.ai", "netflix.com",
  "weebly.com", "repo.maven.apache.org"
]
```

## Verify the gateway behavior

The notebook sends requests with no tools, a web-search tool without filters,
and a web-search tool with existing blocked domains. Use APIM's portal **Test**
tab and enable tracing for the `Create a response` operation. Inspect the
**Backend request body** to verify the merged blocklist and unchanged controls.
Do not infer successful policy merging solely from response citations: a model
response does not necessarily echo the request tool configuration.

For HTTP 401 with `PermissionDenied`, inspect the deployed backend using the
helper above and check APIM's managed identity role on that account. For an
invalid subscription-key error, check `APIM_SUBSCRIPTION_KEY` and its API scope.
The request helper includes the Azure error body and request ID in exceptions.
After changing helper code in a running notebook, reload it before retrying:

```python
import importlib
from src import lab
importlib.reload(lab)
send_response = lab.send_response
```

Local checks (from this directory):

```bash
python -m unittest discover -s tests -v
az bicep build --file main.bicep --outfile /tmp/web-search-blocklist.json
```

The policy tests require the .NET 8 SDK and access to NuGet on first run. They
compile and execute the C# expressions extracted from `policy.xml`, including
the tool guard, filter validation, and merge. They cover preserved entries,
case-insensitive duplicate avoidance, repeated application, multiple tools,
absent tools, preview tools, null values, and malformed filters. They simulate
the request body interface locally; deployment is still needed to validate
APIM's policy runtime and the live Foundry service.

## Files and cleanup

| File | Purpose |
| --- | --- |
| [policy.xml](policy.xml) | Conditional validation and additive blocklist merge |
| [main.bicep](main.bicep) | Dedicated API on existing APIM; optional backend for an existing endpoint |
| [src/lab.py](src/lab.py) | Environment loading, backend discovery, deployment, HTTP requests |
| [ai-foundry-web-search-blocklist.ipynb](ai-foundry-web-search-blocklist.ipynb) | Deployment and request walkthrough |
| [clean-up-resources.ipynb](clean-up-resources.ipynb) | Remove the lab API and any lab-created backend |

`main.bicep` fills the routing/authentication placeholders in `policy.xml`.
To adopt just the merge in an existing policy, copy its first inbound `<choose>`
block after `<base />` and keep your existing backend routing/authentication.
Place the merge after other policies that construct or replace `tools` so that
subsequent policies do not undo it.

Use the cleanup notebook when finished. It removes only this lab's API and,
when created by this lab, its backend. It does not delete the resource group,
APIM service, reused backend, Foundry resource, or model deployments. The APIM
identity and its Foundry role assignments are retained because other APIs may
share them.

## Reference

[Microsoft Learn: Web search with the Responses API — domain filtering](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search#domain-filtering)
