// The spoke network in the Tech subscription. It keeps the database off the internet and, with
// internalEnvironment, the app too. Two subnets, each behind its own network security group:
//   apps       the Container Apps environment (a workload-profiles environment needs a /27, delegated)
//   endpoints  private endpoints; today only the PostgreSQL server's
// Layout of the IPAM /24: apps x.0/27, endpoints x.32/27, x.64 to x.255 left free.
// The endpoints subnet is a /27, not a /28, so the Key Vault, Blob, registry and Azure Monitor private
// endpoints of the target round fit without resizing a subnet that already holds an endpoint.
// The private DNS zone makes the server's public name resolve to its private address inside the
// network, so the connection string does not change.
//
// Outbound (InfoSec LLD §3): with restrictEgress the apps NSG allows only the Azure services the app and the
// Container Apps platform need, from Microsoft's list for a workload-profiles environment ("Securing a virtual
// network in Azure Container Apps"), and denies the internet. With egressFirewallIp a route table also sends
// 0.0.0.0/0 to the hub firewall, which then holds the finer allow-list (access request H3, H4).
// Private endpoints for the vault and Blob are in privatelink.bicep; they sit in the endpoints subnet, which
// this file's endpoints NSG already opens to 443 from the apps subnet.
param name string
param location string
param tags object = {}
@description('Address space of the spoke: the /24 the platform\'s IPAM assigned (access request H1, <SPOKE_VNET_CIDR>). It must not overlap the hub or any network peered with it.')
param addressPrefix string
@description('Resource id of the hub virtual network (access request H2, <HUB_VNET_ID>). Empty: no peering is created here, either because there is no hub yet or because the platform creates both directions.')
param hubVnetId string = ''
@description('DNS servers for the spoke: the hub DNS server or Private DNS Resolver inbound IP (access request H2, <HUB_DNS_IP>). Empty: Azure-provided DNS.')
param dnsServers array = []
@description('True when the Container Apps environment is internal. Then HTTP(S) into the apps subnet is accepted from the virtual network (the hub and its entry point, over the peering) only; false accepts it from the internet, as the public first deploy needs.')
param internalEnvironment bool = false
@description('True (default) limits outbound traffic from the apps subnet to the Azure services the app needs (Entra ID, registry, Storage, Key Vault, Azure Monitor, the private endpoints) and denies the internet. False leaves Azure\'s default open outbound.')
param restrictEgress bool = true
@description('Private IP of the hub firewall (access request H3). Set: a route table sends all outbound traffic of the apps subnet through it. Empty: no route table.')
param egressFirewallIp string = ''
@description('Where AI calls go when AI is switched on (AZURE.md §7): a service tag such as CognitiveServicesFrontend, or the CIDR of the Foundry or Citadel private endpoint. Empty: no AI egress rule, so with restrictEgress every AI provider is unreachable, which is what release 1 (AI off) wants.')
param aiEgressDestination string = ''

var appsPrefix = cidrSubnet(addressPrefix, 27, 0)
var endpointsPrefix = cidrSubnet(addressPrefix, 27, 1)
var clientSource = internalEnvironment ? 'VirtualNetwork' : 'Internet'

// Outbound from the apps subnet: one allow rule per Azure service the app or the platform calls on 443, then a
// deny of the internet. Traffic inside the virtual network and over the hub peering keeps Azure's default
// AllowVnetOutBound; Azure DNS (168.63.129.16) is not subject to NSGs. Service tags are not region-scoped here:
// the region suffix (Storage.UAENorth) depends on spelling that the location parameter does not carry.
var egressServices = [
  { name: 'entra-id', tag: 'AzureActiveDirectory', why: 'Entra ID signing keys and managed identity tokens' }
  { name: 'mcr', tag: 'MicrosoftContainerRegistry', why: 'Platform system containers' }
  { name: 'mcr-frontdoor', tag: 'AzureFrontDoor.FirstParty', why: 'Dependency of MicrosoftContainerRegistry' }
  { name: 'acr', tag: 'AzureContainerRegistry', why: 'Image pull from our registry' }
  { name: 'storage', tag: 'Storage', why: 'Registry image layers; Blob while it is still public' }
  { name: 'key-vault', tag: 'AzureKeyVault', why: 'Secret references while the vault is still public' }
  { name: 'azure-monitor', tag: 'AzureMonitor', why: 'Logs and metrics' }
]
var aiEgress = empty(aiEgressDestination) ? [] : [
  { name: 'ai', tag: aiEgressDestination, why: 'Azure AI Foundry or the Citadel gateway (AZURE.md §7)' }
]
var egressAllowed = concat(egressServices, aiEgress)
var egressRules = concat([
  {
    name: 'allow-out-within-apps-subnet'
    properties: {
      priority: 100
      direction: 'Outbound'
      access: 'Allow'
      protocol: '*'
      sourceAddressPrefix: appsPrefix
      sourcePortRange: '*'
      destinationAddressPrefix: appsPrefix
      destinationPortRange: '*'
    }
  }
  {
    // PostgreSQL, and the vault and Blob private endpoints.
    name: 'allow-out-private-endpoints'
    properties: {
      priority: 110
      direction: 'Outbound'
      access: 'Allow'
      protocol: 'Tcp'
      sourceAddressPrefix: appsPrefix
      sourcePortRange: '*'
      destinationAddressPrefix: endpointsPrefix
      destinationPortRanges: [
        '443'
        '5432'
      ]
    }
  }
], map(range(0, length(egressAllowed)), i => {
  name: 'allow-out-${egressAllowed[i].name}'
  properties: {
    description: egressAllowed[i].why
    priority: 200 + i * 10
    direction: 'Outbound'
    access: 'Allow'
    protocol: 'Tcp'
    sourceAddressPrefix: appsPrefix
    sourcePortRange: '*'
    destinationAddressPrefix: egressAllowed[i].tag
    destinationPortRange: '443'
  }
}), [
  {
    // Every other provider and host: an admin cannot point the app at a non-Azure AI service, and a file
    // reader that is tricked into a network call has nowhere to go.
    name: 'deny-out-internet'
    properties: {
      priority: 4000
      direction: 'Outbound'
      access: 'Deny'
      protocol: '*'
      sourceAddressPrefix: '*'
      sourcePortRange: '*'
      destinationAddressPrefix: 'Internet'
      destinationPortRange: '*'
    }
  }
])

