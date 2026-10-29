// The private network that keeps the database off the internet. Two subnets:
//   apps       the Container Apps environment (a workload-profiles environment needs a /27, delegated)
//   endpoints  private endpoints; today only the PostgreSQL server's
// The private DNS zone makes the server's public name resolve to its private address inside the
// network, so the connection string does not change.
param name string
param location string
param tags object = {}
@description('Address space for the network. A /24 is plenty: the subnets take a /27 and a /28 of it. Pick a range that does not overlap any network this one may be peered with later.')
param addressPrefix string = '10.40.0.0/24'

var appsSubnetPrefix = cidrSubnet(addressPrefix, 27, 0)
var endpointsSubnetPrefix = cidrSubnet(addressPrefix, 28, 2)

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        addressPrefix
      ]
    }
    subnets: [
      {
        name: 'apps'
        properties: {
          addressPrefix: appsSubnetPrefix
          delegations: [
            {
              name: 'container-apps'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'endpoints'
        properties: {
          addressPrefix: endpointsSubnetPrefix
        }
      }
    ]
  }
}

resource postgresZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.postgres.database.azure.com'
  location: 'global'
  tags: tags
}

resource postgresZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: postgresZone
  name: '${name}-link'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

output appsSubnetId string = vnet.properties.subnets[0].id
output endpointsSubnetId string = vnet.properties.subnets[1].id
output postgresDnsZoneId string = postgresZone.id
