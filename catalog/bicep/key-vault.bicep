// ──────────────────────────────────────────────────────────────
// InfraForge Approved Template: Azure Key Vault
// Category: Security | Format: Bicep
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

@description('Log Analytics workspace ID for diagnostic logging')
param logAnalyticsWorkspaceId string = ''

// ── Naming & Tags ───────────────────────────────────────────
var resourcePrefix = '${projectName}-${environment}'
var tags = {
  environment: environment
  project: projectName
  managedBy: 'InfraForge'
}

// ── Key Vault ───────────────────────────────────────────────
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${resourcePrefix}-kv'
  location: location
  tags: tags
  properties: {
    sku: { family: 'A'; name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: environment == 'prod' ? true : null
    networkAcls: {
      defaultAction: environment == 'prod' ? 'Deny' : 'Allow'
      bypass: 'AzureServices'
    }
  }
}

// ── Diagnostic Logging ──────────────────────────────────────
resource kvDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (!empty(logAnalyticsWorkspaceId)) {
  name: '${keyVault.name}-diag'
  scope: keyVault
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      { category: 'AuditEvent'; enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics'; enabled: true }
    ]
  }
}

// ── Outputs ─────────────────────────────────────────────────
output keyVaultUri string = keyVault.properties.vaultUri
output keyVaultName string = keyVault.name
output keyVaultId string = keyVault.id
