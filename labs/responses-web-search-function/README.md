---
name: Responses Web Search with Azure Functions
architectureDiagram: ""
categories:
  - Knowledge & Tools
  - Security & Access Control
services:
  - Azure API Management
  - Azure Functions
  - Microsoft Foundry
  - Microsoft Entra ID
shortDescription: Buffer Foundry web-search responses in APIM, then return an Entra-protected Function's final JSON or streamed answer.
detailedDescription: Inspect completed Responses API web_search_call output in APIM, pass the response and a CSV of source URLs to an Azure Function, and generate a final cited answer with GPT-5.6 Luna on a Foundry endpoint. Authenticate every application hop with Entra ID, buffer the initial response at the gateway, and relay the Function's final SSE response without reading its body in APIM.
tags:
  - Responses API
  - Web search
  - Streaming
  - Managed identity
authors: []
---

# Responses web search with Azure Functions

Start with [the deployment and test notebook](responses-web-search-function.ipynb). This lab creates an Azure Function and APIM API around an **existing Foundry account and GPT-5.6 Luna deployment**. The caller sends a Responses request and a CSV of URLs. APIM waits for the initial model response and, if it contains a completed hosted `web_search_call`, sends the response, original request, and CSV to the Function. The Function performs a second Foundry Responses request and returns a final cited answer.

The requested `gpt-5.6.-luna` is interpreted as the documented model ID **`gpt-5.6-luna`**. `FOUNDRY_DEPLOYMENT` is configurable for Azure deployment aliases. The notebook verifies that the selected deployment hosts this model; it does not create a model deployment or silently substitute another model. OpenAI's model documentation establishes model capabilities, but availability in a particular Azure subscription and region must be checked in Foundry.

## Behavior and architecture

```mermaid
sequenceDiagram
    participant C as Caller
    participant G as APIM
    participant M as Foundry Responses
    participant F as Azure Function
    C->>G: Entra token + Responses body + urls_csv
    G->>G: Validate tenant, audience, user object ID, scope
    alt Hosted web_search is offered
        G->>M: Managed identity; first Responses request
        M-->>G: JSON or SSE including hosted search output
        G->>G: Buffer entire response; inspect terminal output
        alt Completed web_search_call exists
            G->>F: APIM identity + original request + response + CSV
            F->>F: Easy Auth permits only APIM; validate CSV
            F->>M: Function identity; GPT-5.6 Luna removes blocked links, no tools
            M-->>F: Final JSON or incremental Responses SSE
            F-->>G: Final response / SSE chunks
            G-->>C: Function response, streaming without body inspection
        else Search was not used, or first response was incomplete/failed
            G-->>C: Original buffered JSON / SSE
        end
    else No web_search tool offered
        G->>M: Managed identity; direct Responses request
        M-->>G: JSON or SSE
        G-->>C: Direct response
    end
```

`web_search` is a **hosted Responses API tool**. Foundry executes it before returning `web_search_call` output; it is not an external `function_call` that APIM can intercept or satisfy with `function_call_output`. This lab post-processes the completed response with a second model invocation.

The first call uses `send-request` in APIM's **inbound** policy, followed by a full body read. This placement leaves the **backend** policy available to forward to the Function with `buffer-response="false"`. `forward-request` is only valid in the backend section, and an outbound `send-request`/`return-response` chain does not provide an incremental Function stream. Setting `buffer-response="true"` alone only buffers chunks; it does not wait for an entire SSE response.

When streaming, APIM extracts the terminal `response.completed`, `response.incomplete`, or `response.failed` object from the buffered initial SSE. Only a completed response containing a completed `web_search_call` invokes the Function. A malformed or truncated initial stream returns 502. No first-stage SSE events are mixed into the final Function stream.

The Function relays Foundry's final SSE bytes, preserving event types, deltas, response IDs, source annotations, and terminal events. It emits SSE comment heartbeats during long reads and closes the upstream connection if the client disconnects. A failure after streaming has started is an SSE `error` event; clients must require a terminal `response.completed`, not HTTP 200 alone.

## CSV and API contract

Call `POST https://<apim>.azure-api.net/web-search-function/openai/v1/responses` with a Gateway API Entra bearer token:

```json
{
  "model": "gpt-5.6-luna",
  "input": "Use web search to explain how APIM forwards streaming Azure Function responses. Cite official documentation.",
  "tools": [{"type": "web_search"}],
  "tool_choice": "required",
  "reasoning": {"effort": "low"},
  "stream": true,
  "urls_csv": "url\nhttps://learn.microsoft.com/azure/api-management/\nhttps://developers.openai.com/api/docs/"
}
```

`urls_csv` is a JSON string containing either a CSV row or one-column CSV file, with an optional `url` header. Quoting works, including quoted commas in URL paths. [sample-urls.csv](sample-urls.csv) is a ready-to-use example. APIM removes this lab-specific field before calling Foundry.

