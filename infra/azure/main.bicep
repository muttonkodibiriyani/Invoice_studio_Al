// Invoice Studio on Azure, phase 1: the same container image as the Cloud Run deployment, with
// each Google service swapped for its Azure equivalent. Deploy into an existing resource group:
//
//   az deployment group create -g rg-inv-studio -f infra/azure/main.bicep -p @infra/azure/main.parameters.json
//
// Step by step, including what to do before and after, is in docs/AZURE.md.
targetScope = 'resourceGroup'

@description('Region for every resource, e.g. uaenorth or westeurope. Defaults to the resource group region.')
param location string = resourceGroup().location

@description('Prefix for every resource name. Lower-case letters and digits only: it is part of the globally unique storage account, registry and vault names. Nine characters is the limit because uniqueString adds thirteen and a Key Vault name may be no longer than twenty-four.')
@minLength(3)
@maxLength(9)
param namePrefix string = 'invstudio'

@description('PostgreSQL administrator login.')
param postgresAdminLogin string = 'invstudio'

@description('PostgreSQL administrator password. Never commit it: pass it on the command line or from a file outside the repository.')
@secure()
@minLength(12)
param postgresAdminPassword string

@description('The Fernet key that encrypts the credentials table. Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())". Changing it makes stored provider credentials unreadable.')
@secure()
param vaultKey string

@description('Azure AI Foundry api-key. Empty writes a placeholder to Key Vault.')
@secure()
param foundryApiKey string = ''

@description('Azure AI Foundry endpoint, e.g. https://my-resource.openai.azure.com. Empty leaves the provider unconfigured.')
param foundryEndpoint string = ''

@description('Azure AI Foundry deployment name, e.g. gpt-4.1.')
param foundryDeployment string = ''

@description('Image to run. Empty on the first deployment: the app starts on the Microsoft hello-world image and the deploy workflow replaces it with the real one.')
param containerImage string = ''

@description('Extra browser origins allowed to call the API (a custom domain). The container app URL is always allowed.')
param extraAllowedOrigins array = []

@description('Directory (tenant) id of the workforce Entra ID tenant that signs people in: a GUID, never common, organizations or consumers. Public, not a secret.')
@minLength(36)
@maxLength(36)
param entraTenantId string

@description('Application (client) id of the Invoice Studio app registration (single-page application that also exposes the API). Public, not a secret: the registration has no client secret.')
@minLength(36)
@maxLength(36)
param entraClientId string

@description('Display name of the Entra group granted the admin app role. Shown on the Users screen only; who is an administrator is decided in Entra.')
param entraAdminGroupName string = ''

@description('Display name of the Entra group granted the member app role. Shown on the Users screen only.')
param entraMemberGroupName string = ''

@description('Address space of the spoke network: the /24 the platform\'s IPAM assigned (access request H1, <SPOKE_VNET_CIDR>, deploy kit line SPOKE_VNET_CIDR). No default on purpose: a guessed range can overlap the hub or another spoke. Apps take its first /27, private endpoints the second.')
param networkAddressPrefix string

@description('Resource id of the hub virtual network to peer with (access request H2, deploy kit HUB_VNET_ID). Empty creates no peering: no hub yet, or the platform creates both directions. The hub side is always the platform\'s.')
param hubVnetId string = ''

@description('DNS servers for the spoke network: the hub DNS server or Private DNS Resolver inbound IP (access request H2, deploy kit HUB_DNS_IP). Empty uses Azure-provided DNS. The hub resolver must then answer for privatelink.postgres.database.azure.com, or the app cannot find the database.')
param dnsServers array = []

@description('True makes the Container Apps environment internal: the app has no public address and is reached from the virtual network only, through the platform\'s entry point in the hub (access request H8). False (the default) keeps the public address the first POC deploy was agreed with; the target redeploy sets true. An environment cannot switch between the two in place: changing it replaces the environment and the app.')
param internalEnvironment bool = false

@description('True on the first deployment. Set it to false when re-running after rotating a secret with az keyvault secret set, so the rotation is kept.')
param writeSecrets bool = true

