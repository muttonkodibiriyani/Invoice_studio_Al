// PostgreSQL Flexible Server 16, the counterpart of Cloud SQL `inv-studio-db`.
// No public endpoint: the server is reached only through a private endpoint in the app's network,
// and its usual name (<server>.postgres.database.azure.com) resolves to that private address there.
param serverName string
param location string
param tags object = {}
param administratorLogin string
@secure()
param administratorPassword string
param databaseName string = 'invstudio'
param skuName string = 'Standard_B2s'
param skuTier string = 'Burstable'
param storageSizeGB int = 32
param backupRetentionDays int = 14
param postgresVersion string = '16'
param endpointsSubnetId string
param privateDnsZoneId string

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: serverName
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuTier
  }
  properties: {
    version: postgresVersion
    administratorLogin: administratorLogin
    administratorLoginPassword: administratorPassword
    createMode: 'Default'
    storage: {
      storageSizeGB: storageSizeGB
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: backupRetentionDays
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Disabled'
    }
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

resource endpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${serverName}-pe'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: endpointsSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: '${serverName}-pe'
        properties: {
          privateLinkServiceId: server.id
          groupIds: [
            'postgresqlServer'
          ]
        }
      }
    ]
  }
}

resource endpointDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'postgres'
        properties: {
          privateDnsZoneId: privateDnsZoneId
        }
      }
    ]
  }
}

output fullyQualifiedDomainName string = server.properties.fullyQualifiedDomainName
output postgresDatabaseName string = database.name
output postgresServerName string = server.name
