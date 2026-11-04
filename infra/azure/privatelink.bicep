// Private endpoints for Key Vault and Blob Storage in the endpoints subnet (InfoSec LLD §9: "Are Key Vault and
// Blob private?"). The app then reaches both over the spoke, and the lockdown (scripts/azure_lockdown.py) can switch
// their public endpoints off.
//
// DNS: each service needs its privatelink zone. A zone the platform already runs in the hub is passed in
// privateDnsZoneIds (deploy kit PDNS_ZONE_IDS, {"privatelink.vaultcore.azure.net": "<zone id>", ...}) and the
// endpoint registers its record there. A zone not passed is created here and linked to the spoke, which is right
// when the spoke uses Azure DNS. With hub DNS servers (HUB_DNS_IP) and no hub zone, the hub resolver cannot see a
// zone created here: pass the hub zones, or leave the public endpoints on.
param namePrefix string
param location string
param tags object = {}
param vnetId string
param endpointsSubnetId string
param keyVaultId string
param storageAccountId string
@description('Existing private DNS zone ids by zone name (deploy kit PDNS_ZONE_IDS). A zone left out is created here.')
param privateDnsZoneIds object = {}

var vaultZoneName = 'privatelink.vaultcore.azure.net'
var blobZoneName = 'privatelink.blob.${environment().suffixes.storage}'
var givenVaultZone = privateDnsZoneIds[?vaultZoneName] ?? ''
var givenBlobZone = privateDnsZoneIds[?blobZoneName] ?? ''

resource vaultZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (empty(givenVaultZone)) {
  name: vaultZoneName
  location: 'global'
  tags: tags
}

resource vaultZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (empty(givenVaultZone)) {
  parent: vaultZone
  name: '${namePrefix}-vault-link'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnetId
    }
  }
}

resource blobZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (empty(givenBlobZone)) {
  name: blobZoneName
  location: 'global'
  tags: tags
}

resource blobZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (empty(givenBlobZone)) {
  parent: blobZone
  name: '${namePrefix}-blob-link'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnetId
    }
  }
}

var vaultZoneId = empty(givenVaultZone) ? resourceId('Microsoft.Network/privateDnsZones', vaultZoneName) : givenVaultZone
var blobZoneId = empty(givenBlobZone) ? resourceId('Microsoft.Network/privateDnsZones', blobZoneName) : givenBlobZone

resource vaultEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${namePrefix}-vault-pe'
  location: location
  tags: tags
  dependsOn: [
    vaultZoneLink
  ]
  properties: {
    subnet: {
      id: endpointsSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'vault'
        properties: {
          privateLinkServiceId: keyVaultId
          groupIds: [
            'vault'
          ]
        }
      }
    ]
  }
}

resource vaultDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: vaultEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'vault'
        properties: {
          privateDnsZoneId: vaultZoneId
        }
      }
    ]
  }
}

resource blobEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${namePrefix}-blob-pe'
  location: location
  tags: tags
  dependsOn: [
    blobZoneLink
  ]
  properties: {
    subnet: {
      id: endpointsSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'blob'
        properties: {
          privateLinkServiceId: storageAccountId
          groupIds: [
            'blob'
          ]
        }
      }
    ]
  }
}

resource blobDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: blobEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'blob'
        properties: {
          privateDnsZoneId: blobZoneId
        }
      }
    ]
  }
}

output vaultEndpointName string = vaultEndpoint.name
output blobEndpointName string = blobEndpoint.name
output vaultDnsZoneId string = vaultZoneId
output blobDnsZoneId string = blobZoneId
