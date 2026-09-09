// ------------------
//    PARAMETERS
// ------------------

@description('The API Management SKU used by the lab.')
param apimSku string = 'Basicv2'

@description('Configuration for APIM subscriptions used to attribute Web IQ usage.')
param apimSubscriptionsConfig array = []

@description('Path of the Web IQ API in API Management.')
param webIqApiPath string = 'web-iq'

@description('Microsoft Web IQ v3 service URL.')
param webIqServiceUrl string = 'https://api.microsoft.ai/v3'

@secure()
@description('Microsoft Web IQ API key. Stored as a secret named value in API Management.')
param webIqApiKey string

// ------------------
//    VARIABLES
// ------------------

var resourceSuffix = uniqueString(subscription().id, resourceGroup().id)
var apiManagementName = 'apim-${resourceSuffix}'
var appInsightsLoggerId = resourceId('Microsoft.ApiManagement/service/loggers', apiManagementName, 'appinsights-logger')

var logSettings = {
  headers: []
  body: {
    bytes: 0
  }
}

// ------------------
//    RESOURCES
// ------------------

// 1. Log Analytics Workspace
module lawModule '../../modules/operational-insights/v1/workspaces.bicep' = {
  name: 'lawModule'
}

// 2. Application Insights with dimensional custom metrics enabled
module appInsightsModule '../../modules/monitor/v1/appinsights.bicep' = {
  name: 'appInsightsModule'
  params: {
    lawId: lawModule.outputs.id
    customMetricsOptedInType: 'WithDimensions'
  }
}

// 3. API Management and per-consumer subscriptions
module apimModule '../../modules/apim/v3/apim.bicep' = {
  name: 'apimModule'
  params: {
    apimSku: apimSku
    apimSubscriptionsConfig: apimSubscriptionsConfig
    lawId: lawModule.outputs.id
    appInsightsId: appInsightsModule.outputs.id
    appInsightsInstrumentationKey: appInsightsModule.outputs.instrumentationKey
  }
}

resource apimService 'Microsoft.ApiManagement/service@2024-06-01-preview' existing = {
  name: apiManagementName
  dependsOn: [
    apimModule
  ]
}

// 4. Keep the upstream Web IQ credential in APIM, not in client code or policies
resource webIqApiKeyNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  name: 'web-iq-api-key'
  parent: apimService
  properties: {
    displayName: 'web-iq-api-key'
    secret: true
    value: webIqApiKey
    tags: [
      'web-iq'
    ]
  }
}

// 5. Configure Web IQ as an APIM backend
resource webIqBackend 'Microsoft.ApiManagement/service/backends@2024-06-01-preview' = {
  name: 'web-iq-backend'
  parent: apimService
  properties: {
    description: 'Microsoft Web IQ v3 API'
    protocol: 'http'
    url: webIqServiceUrl
  }
}

// 6. Import the Web Search and Browse operations
resource webIqApi 'Microsoft.ApiManagement/service/apis@2024-06-01-preview' = {
  name: 'web-iq-api'
  parent: apimService
  properties: {
    apiRevision: '1'
    apiType: 'http'
    description: 'Microsoft Web IQ routed through Azure API Management'
    displayName: 'Microsoft Web IQ'
    format: 'openapi+json'
    path: webIqApiPath
    protocols: [
      'https'
    ]
    serviceUrl: webIqServiceUrl
    subscriptionKeyParameterNames: {
      header: 'Ocp-Apim-Subscription-Key'
      query: 'subscription-key'
    }
    subscriptionRequired: true
    type: 'http'
    value: loadTextContent('openapi.json')
  }
}

// 7. Route allowed operations to Web IQ, block Browse, and emit usage metrics
resource webIqApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-06-01-preview' = {
  name: 'policy'
  parent: webIqApi
  properties: {
    format: 'rawxml'
    value: loadTextContent('policy.xml')
  }
  dependsOn: [
    webIqBackend
    webIqApiKeyNamedValue
  ]
}

// 8. Connect this API to Application Insights without logging bodies or credentials
resource webIqApiDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2022-08-01' = {
  name: 'applicationinsights'
  parent: webIqApi
  properties: {
    alwaysLog: 'allErrors'
    httpCorrelationProtocol: 'W3C'
    logClientIp: false
    loggerId: appInsightsLoggerId
    metrics: true
    verbosity: 'information'
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    frontend: {
      request: logSettings
      response: logSettings
    }
    backend: {
      request: logSettings
      response: logSettings
    }
  }
  dependsOn: [
    apimModule
  ]
}

// ------------------
//    OUTPUTS
// ------------------

output logAnalyticsWorkspaceId string = lawModule.outputs.customerId
output applicationInsightsName string = appInsightsModule.outputs.name
output apimServiceId string = apimModule.outputs.id
output apimResourceGatewayURL string = apimModule.outputs.gatewayUrl
output apimSubscriptions array = apimModule.outputs.apimSubscriptions
output webIqApiPath string = webIqApiPath
