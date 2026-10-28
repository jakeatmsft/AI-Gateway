targetScope = 'resourceGroup'

@description('Name of the existing Microsoft Foundry resource.')
param existingFoundryName string

@description('Object ID of the API Management managed identity.')
param apimPrincipalId string

var cognitiveServicesUserRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'a97b65f3-24c7-4388-baec-2e87135dc908'
)

resource existingFoundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: existingFoundryName
}

resource apimCognitiveServicesUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(existingFoundry.id, apimPrincipalId, cognitiveServicesUserRoleDefinitionId)
  scope: existingFoundry
  properties: {
    roleDefinitionId: cognitiveServicesUserRoleDefinitionId
    principalId: apimPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output foundryId string = existingFoundry.id
output roleAssignmentId string = apimCognitiveServicesUser.id
