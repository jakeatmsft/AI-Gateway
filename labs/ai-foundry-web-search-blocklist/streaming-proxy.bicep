param apimServiceName string
param apiName string
param apiPath string
param foundryResponsesUrl string
param location string = resourceGroup().location
@secure()
param foundryApiKey string = ''
@secure()
param proxyApiKey string

var suffix = uniqueString(resourceGroup().id, apiName)
resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = { name: apimServiceName }
resource usageApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: '${apiName}-usage'
  properties: {
    displayName: 'Trusted web-search usage reports'
    path: '${apiPath}-usage'
    protocols: ['https']
    apiType: 'http'
    subscriptionRequired: true
    subscriptionKeyParameterNames: { header: 'api-key', query: 'subscription-key' }
  }
}
resource usageOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: usageApi
  name: 'report-usage'
  properties: { displayName: 'Report web-search usage', method: 'POST', urlTemplate: '/reports', responses: [] }
}
resource reporterSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: apim
  name: '${apiName}-proxy-reporter'
  properties: { displayName: 'Streaming proxy only', scope: usageApi.id, state: 'active', allowTracing: false }
}
resource usagePolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: usageApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: replace(loadTextContent('usage-policy.xml'), '{reporter-subscription-id}', reporterSubscription.name)
  }
  dependsOn: [usageOperation]
}
resource monitorLogger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: 'azuremonitor'
  properties: { loggerType: 'azureMonitor', isBuffered: false }
}
var noBody = { headers: [], body: { bytes: 0 } }
resource usageDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = {
  parent: usageApi
  name: 'azuremonitor'
  properties: {
    loggerId: monitorLogger.id
    alwaysLog: 'allErrors'
    sampling: { samplingType: 'fixed', percentage: 100 }
    frontend: {
      request: noBody
      response: {
        headers: ['x-web-search-count', 'x-web-search-count-status', 'x-web-search-request-id']
        body: { bytes: 0 }
      }
    }
    backend: { request: noBody, response: noBody }
  }
}
resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-wsc-${suffix}'
  location: location
  kind: 'linux'
  sku: { name: 'B1', tier: 'Basic', capacity: 1 }
  properties: { reserved: true }
}
resource proxyApp 'Microsoft.Web/sites@2024-04-01' = {
  name: 'func-wsc-${suffix}-http'
  location: location
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'Python|3.12'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      healthCheckPath: '/api/healthz'
    }
  }
  dependsOn: [usagePolicy]
}

// Apply settings separately, as for the validated HTTP-only Function deployment.
resource proxySettings 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: proxyApp
  name: 'appsettings'
  properties: {
    SCM_DO_BUILD_DURING_DEPLOYMENT: 'true'
    WEBSITES_CONTAINER_START_TIME_LIMIT: '600'
    PROXY_API_KEY: proxyApiKey
    FOUNDRY_RESPONSES_URL: foundryResponsesUrl
    FOUNDRY_API_KEY: foundryApiKey
    USAGE_REPORT_URL: '${apim.properties.gatewayUrl}/${apiPath}-usage/reports'
    USAGE_REPORT_KEY: reporterSubscription.listSecrets().primaryKey
    FUNCTIONS_EXTENSION_VERSION: '~4'
    FUNCTIONS_WORKER_RUNTIME: 'python'
    PYTHON_ENABLE_INIT_INDEXING: '1'
    // No queue/timer/blob/Durable bindings. Host keys use the platform filesystem.
    AzureWebJobsSecretStorageType: 'files'
  }
}

// Console logs retain failed usage-report IDs/counts without an Azure Storage account.
resource proxyLogs 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: proxyApp
  name: 'logs'
  properties: {
    applicationLogs: { fileSystem: { level: 'Information' } }
    httpLogs: { fileSystem: { enabled: true, retentionInDays: 3, retentionInMb: 35 } }
  }
  dependsOn: [proxySettings]
}

output proxyAppName string = proxyApp.name
output proxyPrincipalId string = proxyApp.identity.principalId
output proxyUrl string = 'https://${proxyApp.properties.defaultHostName}'
output usageUrl string = '${apim.properties.gatewayUrl}/${apiPath}-usage/reports'
output planId string = plan.id
output proxyAppId string = proxyApp.id
output usageApiId string = usageApi.id
output reporterSubscriptionId string = reporterSubscription.id
