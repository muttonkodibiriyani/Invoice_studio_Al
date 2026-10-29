// The one managed identity the app runs as: it pulls the image from ACR, reads the Key Vault
// secrets and reads and writes the evidence blobs. User-assigned (not system-assigned) so the
// role assignments can be made in the same deployment that creates the container app.
param name string
param location string
param tags object = {}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: name
  location: location
  tags: tags
}

output id string = identity.id
output principalId string = identity.properties.principalId
output clientId string = identity.properties.clientId