// All outbound traffic of the apps subnet through the hub firewall (access request H3). The firewall must allow
// what egressRules allow, or the environment cannot pull its system images.
resource egressRoutes 'Microsoft.Network/routeTables@2024-05-01' = if (!empty(egressFirewallIp)) {
  name: '${name}-apps-routes'
  location: location
  tags: tags
  properties: {
    disableBgpRoutePropagation: true
    routes: [
      {
        name: 'default-to-hub-firewall'
        properties: {
          addressPrefix: '0.0.0.0/0'
          nextHopType: 'VirtualAppliance'
          nextHopIpAddress: egressFirewallIp
        }
      }
    ]
  }
}

// Inbound to the Container Apps environment, from Microsoft's list for a workload-profiles environment
// ("Securing a virtual network in Azure Container Apps with network security groups"). Everything else
// inbound is refused by the last rule, which also overrides Azure's default AllowVnetInBound (65000).
// Outbound is egressRules above (restrictEgress), and the hub firewall when egressFirewallIp is set.
resource appsNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${name}-apps-nsg'
  location: location
  tags: tags
  properties: {
    securityRules: concat([
      {
        // Users. 80 only redirects (the app sets allowInsecure false); 31080 and 31443 are the
        // environment's edge proxy behind its load balancer. Internal: from the virtual network, which
        // includes the peered hub and its entry point. External: from the internet.
        name: 'allow-clients-http-https'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: clientSource
          sourcePortRange: '*'
          destinationAddressPrefix: appsPrefix
          destinationPortRanges: [
            '80'
            '443'
            '31080'
            '31443'
          ]
        }
      }
      {
        // The environment's load balancer probes its backend pools on the node ports.
        name: 'allow-azure-load-balancer-probes'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'AzureLoadBalancer'
          sourcePortRange: '*'
          destinationAddressPrefix: appsPrefix
          destinationPortRange: '30000-32767'
        }
      }
      {
        // Inside the environment: the Envoy sidecars and the platform's own components talk to each other.
        name: 'allow-within-apps-subnet'
        properties: {
          priority: 120
          direction: 'Inbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: appsPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: appsPrefix
          destinationPortRange: '*'
        }
      }
      {
        name: 'deny-all-other-inbound'
        properties: {
          priority: 4096
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ], restrictEgress ? egressRules : [])
  }
}

// Inbound to the private endpoints: only the app may reach them. A private endpoint obeys these rules
// because the subnet sets privateEndpointNetworkPolicies (below).
resource endpointsNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${name}-endpoints-nsg'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        // The PostgreSQL server's private endpoint.
        name: 'allow-apps-postgres'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: appsPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: endpointsPrefix
          destinationPortRange: '5432'
        }
      }
      {
        // Key Vault, Blob, registry and Azure Monitor private endpoints (private-link task). Nothing
        // listens here until they exist.
        name: 'allow-apps-https'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: appsPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: endpointsPrefix
          destinationPortRange: '443'
        }
      }
      {
        name: 'deny-all-other-inbound'
        properties: {
          priority: 4096
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

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
    dhcpOptions: empty(dnsServers) ? null : {
      dnsServers: dnsServers
    }
    subnets: [
      {
        name: 'apps'
        properties: {
          addressPrefix: appsPrefix
          networkSecurityGroup: {
            id: appsNsg.id
          }
          routeTable: empty(egressFirewallIp) ? null : {
            id: egressRoutes.id
          }
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
          addressPrefix: endpointsPrefix
          networkSecurityGroup: {
            id: endpointsNsg.id
          }
          // NSG rules apply to private endpoints only with this set. Route tables are left out: if the
          // egress task needs them on this subnet too, it changes this to 'Enabled'.
          privateEndpointNetworkPolicies: 'NetworkSecurityGroupEnabled'
        }
      }
    ]
  }
}

// The spoke side of the hub peering. The hub side is the platform's (access request H2). Forwarded
// traffic is allowed so replies that the hub firewall forwards come back in. No gateway transit: the
// platform says if the spoke should use the hub's gateway, and that is a later change.
resource hubPeering 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2024-05-01' = if (!empty(hubVnetId)) {
  parent: vnet
  name: '${name}-to-hub'
  properties: {
    remoteVirtualNetwork: {
      id: hubVnetId
    }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: true
    allowGatewayTransit: false
    useRemoteGateways: false
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

output vnetId string = vnet.id
output vnetName string = vnet.name
output appsSubnetId string = vnet.properties.subnets[0].id
output endpointsSubnetId string = vnet.properties.subnets[1].id
output appsSubnetPrefix string = appsPrefix
output endpointsSubnetPrefix string = endpointsPrefix
output hubPeeringName string = empty(hubVnetId) ? '' : hubPeering.name
output postgresDnsZoneId string = postgresZone.id
output egressRestricted bool = restrictEgress
output egressFirewall string = egressFirewallIp
