// Deploys the logging lab around an existing Microsoft Foundry resource.
// The existing resource and its model deployments are not created or modified.

@description('API Management pricing tier.')
param apimSku string

@description('Configuration for the APIM subscriptions used by the lab clients.')
param apimSubscriptionsConfig array = []

@description('Name of the existing Microsoft Foundry resource.')
param existingFoundryName string

@description('Resource group containing the existing Microsoft Foundry resource.')
param existingFoundryResourceGroupName string

@description('Subscription containing the existing Microsoft Foundry resource.')
param existingFoundrySubscriptionId string = subscription().subscriptionId

@description('Base URL of the existing Foundry model inference endpoint, including a trailing slash.')
param existingFoundryEndpoint string

@description('Inference API type exposed by API Management.')
@allowed([
  'AzureOpenAIV1'
])
param inferenceAPIType string = 'AzureOpenAIV1'

@description('Path of the inference API in API Management.')
param inferenceAPIPath string = 'inference'

var existingFoundryConfig = [
  {
    name: existingFoundryName
    endpoint: existingFoundryEndpoint
  }
]

// 1. Log Analytics workspace
module lawModule '../../modules/operational-insights/v1/workspaces.bicep' = {
  name: 'lawModule'
}

// 2. Application Insights
module appInsightsModule '../../modules/monitor/v1/appinsights.bicep' = {
  name: 'appInsightsModule'
  params: {
    lawId: lawModule.outputs.id
    customMetricsOptedInType: 'WithDimensions'
  }
}

// 3. API Management
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

// 4. Allow APIM to call the keyless Foundry endpoint with its managed identity.
module existingFoundryAccessModule 'existing-foundry-access.bicep' = {
  name: 'existingFoundryAccessModule'
  scope: resourceGroup(existingFoundrySubscriptionId, existingFoundryResourceGroupName)
  params: {
    existingFoundryName: existingFoundryName
    apimPrincipalId: apimModule.outputs.principalId
  }
}

// 5. APIM inference API and built-in LLM diagnostics
module inferenceAPIModule '../../modules/apim/v3/inference-api.bicep' = {
  name: 'inferenceAPIModule'
  params: {
    policyXml: loadTextContent('policy.xml')
    apimLoggerId: apimModule.outputs.loggerId
    aiServicesConfig: existingFoundryConfig
    inferenceAPIType: inferenceAPIType
    inferenceAPIPath: inferenceAPIPath
  }
  dependsOn: [
    existingFoundryAccessModule
  ]
}

output logAnalyticsWorkspaceId string = lawModule.outputs.customerId
output apimServiceId string = apimModule.outputs.id
output apimResourceGatewayURL string = apimModule.outputs.gatewayUrl
output apimSubscriptions array = apimModule.outputs.apimSubscriptions
output existingFoundryId string = existingFoundryAccessModule.outputs.foundryId
output existingFoundryRoleAssignmentId string = existingFoundryAccessModule.outputs.roleAssignmentId
