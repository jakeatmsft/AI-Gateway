targetScope = 'resourceGroup'

param foundryName string
param principalIds array

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryName
}

var openAIUserRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
resource access 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principalId in principalIds: {
  name: guid(foundry.id, principalId, openAIUserRole)
  scope: foundry
  properties: {
    roleDefinitionId: openAIUserRole
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}]

output roleAssignmentIds array = [for i in range(0, length(principalIds)): access[i].id]