The Function uses the CSV as **blocked domains for the final answer**. It passes the original input and initial output as untrusted context, then asks the model to remove only links to those domains from the existing answer, with no tools. All other domains are allowed. The prompt requires preserving the rest of the answer exactly, including wording, formatting, whitespace, and unblocked links. For blocked Markdown or HTML links, it preserves visible text while removing the blocked target and the markup needed to unlink it. The Function never downloads URLs itself. It rejects non-HTTPS URLs, credentials, IP literals, local hostnames, custom ports, malformed CSV, more than 100 URLs, or more than 16,000 CSV bytes.

Blocked domains include their subdomains; URL paths do not restrict the list to exact pages. The CSV is caller supplied and **applies to the final-answer prompt only**. The first search uses the caller's `web_search` configuration, so its context can contain sources from blocked domains. URL removal depends on model instruction following: the Function relays the generated JSON or SSE without a deterministic URL scrubber. The prompt prohibits adding explanations, replacement links, or evidence limitations and requires returning the answer unchanged when no blocked links occur.

The internal Function endpoint `POST /api/process` receives:

```json
{
  "original_request": {"input": "...", "tools": [{"type": "web_search"}]},
  "initial_response": {
    "id": "resp_first",
    "status": "completed",
    "output": [{"type": "web_search_call", "status": "completed"}]
  },
  "urls_csv": "https://learn.microsoft.com/",
  "stream": true
}
```

The Function calls its configured deployment, irrespective of a caller's `model`, with `store=false`, low reasoning effort, at most 4096 output tokens, and no tools or tool-specific request options. The initial answer is context rather than a trusted instruction or continuation ID. Final JSON is the second Responses object; final streaming is the second Responses SSE stream. The final response ID differs from the initial ID. Citations are generated as Markdown links; a second hosted `web_search_call` and hosted-search citation annotations are not expected.

| Response header | Meaning |
| --- | --- |
| `x-lab-route: function` | APIM found an actual completed search and forwarded to the Function. |
| `x-lab-route: foundry` | No search tool was offered; ordinary direct proxying. |
| `x-lab-route: foundry-buffered` | Search was offered but no qualifying search completed; original response returned. |
| `x-lab-initial-buffered` | Whether APIM buffered the first response. |
| `x-lab-initial-response-id` | Initial response ID on the Function route, for test assertions. |

## Entra authentication

| Connection | Authentication and authorization |
| --- | --- |
| Notebook → APIM | Public-client Entra sign-in, Gateway API audience, `access_as_user` scope, and the configured user's object ID. |
| APIM → Foundry | APIM system-assigned identity; `https://ai.azure.com` audience; Cognitive Services OpenAI User. |
| APIM → Function | APIM system-assigned identity; `api://<function-app-client-id>` resource; Easy Auth validates tenant/audience and permits only APIM's object ID. |
| Function → Foundry | Function system-assigned identity; `https://ai.azure.com/.default`; Cognitive Services OpenAI User. |
| Function host → storage | Managed identity and Storage Blob Data Owner; storage shared keys disabled. |
| Deployment → Azure/Graph/SCM | Azure CLI Entra session; SCM and FTP basic publishing credentials disabled. |

The Function trigger uses `AuthLevel.ANONYMOUS` to disable **Function-key requirements**. Deployed platform authentication is mandatory through Easy Auth. It does not accept anonymous callers. The notebook proves both missing-token rejection and rejection of a valid Function-audience token belonging to a user. Do not expose this app outside Azure Easy Auth without an equivalent authentication layer.

The lab creates three single-tenant app registrations and corresponding service principals: Gateway API, notebook public client, and Function API. They contain no passwords or certificates. A Function delegated scope exists only to obtain a correctly-audienced token for the negative authorization test; it does not bypass the principal allowlist. App IDs and deployment outputs are saved in an ignored `.lab-state.json`; tokens remain in kernel memory.

Scope publication and notebook preauthorization use separate Microsoft Graph updates. The helper waits for each scope to become visible and retries the specific missing-permission error during replication. If registration setup is interrupted, rerun its notebook cell to reuse the saved application and scope IDs; keep `.lab-state.json`.

Hosted search accesses public web sources through Foundry's service. Those public websites do not authenticate via your Entra tenant; Entra protects the application and Azure service connections shown above. No API keys or Function keys are used by the lab.

## Run the lab

1. Have an existing Foundry deployment of `gpt-5.6-luna` with Responses and `web_search` available for the initial request. The account must be reachable from the public-cloud APIM and Function resources in this lab.
2. Authenticate Azure CLI to the intended tenant/subscription. You need Contributor plus RBAC Administrator (or Owner) for the lab and existing Foundry scopes, plus permission to create/configure Entra registrations and service principals. Tenant restrictions may require an administrator to perform the registration step.
3. From this folder, run `uv sync --group dev`. Copy `.env.example` to `.env`, supply the Foundry account/group/deployment, and optionally change the default `westus2` region and `Basicv2` APIM SKU. Select the lab's `.venv` notebook kernel.
4. Run [responses-web-search-function.ipynb](responses-web-search-function.ipynb) in order. It validates the model, runs offline tests, creates registrations, deploys [main.bicep](main.bicep), publishes the Function using Entra-authenticated zip deployment and remote build, and exercises all routes.
5. Use [clean-up-resources.ipynb](clean-up-resources.ipynb) when finished. It removes the Foundry role assignments, lab resource group, and app registrations. Keep `.lab-state.json` until cleanup is complete.

