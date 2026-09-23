@description('Existing API Management service in the deployment resource group.')
param apimServiceName string

@description('Existing Foundry APIM backend name. Its URL is reused and this API authenticates with the APIM managed identity.')
param backendId string = ''

@description('Existing Foundry / Azure OpenAI resource endpoint, used only when backendId is empty. Include /openai/v1, but not /responses.')
param foundryBaseUrl string = ''

@secure()
@description('Optional Foundry API key for a new backend. If empty, use the APIM system-assigned managed identity.')
param foundryApiKey string = ''

@description('Responses path relative to the selected backend URL: /v1/responses for a backend ending in /openai.')
param backendResponsesPath string = '/v1/responses'

@description('Dedicated lab API name and ID.')
param apiName string = 'web-search-blocklist'

param apiPath string = 'web-search/openai'

@description('Optional existing Log Analytics workspace resource ID. Omit when APIM already exports GatewayLogs to a destination.')
param logAnalyticsWorkspaceId string = ''

@description('Streaming proxy origin provisioned and published by deploy(). Empty disables proxy routing.')
param streamingProxyUrl string = ''
@secure()
param streamingProxyKey string = ''

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimServiceName
}

// This template never provisions a Foundry account or a model deployment.
resource labBackend 'Microsoft.ApiManagement/service/backends@2024-05-01' = if (empty(backendId)) {
  parent: apim
  name: '${apiName}-foundry'
  properties: {
    description: 'Existing Foundry endpoint for the web-search blocklist lab'
    protocol: 'http'
    url: foundryBaseUrl
    credentials: empty(foundryApiKey) ? {} : {
      header: {
        'api-key': [foundryApiKey]
      }
    }
  }
}

resource proxyBackend 'Microsoft.ApiManagement/service/backends@2024-05-01' = if (!empty(streamingProxyUrl)) {
  parent: apim
  name: '${apiName}-streaming-proxy'
  properties: {
    protocol: 'http'
    url: streamingProxyUrl
    credentials: { header: { 'x-proxy-key': [streamingProxyKey] } }
  }
}

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: apiName
  properties: {
    displayName: 'Foundry web-search blocklist'
    description: 'Append blocked domains, redact matching URLs after actual web search, and log JSON and streaming metrics.'
    apiType: 'http'
    path: apiPath
    protocols: ['https']
    subscriptionRequired: true
    subscriptionKeyParameterNames: {
      header: 'api-key'
      query: 'subscription-key'
    }
  }
}

resource responses 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'create-response'
  properties: {
    displayName: 'Create a response'
    method: 'POST'
    urlTemplate: '/v1/responses'
    responses: []
  }
}

var selectedBackend = empty(backendId) ? labBackend.name : backendId
// Reused backends need this API's Entra token too; another API's policy is not inherited.
var backendAuthentication = !empty(backendId) || empty(foundryApiKey)
  ? '<authentication-managed-identity resource="https://ai.azure.com" ignore-error="false" />'
  : ''

var routing = empty(streamingProxyUrl)
  ? '{backend-authentication}<set-backend-service backend-id="{backend-id}" /><rewrite-uri template="{backend-responses-path}" copy-unmatched-params="true" />'
  : replace(loadTextContent('streaming-routing.xml'), '{streaming-proxy-backend-id}', proxyBackend.name)
var blocklistPolicy = replace(loadTextContent('policy.xml'), '{organization-blocked-domain-literals}', join(map(loadJsonContent('blocked-domains.json'), domain => '"${domain}"'), ', '))
var routedPolicy = replace(blocklistPolicy, '{backend-routing}', routing)

resource apiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: api
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: replace(replace(replace(routedPolicy, '{backend-id}', selectedBackend), '{backend-authentication}', backendAuthentication), '{backend-responses-path}', backendResponsesPath)
  }
  dependsOn: [responses]
}

// The Azure Monitor logger is shared by APIs on this existing service.
resource monitorLogger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: 'azuremonitor'
  properties: {
    loggerType: 'azureMonitor'
    isBuffered: false
  }
}

// Capture the frontend response: this is where outbound policies add headers.
// Body logging at either stage would introduce buffering on ordinary SSE calls.
var noBodyLogging = {
  headers: []
  body: { bytes: 0 }
}

resource apiDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = {
  parent: api
  name: 'azuremonitor'
  properties: {
    loggerId: monitorLogger.id
    alwaysLog: 'allErrors'
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    frontend: {
      request: noBodyLogging
      response: {
        headers: ['x-response-metrics', 'x-response-metrics-status', 'x-response-request-id', 'x-response-redaction', 'x-response-buffered']
        body: { bytes: 0 }
      }
    }
    backend: {
      request: noBodyLogging
      response: noBodyLogging
    }
  }
}

// Azure Monitor export is configured at service scope. Leave existing exports
// alone unless the caller explicitly supplies a destination for this lab.
resource gatewayLogs 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (!empty(logAnalyticsWorkspaceId)) {
  scope: apim
  name: '${apiName}-gateway-logs'
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        category: 'GatewayLogs'
        enabled: true
      }
    ]
  }
}

output apiId string = api.id
output responsesUrl string = '${apim.properties.gatewayUrl}/${apiPath}/v1/responses'
