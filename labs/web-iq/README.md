---
name: Microsoft Web IQ Usage Tracking
architectureDiagram: ""
categories:
  - Knowledge & Tools
  - Platform Capabilities
services:
  - Microsoft Web IQ
  - Azure API Management
  - Application Insights
  - Log Analytics
shortDescription: Track Microsoft Web IQ API consumption and block Browse at the APIM gateway.
detailedDescription: Route Microsoft Web IQ Web Search requests through Azure API Management with API key or optional Microsoft Entra ID authentication, and block the published Browse operation before it reaches Web IQ. Emit privacy-conscious custom metrics to Application Insights so operators can attribute request volume, blocked calls, authentication mode, response status, gateway errors, and latency to individual APIM subscriptions and operations.
tags:
  - Web grounding
  - Usage tracking
  - Custom metrics
authors: []
---

# APIM ❤️ Microsoft Web IQ

## [Microsoft Web IQ Usage Tracking lab](web-iq.ipynb)

This lab places Azure API Management in front of the [Microsoft Web IQ](https://webiq.microsoft.ai/documentation/overview/) Web Search and Browse APIs. Clients authenticate to APIM with subscription keys. For Web IQ authentication, APIM either injects its secret API key or passes through an optional Microsoft Entra ID bearer token. APIM blocks Browse with a structured `403` before any backend call and emits custom usage metrics to Application Insights.

```mermaid
flowchart LR
    Client[Client application]
    MicrosoftEntra[Microsoft Entra ID]
    subgraph Gateway[Azure API Management]
        Subscription[Validate APIM subscription]
        RequestMetric[Emit request metric]
        Operation{Operation?}
        BlockMetric[Emit blocked-request metric]
        Forbidden[Return structured 403]
        Auth{Web IQ bearer token present?}
        NamedValue[(Secret named value)]
        ApiKey[Inject x-apikey<br/>from secret named value]
        Entra[Pass through Entra bearer token]
        ResponseMetric[Emit response and latency metrics]
        ErrorMetric[Emit gateway-error metric]
    end
    Client -.->|Client credentials<br/>Web IQ scope| MicrosoftEntra
    MicrosoftEntra -.->|Bearer token| Client
    Client -->|APIM subscription key<br/>optional Web IQ bearer token| Subscription
    Subscription --> RequestMetric --> Operation
    Operation -->|Browse| BlockMetric --> Forbidden --> Client
    Operation -->|Web Search| Auth
    NamedValue --> ApiKey
    Auth -->|No| ApiKey --> WebIQ[Microsoft Web IQ]
    Auth -->|Yes| Entra --> WebIQ
    WebIQ --> ResponseMetric --> Client
    Subscription -.->|Policy or gateway failure| ErrorMetric
    ErrorMetric --> Client
    RequestMetric -.-> AppInsights[Application Insights]
    BlockMetric -.-> AppInsights
    ResponseMetric -.-> AppInsights
    ErrorMetric -.-> AppInsights
    AppInsights --> Logs[Log Analytics]
```

### What you'll learn

- Store the Web IQ API key as a secret APIM named value.
- Optionally authenticate Web IQ with a Microsoft Entra ID app-only token.
- Proxy Web Search without exposing the upstream credential.
- Block Browse before APIM sends a request to Web IQ.
- Attribute usage to APIM subscriptions and operations.
- Query request volume, response status, and latency in Application Insights.

The policy records bounded dimensions only. It does not log search queries, response content, Web IQ API keys, or APIM subscription keys.

### Prerequisites

- [Microsoft Web IQ access](https://webiq.microsoft.ai/documentation/overview/) and an API key from Web IQ Profile Management. Web IQ is currently limited access.
- Optional for [Entra ID authentication](https://webiq.microsoft.ai/documentation/authentication/#entra-id): an app registration client ID bound in Web IQ Profile Management, its tenant ID, and a client secret.
- [Python 3.12 or later](https://www.python.org/).
- [VS Code](https://code.visualstudio.com/) with the [Jupyter extension](https://marketplace.visualstudio.com/items?itemName=ms-toolsai.jupyter).
- [uv](https://docs.astral.sh/uv/) — run `uv sync` at the repository root.
- An Azure subscription with Contributor + RBAC Administrator, or Owner, permissions.
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) installed and authenticated.

### 🚀 Get started

Open [web-iq.ipynb](web-iq.ipynb) and run the steps in order.

### 🗑️ Clean up resources

When finished, run [clean-up-resources.ipynb](clean-up-resources.ipynb) to remove the lab resource group and avoid further charges.
