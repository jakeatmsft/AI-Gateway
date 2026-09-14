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

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: apiName
  properties: {
    displayName: 'Foundry web-search blocklist'
    description: 'Append organization blocked domains to Responses API web_search tools.'
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

resource apiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: api
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: replace(replace(replace(loadTextContent('policy.xml'), '{backend-id}', selectedBackend), '{backend-authentication}', backendAuthentication), '{backend-responses-path}', backendResponsesPath)
  }
  dependsOn: [responses]
}

output apiId string = api.id
output responsesUrl string = '${apim.properties.gatewayUrl}/${apiPath}/v1/responses'
