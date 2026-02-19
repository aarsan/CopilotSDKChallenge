// ──────────────────────────────────────────────────────────────
// InfraForge Approved Template: Storage Account
// Category: Storage | Format: Bicep
// ──────────────────────────────────────────────────────────────

@description('Project name used for resource naming')
@minLength(2)
@maxLength(12)
param projectName string

@description('Target environment')
@allowed(['dev', 'staging', 'prod'])
param environment string

@description('Azure region')
param location string = resourceGroup().location

// ── Naming & Tags ───────────────────────────────────────────
// Storage accounts have strict naming: lowercase, no hyphens, 3-24 chars
var storageName = toLower(replace('${projectName}${environment}st', '-', ''))
var tags = {
  environment: environment
  project: projectName
  managedBy: 'InfraForge'
}

// ── Redundancy per environment ──────────────────────────────
var redundancyMap = {
  dev: 'Standard_LRS'
  staging: 'Standard_ZRS'
  prod: 'Standard_GRS'
}

// ── Storage Account ─────────────────────────────────────────
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: redundancyMap[environment] }
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    networkAcls: {
      defaultAction: environment == 'prod' ? 'Deny' : 'Allow'
      bypass: 'AzureServices'
    }
  }
}

// ── Outputs ─────────────────────────────────────────────────
output storageAccountName string = storageAccount.name
output storageAccountId string = storageAccount.id
output primaryBlobEndpoint string = storageAccount.properties.primaryEndpoints.blob
