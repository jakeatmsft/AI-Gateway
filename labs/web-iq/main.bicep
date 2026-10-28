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

@description('Optional Web IQ API key for testing when the APIM managed identity cannot be registered with Web IQ. Not recommended for production. Leave empty to use managed identity.')
@secure()
param webIqApiKey string = ''

@description('Allowed logical operations, shared by REST and MCP. An empty array denies all tool operations.')
@allowed(['web', 'videos', 'news', 'images', 'browse'])
param webIqAllowedOperations array = ['web']

@description('Denied logical operations. Deny takes precedence over allow.')
@allowed(['web', 'videos', 'news', 'images', 'browse'])
param webIqDeniedOperations array = ['browse']

@description('Maximum admitted HTTP requests per team per minute, including MCP protocol messages. Requires an APIM tier supporting rate-limit-by-key.')
@minValue(1)
param webIqRequestsPerMinute int = 60

@description('Maximum admitted REST operations and MCP tool calls per team per UTC day. Protocol messages are excluded.')
@minValue(1)
param webIqDailyQuota int = 1000

@description('Block web results when any standard Content Safety score reaches this severity. Use 8 for scores only.')
@allowed([0, 2, 4, 6, 8])
param contentSafetyBlockThreshold int = 4

@description('Azure region for the Content Safety text moderation resource.')
param contentSafetyLocation string = resourceGroup().location

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

// Include only the selected authentication policy so managed identity deployments do not need a named value.
var webIqAuthenticationPolicy = empty(webIqApiKey)
  ? '<authentication-managed-identity resource="https://api.microsoft.ai" />'
  : '<set-header name="x-apikey" exists-action="override"><value>{{web-iq-api-key}}</value></set-header>'

// Substitute only validated configuration; the API key is referenced by its secret named value.
var configuredPolicy = replace(replace(replace(replace(replace(replace(loadTextContent('policy.xml'),
  '{web-iq-authentication-policy}', webIqAuthenticationPolicy),
  '{web-iq-authentication-label}', empty(webIqApiKey) ? 'Managed identity' : 'API key (test only)'),
  '{web-iq-allowed-operations}', join(webIqAllowedOperations, ',')),
  '{web-iq-denied-operations}', join(webIqDeniedOperations, ',')),
  '{web-iq-requests-per-minute}', string(webIqRequestsPerMinute)),
  '{web-iq-daily-quota}', string(webIqDailyQuota))

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
    apimManagedIdentityType: 'SystemAssigned'
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

resource webIqApiKeyNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = if (!empty(webIqApiKey)) {
  name: 'web-iq-api-key'
  parent: apimService
  properties: {
    displayName: 'web-iq-api-key'
    secret: true
    value: webIqApiKey
  }
}

// Content Safety is always enabled in this example and uses APIM's system identity.
resource contentSafetyResource 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: 'contentsafety-${resourceSuffix}'
  location: contentSafetyLocation
  kind: 'ContentSafety'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: 'contentsafety-${resourceSuffix}'
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
  }
}

var cognitiveServicesUserRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
resource contentSafetyRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(contentSafetyResource.id, apimService.id, cognitiveServicesUserRoleId)
  scope: contentSafetyResource
  properties: {
    roleDefinitionId: cognitiveServicesUserRoleId
    principalId: apimModule.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

resource contentSafetyPolicyFragment 'Microsoft.ApiManagement/service/policyFragments@2024-05-01' = {
  name: 'web-iq-content-safety'
  parent: apimService
  properties: {
    description: 'Moderate complete Web IQ responses using the four standard text categories.'
    format: 'rawxml'
    value: replace(replace(loadTextContent('content-safety-outbound.xml'), '{content-safety-endpoint}', contentSafetyResource.properties.endpoint), '{content-safety-block-threshold}', string(contentSafetyBlockThreshold))
  }
  dependsOn: [
    contentSafetyRoleAssignment
  ]
}

// 4. Configure Web IQ as an APIM backend
resource webIqBackend 'Microsoft.ApiManagement/service/backends@2024-06-01-preview' = {
  name: 'web-iq-backend'
  parent: apimService
  properties: {
    description: 'Microsoft Web IQ v3 API'
    protocol: 'http'
    url: webIqServiceUrl
  }
}

// 5. Import the Web Search, Browse, and streamable HTTP MCP operations
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

// 6. Authenticate, enforce operation permissions and team limits, and emit usage metrics
resource webIqApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-06-01-preview' = {
  name: 'policy'
  parent: webIqApi
  properties: {
    format: 'rawxml'
    value: configuredPolicy
  }
  dependsOn: [
    webIqBackend
    webIqApiKeyNamedValue
    contentSafetyPolicyFragment
  ]
}

// 7. Connect this API to Application Insights without logging bodies or credentials
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
output contentSafetyEndpoint string = contentSafetyResource.properties.endpoint
output apimManagedIdentityPrincipalId string = apimModule.outputs.principalId
