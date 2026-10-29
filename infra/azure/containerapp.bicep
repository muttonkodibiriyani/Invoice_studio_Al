// Container Apps environment and the app itself: the counterpart of Cloud Run `inv-studio-api`.
// One replica, always on (min = max = 1), because invoice jobs run in an in-process queue and the
// OCR readers take minutes to warm up. Revisions stay addressable so a rollback is a traffic switch.
param environmentName string
param containerAppName string
param location string
param tags object = {}
param logWorkspaceName string
param userAssignedIdentityId string
param userAssignedIdentityClientId string
@description('The delegated subnet the environment runs in, so the app reaches the database privately.')
param infrastructureSubnetId string
param registryLoginServer string
param storageAccountName string
param blobContainerName string
param databaseUrlSecretUri string
param vaultKeySecretUri string
param foundrySecretUri string
@description('Image to run, e.g. <registry>/inv-studio:<git sha>. Empty runs the Microsoft hello-world image, so the environment can be created before the first application image exists.')
param containerImage string = ''
@description('vCPU for the single replica. 4.0 is the most a Consumption workload profile gives.')
param cpuCores string = '4.0'
param memory string = '8Gi'
@description('Extra browser origins allowed to call the API, on top of the container app URL.')
param extraAllowedOrigins array = []
param foundryEndpoint string = ''
param foundryDeployment string = ''
@description('Entra ID tenant (GUID). The app refuses to start in cloud mode without it.')
param entraTenantId string
@description('Client id of the Entra app registration: the audience of the access tokens the API accepts.')
param entraClientId string
@description('Group display names for the Users screen. Display only.')
param entraAdminGroupName string = ''
param entraMemberGroupName string = ''

var isPlaceholder = empty(containerImage)
var image = isPlaceholder ? 'mcr.microsoft.com/k8se/quickstart:latest' : containerImage
var targetPort = isPlaceholder ? 80 : 8080

resource logWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logWorkspaceName
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    // A workload-profiles environment in our own subnet. External: the app keeps its public HTTPS
    // address; only its outbound traffic and the private endpoints share the network.
    vnetConfiguration: {
      infrastructureSubnetId: infrastructureSubnetId
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logWorkspace.properties.customerId
        sharedKey: logWorkspace.listKeys().primarySharedKey
      }
    }
    zoneRedundant: false
  }
}

// The app has to know its own address: the API refuses writes from origins it does not know, and
// the create-password and reset links point at it. The hostname is fixed once the environment exists.
var appHost = '${containerAppName}.${environment.properties.defaultDomain}'
var predictedUrl = 'https://${appHost}'
var originList = join(union([predictedUrl], extraAllowedOrigins), ',')

// No INV_STUDIO_RECOVER_JOBS here: a revision this template creates takes the traffic, so it must take over the
// invoices of an instance that stopped. Only a no-traffic candidate sets it to 0 (AZURE.md §5, §8).
var baseEnv = [
  {
    name: 'INV_STUDIO_CLOUD'
    value: '1'
  }
  {
    name: 'INV_STUDIO_DATA'
    value: '/tmp/inv-studio-data'
  }
  {
    name: 'INV_STUDIO_STORAGE_BACKEND'
    value: 'azure'
  }
  {
    name: 'INV_STUDIO_AZURE_STORAGE_ACCOUNT'
    value: storageAccountName
  }
  {
    name: 'INV_STUDIO_BUCKET'
    value: blobContainerName
  }
  {
    name: 'INV_STUDIO_ALLOWED_ORIGINS'
    value: originList
  }
  {
    // The app refuses Host headers it does not know. A revision's own address
    // (<app>--<suffix>.<environment domain>) is how a revision is tested before it gets traffic (AZURE.md §5);
    // only apps in this environment share that domain.
    name: 'INV_STUDIO_ALLOWED_HOSTS'
    value: '*.${environment.properties.defaultDomain}'
  }
  {
    // The image sets this too. Readers use only the models baked into the image and never ask huggingface.co
    // for a newer one (N6).
    name: 'HF_HUB_OFFLINE'
    value: '1'
  }
  {
    // Public values, not credentials: the API checks Entra access tokens against the tenant's public
    // signing keys (issuer, tenant and audience from these two), and the browser signs in with them.
    name: 'INV_STUDIO_ENTRA_TENANT_ID'
    value: entraTenantId
  }
  {
    name: 'INV_STUDIO_ENTRA_CLIENT_ID'
    value: entraClientId
  }
  {
    name: 'INV_STUDIO_ENTRA_ADMIN_GROUP_NAME'
    value: entraAdminGroupName
  }
  {
    name: 'INV_STUDIO_ENTRA_MEMBER_GROUP_NAME'
    value: entraMemberGroupName
  }
  {
    // DefaultAzureCredential needs the client id to pick a user-assigned identity.
    name: 'AZURE_CLIENT_ID'
    value: userAssignedIdentityClientId
  }
  {
    name: 'INV_STUDIO_DATABASE_URL'
    secretRef: 'database-url'
  }
  {
    name: 'INV_STUDIO_VAULT_KEY'
    secretRef: 'vault-key'
  }
  {
    name: 'INV_STUDIO_AZURE_API_KEY'
    secretRef: 'foundry-key'
  }
]

var endpointEnv = empty(foundryEndpoint) ? [] : [{ name: 'INV_STUDIO_AZURE_ENDPOINT', value: foundryEndpoint }]
var deploymentEnv = empty(foundryDeployment) ? [] : [{ name: 'INV_STUDIO_AZURE_DEPLOYMENT', value: foundryDeployment }]
var appEnv = concat(baseEnv, endpointEnv, deploymentEnv)

// The image is about 5.6 GB and warms the OCR readers, so the first start of a revision is slow.
// No liveness probe: a long invoice run must not be restarted under it.
var startupProbe = {
  type: 'Startup'
  httpGet: {
    path: '/health'
    port: 8080
    scheme: 'HTTP'
    // Without it the probe's Host is the replica's IP, which the app answers with 400, and no revision
    // ever becomes healthy.
    httpHeaders: [
      {
        name: 'Host'
        value: appHost
      }
    ]
  }
  initialDelaySeconds: 20
  periodSeconds: 10
  timeoutSeconds: 5
  failureThreshold: 40
}
var probes = isPlaceholder ? [] : [startupProbe]

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityId}': {}
    }
  }
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      // Multiple-revision mode keeps older revisions serving-capable, so a rollback is
      // `az containerapp ingress traffic set --revision-weight <old revision>=100`.
      activeRevisionsMode: 'Multiple'
      maxInactiveRevisions: 10
      ingress: {
        external: true
        targetPort: targetPort
        transport: 'auto'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: registryLoginServer
          identity: userAssignedIdentityId
        }
      ]
      secrets: [
        {
          name: 'database-url'
          keyVaultUrl: databaseUrlSecretUri
          identity: userAssignedIdentityId
        }
        {
          name: 'vault-key'
          keyVaultUrl: vaultKeySecretUri
          identity: userAssignedIdentityId
        }
        {
          name: 'foundry-key'
          keyVaultUrl: foundrySecretUri
          identity: userAssignedIdentityId
        }
      ]
    }
    template: {
      terminationGracePeriodSeconds: 120
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
      containers: [
        {
          name: 'api'
          image: image
          resources: {
            cpu: json(cpuCores)
            memory: memory
          }
          env: appEnv
          probes: probes
        }
      ]
    }
  }
}

output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output appName string = app.name
output appEnvironmentName string = environment.name
output appAllowedOrigins string = originList
