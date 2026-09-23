targetScope = 'resourceGroup'

param location string = resourceGroup().location
@allowed(['Developer', 'Basicv2', 'Standardv2'])
param apimSku string = 'Basicv2'
param publisherEmail string
param publisherName string = 'AI Gateway labs'
@description('Existing Foundry account with a deployed gpt-5.6-luna model.')
param foundryName string
param foundryResourceGroup string
param foundrySubscriptionId string = subscription().subscriptionId
@description('OpenAI v1 base URL, e.g. https://NAME.openai.azure.com/openai/v1 (no trailing slash).')
param foundryEndpoint string
@description('Azure deployment name. No other model is silently substituted.')
param foundryDeployment string = 'gpt-5.6-luna'
@description('Gateway API Entra app registration: v2 tokens, access_as_user scope.')
param gatewayClientId string
@description('Function API Entra app registration: v2 tokens, api://CLIENT_ID identifier URI.')
param functionClientId string
@description('Entra object ID of the user permitted to run the notebook.')
param callerObjectId string

var suffix = uniqueString(resourceGroup().id)

resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: 'apim-wsf-${suffix}'
  location: location
  sku: { name: apimSku, capacity: 1 }
  identity: { type: 'SystemAssigned' }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
  }
}

// Dedicated Linux hosting avoids an Azure Files connection string and permits
// Entra-authenticated zip deployment. No host/storage/Function keys are used.
resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-wsf-${suffix}'
  location: location
  kind: 'linux'
  sku: { name: 'B1', tier: 'Basic', capacity: 1 }
  properties: { reserved: true }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stwsf${suffix}'
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    defaultToOAuthAuthentication: true
  }
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: 'func-wsf-${suffix}'
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
      appSettings: [
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
        { name: 'PYTHON_ENABLE_INIT_INDEXING', value: '1' }
        { name: 'AzureWebJobsStorage__accountName', value: storage.name }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'ENABLE_ORYX_BUILD', value: 'true' }
        { name: 'FOUNDRY_ENDPOINT', value: foundryEndpoint }
        { name: 'FOUNDRY_DEPLOYMENT', value: foundryDeployment }
      ]
    }
  }
}

resource functionAuth 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: functionApp
  name: 'authsettingsV2'
  properties: {
    platform: { enabled: true, runtimeVersion: '~1' }
    globalValidation: { requireAuthentication: true, unauthenticatedClientAction: 'Return401' }
    httpSettings: { requireHttps: true }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: functionClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${subscription().tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [functionClientId, 'api://${functionClientId}']
          defaultAuthorizationPolicy: {
            allowedPrincipals: { identities: [apim.identity.principalId] }
          }
        }
      }
    }
    login: { tokenStore: { enabled: false } }
  }
}

resource scmAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: functionApp
  name: 'scm'
  properties: { allow: false }
}

resource ftpAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: functionApp
  name: 'ftp'
  properties: { allow: false }
}

// HTTP-only host uses blob storage for host state and internal host secrets.
// It has no Blob/Queue triggers that would need additional trigger roles.
var blobOwnerRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')
resource hostStorageAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, blobOwnerRole)
  scope: storage
  properties: {
    roleDefinitionId: blobOwnerRole
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

module foundryAccess 'foundry-access.bicep' = {
  name: 'web-search-foundry-access'
  scope: resourceGroup(foundrySubscriptionId, foundryResourceGroup)
  params: {
    foundryName: foundryName
    principalIds: [apim.identity.principalId, functionApp.identity.principalId]
  }
}

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'web-search-function'
  properties: {
    displayName: 'Responses web search with Azure Functions'
    path: 'web-search-function/openai/v1'
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
  '{function-endpoint}': 'https://${functionApp.properties.defaultHostName}'
  '{deployment}': foundryDeployment
}
var policy = reduce(items(replacements), loadTextContent('policy.xml'), (text, item) => replace(text, item.key, item.value))

resource apiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: api
  name: 'policy'
  properties: { format: 'rawxml', value: policy }
  dependsOn: [operation, functionAuth, foundryAccess]
}

output gatewayUrl string = '${apim.properties.gatewayUrl}/${api.properties.path}/responses'
output functionUrl string = 'https://${functionApp.properties.defaultHostName}/api/process'
output functionAppName string = functionApp.name
output apimName string = apim.name
output apimPrincipalId string = apim.identity.principalId
output functionPrincipalId string = functionApp.identity.principalId
output storageName string = storage.name
output foundryRoleAssignmentIds array = foundryAccess.outputs.roleAssignmentIds