@description('Blocks purging the vault and its secrets until the retention period has passed, so nobody, an administrator included, can destroy the vault key for good (security review L4). On for production. It can never be turned off again: set it to false only for a throwaway trial deployment.')
param enableKeyVaultPurgeProtection bool = true

@description('Days a deleted vault or secret can still be recovered: 7 to 90, fixed when the vault is created. Purge protection keeps the vault name taken for this long after a delete.')
@minValue(7)
@maxValue(90)
param keyVaultSoftDeleteRetentionDays int = 90

@description('Days a deleted or overwritten invoice file is kept as a previous blob version, counted from when that version was written. Soft delete then keeps it 7 more days. docs/AZURE.md §2 has the end-to-end figure.')
@minValue(1)
param blobPreviousVersionDays int = 7

param postgresSkuName string = 'Standard_B2s'
param postgresSkuTier string = 'Burstable'
param postgresStorageGB int = 32
param postgresBackupRetentionDays int = 14

@description('vCPU for the single replica. 4.0 is the most a Consumption workload profile gives.')
param cpuCores string = '4.0'
param memory string = '8Gi'
param logRetentionInDays int = 30
param registrySku string = 'Basic'

param tags object = {
  application: 'inv-studio'
  managedBy: 'infra/azure/main.bicep'
}

var suffix = uniqueString(resourceGroup().id)
var identityName = '${namePrefix}-app'
var registryName = toLower('${namePrefix}acr${suffix}')
var storageAccountName = toLower('${namePrefix}${suffix}')
var keyVaultName = toLower('${namePrefix}kv${suffix}')
var postgresServerName = toLower('${namePrefix}-db-${suffix}')
var logWorkspaceName = '${namePrefix}-logs'
var networkName = '${namePrefix}-vnet'
var environmentName = '${namePrefix}-env'
var containerAppName = '${namePrefix}-api'
var blobContainerName = 'evidence'
var postgresDatabaseName = 'invstudio'

module identity 'identity.bicep' = {
  name: 'identity'
  params: {
    name: identityName
    location: location
    tags: tags
  }
}

module network 'network.bicep' = {
  name: 'network'
  params: {
    name: networkName
    location: location
    tags: tags
    addressPrefix: networkAddressPrefix
    hubVnetId: hubVnetId
    dnsServers: dnsServers
    internalEnvironment: internalEnvironment
  }
}

module registry 'registry.bicep' = {
  name: 'registry'
  params: {
    name: registryName
    location: location
    tags: tags
    sku: registrySku
    principalId: identity.outputs.principalId
    logWorkspaceId: monitoring.outputs.workspaceId
  }
}

module storage 'storage.bicep' = {
  name: 'storage'
  params: {
    accountName: storageAccountName
    location: location
    tags: tags
    containerName: blobContainerName
    previousVersionDays: blobPreviousVersionDays
    principalId: identity.outputs.principalId
    logWorkspaceId: monitoring.outputs.workspaceId
  }
}

module postgres 'postgres.bicep' = {
  name: 'postgres'
  params: {
    serverName: postgresServerName
    location: location
    tags: tags
    administratorLogin: postgresAdminLogin
    administratorPassword: postgresAdminPassword
    databaseName: postgresDatabaseName
    skuName: postgresSkuName
    skuTier: postgresSkuTier
    storageSizeGB: postgresStorageGB
    backupRetentionDays: postgresBackupRetentionDays
    endpointsSubnetId: network.outputs.endpointsSubnetId
    privateDnsZoneId: network.outputs.postgresDnsZoneId
  }
}

module monitoring 'monitoring.bicep' = {
  name: 'monitoring'
  params: {
    name: logWorkspaceName
    location: location
    tags: tags
    retentionInDays: logRetentionInDays
  }
}

