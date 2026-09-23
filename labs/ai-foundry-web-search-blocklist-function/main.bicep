targetScope = 'resourceGroup'

param location string = resourceGroup().location
@allowed(['Developer', 'Basicv2', 'Standardv2'])
param apimSku string = 'Basicv2'
param publisherEmail string
param publisherName string = 'AI Gateway labs'
@description('Existing Foundry account with a Responses/web_search-capable deployment.')
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
@description('Organization blocklist, merged with every caller web_search blocklist by APIM.')
@minLength(1)
@maxLength(100)
param blockedDomains array = loadJsonContent('blocked-domains.json')
@description('Optional existing Log Analytics workspace resource ID for APIM GatewayLogs.')
param logAnalyticsWorkspaceId string = ''

@description('Reuse an existing APIM service when set; otherwise create a dedicated service.')
param existingApimName string = ''
param existingApimResourceGroup string = resourceGroup().name
param existingApimSubscriptionId string = subscription().subscriptionId
@description('Set false when the reused APIM identity already has Foundry inference access.')
param createFoundryRoleAssignment bool = true

var suffix = uniqueString(resourceGroup().id)
var selectedApimName = empty(existingApimName) ? 'apim-wsbf-${suffix}' : existingApimName
var selectedApimGroup = empty(existingApimName) ? resourceGroup().name : existingApimResourceGroup
var selectedApimSubscription = empty(existingApimName) ? subscription().subscriptionId : existingApimSubscriptionId

resource existingApim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: selectedApimName
  scope: resourceGroup(selectedApimSubscription, selectedApimGroup)
}

var apimPrincipalId = empty(existingApimName) ? apim!.identity.principalId : existingApim.identity.principalId


resource apim 'Microsoft.ApiManagement/service@2024-05-01' = if (empty(existingApimName)) {
  name: 'apim-wsbf-${suffix}'
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
  name: 'plan-wsbf-${suffix}'
  location: location
  kind: 'linux'
  sku: { name: 'B1', tier: 'Basic', capacity: 1 }
  properties: { reserved: true }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stwsbf${suffix}'
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    publicNetworkAccess: 'Disabled'
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    defaultToOAuthAuthentication: true
  }
}

// Keep host storage private; outbound VNet integration does not change the
// public, Entra-protected Function endpoint used by APIM.
resource hostNetwork 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-wsbf-${suffix}'
  location: location
  properties: {
    addressSpace: { addressPrefixes: ['10.83.0.0/24'] }
    subnets: [
      {
        name: 'functions'
        properties: {
          addressPrefix: '10.83.0.0/26'
          delegations: [{ name: 'web', properties: { serviceName: 'Microsoft.Web/serverFarms' } }]
        }
      }
      {
        name: 'private-endpoints'
        properties: { addressPrefix: '10.83.0.64/27', privateEndpointNetworkPolicies: 'Disabled' }
      }
    ]
  }
}

resource blobDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.blob.core.windows.net'
  location: 'global'
}

resource blobDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: blobDns
  name: 'function-host'
  location: 'global'
  properties: { registrationEnabled: false, virtualNetwork: { id: hostNetwork.id } }
}

resource blobEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-wsbf-blob-${suffix}'
  location: location
  properties: {
    subnet: { id: '${hostNetwork.id}/subnets/private-endpoints' }
    privateLinkServiceConnections: [{
      name: 'host-blob'
      properties: { privateLinkServiceId: storage.id, groupIds: ['blob'] }
    }]
  }
}

resource blobZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: blobEndpoint
  name: 'default'
  properties: { privateDnsZoneConfigs: [{ name: 'blob', properties: { privateDnsZoneId: blobDns.id } }] }
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: 'func-wsbf-${suffix}'
  location: location
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    virtualNetworkSubnetId: '${hostNetwork.id}/subnets/functions'
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
        { name: 'WEBSITES_CONTAINER_START_TIME_LIMIT', value: '600' }
        { name: 'AzureWebJobsStorage__accountName', value: storage.name }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        // The notebook packages Linux Python 3.12 wheels before publication.
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'false' }
        { name: 'ENABLE_ORYX_BUILD', value: 'false' }
        { name: 'ORGANIZATION_BLOCKED_DOMAINS', value: string(blockedDomains) }
        { name: 'FOUNDRY_RESPONSES_URL', value: '${foundryEndpoint}/responses' }
        { name: 'FOUNDRY_DEPLOYMENT', value: foundryDeployment }
        { name: 'REDACTION_URL', value: 'https://func-wsbf-${suffix}.azurewebsites.net/api/redact' }
        { name: 'REDACTION_AUDIENCE', value: 'api://${functionClientId}' }
      ]
    }
  }
  dependsOn: [blobZoneGroup, blobDnsLink]
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
            // APIM invokes either route; the relay invokes the redactor as itself.
            allowedPrincipals: { identities: [apimPrincipalId, functionApp.identity.principalId] }
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

module foundryAccess 'foundry-access.bicep' = if (createFoundryRoleAssignment) {
  name: 'web-search-foundry-access'
  scope: resourceGroup(foundrySubscriptionId, foundryResourceGroup)
  params: {
    foundryName: foundryName
    principalIds: [apimPrincipalId]
  }
}

module relayFoundryAccess 'foundry-access.bicep' = {
  name: 'web-search-relay-foundry-access'
  scope: resourceGroup(foundrySubscriptionId, foundryResourceGroup)
  params: {
    foundryName: foundryName
    principalIds: [functionApp.identity.principalId]
  }
}

output gatewayUrl string = gateway.outputs.gatewayUrl
output apiId string = gateway.outputs.apiId
output apimReused bool = !empty(existingApimName)
output apimResourceGroup string = selectedApimGroup
output apimSubscriptionId string = selectedApimSubscription
output functionUrl string = 'https://${functionApp.properties.defaultHostName}/api/redact'
output functionAppName string = functionApp.name
output apimName string = selectedApimName
output apimPrincipalId string = apimPrincipalId
output functionPrincipalId string = functionApp.identity.principalId
output storageName string = storage.name
output foundryRoleAssignmentIds array = concat(createFoundryRoleAssignment ? foundryAccess!.outputs.roleAssignmentIds : [], relayFoundryAccess.outputs.roleAssignmentIds)


module gateway 'apim-api.bicep' = {
  name: 'web-search-blocklist-api'
  scope: resourceGroup(selectedApimSubscription, selectedApimGroup)
  params: {
    apimName: selectedApimName
    gatewayClientId: gatewayClientId
    functionClientId: functionClientId
    callerObjectId: callerObjectId
    functionHostName: functionApp.properties.defaultHostName
    foundryEndpoint: foundryEndpoint
    foundryDeployment: foundryDeployment
    blockedDomains: blockedDomains
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
  dependsOn: [functionAuth, foundryAccess, relayFoundryAccess]
}
