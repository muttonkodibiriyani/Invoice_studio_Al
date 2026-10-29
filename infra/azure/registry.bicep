// Container registry for the application image, plus AcrPull for the app identity.
param name string
param location string
param tags object = {}
param sku string = 'Basic'
param principalId string
@description('Log Analytics workspace that receives registry sign-ins and every push, pull and delete.')
param logWorkspaceId string

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: sku
  }
  properties: {
    adminUserEnabled: false
    // Public, behind Entra ID only (security review L4, deferred): Basic has no private endpoints, and
    // the deploy workflow's `az acr build` and the image pull both reach it from outside the network.
    publicNetworkAccess: 'Enabled'
  }
}

// AcrPull
resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, principalId, '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

resource audit 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'audit-to-log-analytics'
  scope: registry
  properties: {
    workspaceId: logWorkspaceId
    logs: [
      {
        category: 'ContainerRegistryLoginEvents'
        enabled: true
      }
      {
        category: 'ContainerRegistryRepositoryEvents'
        enabled: true
      }
    ]
  }
}

output loginServer string = registry.properties.loginServer
output registryName string = registry.name