// Built here rather than output by the postgres module: a deployment output is kept in the
// deployment history, and this string holds the database password.
// verify-full checks that the server certificate chains to a trusted root and names the host
// (security review M1); `require` encrypts but accepts any certificate. The roots are the image's
// Debian bundle, which holds both Azure roots, DigiCert Global Root G2 and Microsoft RSA Root CA 2017.
// The path is given because `sslrootcert=system` does not find that bundle: psycopg-binary's own
// OpenSSL looks in the directory it was built in. The host is the server's usual name, which the
// private DNS zone resolves to the private endpoint, so it still matches the certificate.
// This URL logs in as the admin. The app is moved to its own role once, after the first start (docs/AZURE.md §5a).
var databaseTls = 'sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt'
var databaseUrl = 'postgresql://${postgresAdminLogin}:${uriComponent(postgresAdminPassword)}@${postgres.outputs.fullyQualifiedDomainName}:5432/${postgres.outputs.postgresDatabaseName}?${databaseTls}'

module keyvault 'keyvault.bicep' = {
  name: 'keyvault'
  params: {
    name: keyVaultName
    location: location
    tags: tags
    principalId: identity.outputs.principalId
    logWorkspaceId: monitoring.outputs.workspaceId
    writeSecrets: writeSecrets
    enablePurgeProtection: enableKeyVaultPurgeProtection
    softDeleteRetentionInDays: keyVaultSoftDeleteRetentionDays
    databaseUrl: databaseUrl
    vaultKey: vaultKey
    foundryApiKey: foundryApiKey
  }
}

module containerapp 'containerapp.bicep' = {
  name: 'containerapp'
  params: {
    environmentName: environmentName
    containerAppName: containerAppName
    location: location
    tags: tags
    logWorkspaceName: monitoring.outputs.workspaceName
    userAssignedIdentityId: identity.outputs.id
    userAssignedIdentityClientId: identity.outputs.clientId
    infrastructureSubnetId: network.outputs.appsSubnetId
    internalEnvironment: internalEnvironment
    registryLoginServer: registry.outputs.loginServer
    storageAccountName: storage.outputs.storageAccountName
    blobContainerName: storage.outputs.blobContainerName
    databaseUrlSecretUri: keyvault.outputs.databaseUrlSecretUri
    vaultKeySecretUri: keyvault.outputs.vaultKeySecretUri
    foundrySecretUri: keyvault.outputs.foundrySecretUri
    containerImage: containerImage
    cpuCores: cpuCores
    memory: memory
    extraAllowedOrigins: extraAllowedOrigins
    foundryEndpoint: foundryEndpoint
    foundryDeployment: foundryDeployment
    entraTenantId: entraTenantId
    entraClientId: entraClientId
    entraAdminGroupName: entraAdminGroupName
    entraMemberGroupName: entraMemberGroupName
  }
}

@description('The address of the app. Add <appUrl>/ as a single-page application redirect URI on the Entra app registration before signing in.')
output appUrl string = containerapp.outputs.appUrl
output appName string = containerapp.outputs.appName
output appEnvironmentName string = containerapp.outputs.appEnvironmentName
output appAllowedOrigins string = containerapp.outputs.appAllowedOrigins
output acrLoginServer string = registry.outputs.loginServer
output acrName string = registry.outputs.registryName
output blobAccountName string = storage.outputs.storageAccountName
output blobContainer string = storage.outputs.blobContainerName
output vaultName string = keyvault.outputs.vaultName
output databaseServerName string = postgres.outputs.postgresServerName
output databaseHost string = postgres.outputs.fullyQualifiedDomainName
output appIdentityClientId string = identity.outputs.clientId
output appIdentityPrincipalId string = identity.outputs.principalId
@description('With internalEnvironment, the private address every app name in the environment resolves to. The platform points a wildcard record for appEnvironmentDefaultDomain at it (access request H5) and its entry point at the app.')
output appEnvironmentStaticIp string = containerapp.outputs.appEnvironmentStaticIp
output appEnvironmentDefaultDomain string = containerapp.outputs.appEnvironmentDefaultDomain
output vnetName string = network.outputs.vnetName
output vnetId string = network.outputs.vnetId
output appsSubnetId string = network.outputs.appsSubnetId
output endpointsSubnetId string = network.outputs.endpointsSubnetId
@description('The spoke side of the hub peering, for az network vnet peering show. Empty when hubVnetId is empty.')
output hubPeeringName string = network.outputs.hubPeeringName
