targetScope = 'resourceGroup'

param apimName string
param gatewayClientId string
param functionClientId string
param callerObjectId string
param functionHostName string
param foundryEndpoint string
param foundryDeployment string
param blockedDomains array
param logAnalyticsWorkspaceId string = ''

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'web-search-blocklist-function'
  properties: {
    displayName: 'Foundry web-search blocklist with regex URL redaction'
    path: 'web-search-blocklist-function/openai/v1'
    protocols: ['https']
    subscriptionRequired: false
    type: 'http'
  }
}

resource operation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'create-response'
  properties: {
    displayName: 'Create response'
    method: 'POST'
    urlTemplate: '/responses'
  }
}

var replacements = {
  '{tenant-id}': subscription().tenantId
  '{gateway-client-id}': gatewayClientId
  '{function-client-id}': functionClientId
  '{caller-object-id}': callerObjectId
  '{foundry-endpoint}': foundryEndpoint
  '{deployment}': foundryDeployment
  '{blocked-domains}': string(blockedDomains)
  '{function-endpoint}': 'https://${functionHostName}'
}
var policy = reduce(items(replacements), loadTextContent('policy.xml'), (text, item) => replace(text, item.key, item.value))

resource apiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: api
  name: 'policy'
  properties: { format: 'rawxml', value: policy }
  dependsOn: [operation]
}

resource monitorLogger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: 'azuremonitor'
  properties: { loggerType: 'azureMonitor', isBuffered: false }
}

resource diagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = {
  parent: api
  name: 'azuremonitor'
  properties: {
    loggerId: monitorLogger.id
    sampling: { samplingType: 'fixed', percentage: 100 }
    alwaysLog: 'allErrors'
    frontend: {
      request: { headers: [], body: { bytes: 0 } }
      response: {
        headers: ['x-response-request-id', 'x-response-metrics', 'x-response-metrics-status', 'x-lab-filter']
        body: { bytes: 0 }
      }
    }
    backend: {
      request: { headers: [], body: { bytes: 0 } }
      response: { headers: [], body: { bytes: 0 } }
    }
  }
}

resource gatewayLogs 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (!empty(logAnalyticsWorkspaceId)) {
  name: 'web-search-blocklist-function'
  scope: apim
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [{ category: 'GatewayLogs', enabled: true }]
  }
}

output gatewayUrl string = '${apim.properties.gatewayUrl}/${api.properties.path}/responses'
output apiId string = api.id
