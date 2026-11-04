// Evidence storage: one private container holding uploads/, templates/ and learned/, the same
// layout the GCS bucket uses. Blob versioning and soft delete stand in for the bucket's
// versioning and 7-day soft delete.
param accountName string
param location string
param tags object = {}
param containerName string = 'evidence'
param softDeleteDays int = 7
@description('Days a previous blob version is kept, counted from when that version was written, before lifecycle management deletes it (security pack N1). It then sits in soft delete for softDeleteDays more. docs/AZURE.md §2 has the end-to-end figure.')
@minValue(1)
param previousVersionDays int = 7
param principalId string
@description('Log Analytics workspace that receives the blob audit log: every read, write and delete.')
param logWorkspaceId string
@description('Enabled until the private endpoint is proven, then Disabled (scripts/azure_lockdown.py): Blob is then reachable only through its private endpoint in the spoke.')
@allowed([
  'Enabled'
  'Disabled'
])
param publicNetworkAccess string = 'Enabled'

resource account 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: accountName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    // No shared keys, no anonymous access, Entra ID and RBAC only. A private endpoint (privatelink.bicep) is
    // always created; publicNetworkAccess Disabled (InfoSec LLD §9, scripts/azure_lockdown.py) then closes the
    // public one. It stays Enabled until the lockdown, because Cloud Shell steps before it run from outside.
    publicNetworkAccess: publicNetworkAccess
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: publicNetworkAccess == 'Enabled' ? 'Allow' : 'Deny'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: account
  name: 'default'
  properties: {
    isVersioningEnabled: true
    deleteRetentionPolicy: {
      enabled: true
      days: softDeleteDays
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: softDeleteDays
    }
  }
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}

// Without this rule every previous version is kept for ever: an invoice deleted in the app, or a template
// overwritten, would stay recoverable and billed indefinitely (security pack N1). The rule acts on previous
// versions only, never on the current version of a live file, and only on block blobs in the app's container.
resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: account
  name: 'default'
  dependsOn: [
    blobService
  ]
  properties: {
    policy: {
      rules: [
        {
          name: 'expirePreviousVersions'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: [
                'blockBlob'
              ]
              // The trailing slash keeps the rule to this container, not every container whose name starts the same.
              prefixMatch: [
                '${containerName}/'
              ]
            }
            actions: {
              version: {
                delete: {
                  daysAfterCreationGreaterThan: previousVersionDays
                }
              }
            }
          }
        }
      ]
    }
  }
}

// Storage Blob Data Contributor: the app reads, writes and deletes its own evidence blobs.
resource blobWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, principalId, 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

// Who read, wrote or deleted which evidence blob. With the endpoint public (security review L4,
// deferred), this log is how a misused identity would show.
resource audit 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'audit-to-log-analytics'
  scope: blobService
  properties: {
    workspaceId: logWorkspaceId
    logs: [
      {
        category: 'StorageRead'
        enabled: true
      }
      {
        category: 'StorageWrite'
        enabled: true
      }
      {
        category: 'StorageDelete'
        enabled: true
      }
    ]
  }
}

output storageAccountName string = account.name
output blobContainerName string = container.name
output blobEndpoint string = account.properties.primaryEndpoints.blob
output storageAccountId string = account.id
