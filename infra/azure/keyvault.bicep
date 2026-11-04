// Key Vault, the counterpart of Secret Manager. Three secrets:
//   inv-studio-database-url  the psycopg connection string (the admin login until docs/AZURE.md §5a moves the app to its own role)
//   inv-studio-vault-key     the Fernet key that encrypts the `credentials` table
//   inv-studio-foundry-key   the Azure AI Foundry api-key
// `writeSecrets` is true on the first deployment. Set it to false when re-running the template
// after a secret was rotated with `az keyvault secret set`, so the rotation is not overwritten.
param name string
param location string
param tags object = {}
param principalId string
@description('Log Analytics workspace that receives the vault audit log: every secret read, write and access refusal.')
param logWorkspaceId string
param writeSecrets bool = true
@description('Enabled until the private endpoint is proven, then Disabled (scripts/azure_lockdown.py): the vault is then reachable only through its private endpoint, and ARM template deployments.')
@allowed([
  'Enabled'
  'Disabled'
])
param publicNetworkAccess string = 'Enabled'
param enablePurgeProtection bool = true
@minValue(7)
@maxValue(90)
param softDeleteRetentionInDays int = 90
@secure()
param databaseUrl string
@secure()
param vaultKey string
@secure()
param foundryApiKey string = ''

var unset = 'set-me-with-az-keyvault-secret-set'

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: softDeleteRetentionInDays
    enablePurgeProtection: enablePurgeProtection ? true : null
    // RBAC only. A private endpoint (privatelink.bicep) is always created; publicNetworkAccess Disabled
    // (InfoSec LLD §9) then closes the public one. scripts/azure_lockdown.py does it after the Cloud Shell steps
    // that read the vault from outside, restarts the app and proves its secret references still resolve.
    // The template writes secrets through ARM, which a closed vault still accepts (enabledForTemplateDeployment).
    publicNetworkAccess: publicNetworkAccess
    enabledForTemplateDeployment: true
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: publicNetworkAccess == 'Enabled' ? 'Allow' : 'Deny'
    }
  }
}

resource databaseUrlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (writeSecrets) {
  parent: vault
  name: 'inv-studio-database-url'
  properties: {
    value: databaseUrl
  }
}

resource vaultKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (writeSecrets) {
  parent: vault
  name: 'inv-studio-vault-key'
  properties: {
    value: vaultKey
  }
}

resource foundrySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (writeSecrets) {
  parent: vault
  name: 'inv-studio-foundry-key'
  properties: {
    value: empty(foundryApiKey) ? unset : foundryApiKey
  }
}

// Key Vault Secrets User: the app identity reads secret values, nothing else.
resource reader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, principalId, '4633458b-17de-408a-b874-0445c86b69e6')
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

resource audit 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'audit-to-log-analytics'
  scope: vault
  properties: {
    workspaceId: logWorkspaceId
    logs: [
      {
        categoryGroup: 'audit'
        enabled: true
      }
    ]
  }
}

// Version-less URIs, so rotating a secret and restarting the revision is enough.
output databaseUrlSecretUri string = '${vault.properties.vaultUri}secrets/inv-studio-database-url'
output vaultKeySecretUri string = '${vault.properties.vaultUri}secrets/inv-studio-vault-key'
output foundrySecretUri string = '${vault.properties.vaultUri}secrets/inv-studio-foundry-key'
output vaultName string = vault.name
output vaultUri string = vault.properties.vaultUri
output vaultId string = vault.id