On Windows, the notebook helper resolves the Azure CLI's `az.cmd` launcher. It can also use an Azure CLI Python package installed in the active kernel. If neither is available, install Azure CLI with `winget install --exact --id Microsoft.AzureCLI`, fully close and reopen VS Code/Jupyter to refresh its PATH, and verify `az --version` and `az login`. A terminal in a different Conda environment may have a different PATH from the notebook. Restart the notebook kernel after updating `lab_helpers.py` to load the fix.

APIM provisioning can take 30–60 minutes. The default Basic v2 APIM service, B1 Linux hosting plan, and storage incur ongoing charges. Hosted search and model usage incur additional charges, normally for **two model requests and one search stage**. Existing Foundry resources remain after cleanup; only the lab's role assignments on that resource are removed. APIM may remain soft-deleted until separately purged or retention expires.

## Validation and limitations

The notebook tests missing/invalid/wrong-audience tokens, a valid but unauthorized Function caller, malformed requests, JSON proxying, tool offered but unused, completed search routing, final citations and response IDs, all three streaming routes, and Function CSV validation. It also checks deployed Easy Auth, publishing authentication, and storage settings.

Local tests use mocked Foundry HTTP/SSE and require no Azure credentials:

```bash
uv run --group dev pytest -q tests
az bicep build --file main.bicep --outfile /tmp/web-search-function.json
```

The gated-stream test asserts that a Function text event arrives while upstream completion is still blocked. Other tests verify CSV validation, tool-free request construction with blocked domains in the prompt, managed-identity headers, resource cleanup on disconnect, HTTP failure statuses, read timeouts, stream errors, and malformed SSE. These checks do not execute APIM policy expressions, Azure Easy Auth, or the model's URL-removal instructions; the live notebook is required for deployment validation.

This is a bounded, stateless lab: no conversations, `previous_response_id`, background requests, custom tools, or tool-result continuations. APIM pins the model and `store=false`, caps output tokens at 4096, accepts at most 64 KiB per request, and rejects initial responses over 1 MiB **after materializing them**. These checks are not a hard memory limit during buffering. The Function accepts at most 2 MB per envelope. Invalid CSV syntax/URLs are rejected by the Function after the first paid search; clients can call `parse_urls_csv` before submitting a request.

Each APIM upstream call has a 180-second timeout; Azure platform idle limits also apply. The initial search must finish before any final Function answer can stream, so first-token latency includes the full first stage. Use short prompts. Long research requests require a different asynchronous architecture. Do not add response-body diagnostics, inherited body transforms, or outbound body reads to this API: they can buffer the final SSE stream.

## Files

| File | Purpose |
| --- | --- |
| [responses-web-search-function.ipynb](responses-web-search-function.ipynb) | Deployment, authentication, and complete test walkthrough. |
| [clean-up-resources.ipynb](clean-up-resources.ipynb) | Remove lab resources, external role assignments, and registrations. |
| [main.bicep](main.bicep), [foundry-access.bicep](foundry-access.bicep) | APIM, keyless Function/storage, Easy Auth, and Foundry RBAC. |
| [policy.xml](policy.xml) | Initial response buffering, actual-tool detection, and Function routing. |
| [src/function_app.py](src/function_app.py) | Managed-identity Foundry HTTP client and incremental SSE relay. |
| [src/processing.py](src/processing.py) | CSV validation and final Responses request construction. |
| [lab_helpers.py](lab_helpers.py) | Entra registration setup and notebook parsing helpers. |
| [tests/](tests/) | Offline behavior tests. |

## References

- [Official OpenAI documentation: GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
- [Official OpenAI documentation: Responses web search and domain filters](https://developers.openai.com/api/docs/guides/tools-web-search)
- [Foundry Responses API web search and Entra examples](https://learn.microsoft.com/azure/ai-foundry/openai/how-to/web-search?view=foundry-classic)
- [APIM send-request policy](https://learn.microsoft.com/azure/api-management/send-request-policy)
- [APIM forward-request sections and buffer-response semantics](https://learn.microsoft.com/azure/api-management/forward-request-policy)
- [Azure Functions Python HTTP streams](https://learn.microsoft.com/azure/azure-functions/functions-bindings-http-webhook-trigger?pivots=programming-language-python#http-streams)
- [App Service Entra authentication and allowed principals](https://learn.microsoft.com/azure/app-service/configure-authentication-provider-aad)
- [Entra deployment with basic authentication disabled](https://learn.microsoft.com/azure/app-service/configure-basic-auth-disable)
