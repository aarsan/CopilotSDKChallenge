// ──────────────────────────────────────────────────────────────
// InfraForge Approved Blueprint: 3-Tier Web Application
// Category: Blueprint | Format: Bicep
//
// Composes: App Service + SQL Database + Key Vault + Log Analytics
// All wired together with managed identity and diagnostics.
// ──────────────────────────────────────────────────────────────

targetScope = 'resourceGroup'

@description('Project name used for resource naming (2-12 chars)')
@minLength(2)
@maxLength(12)
param projectName string

@description('Target environment')
@allowed(['dev', 'staging', 'prod'])
param environment string

@description('Azure region for all resources')
param location string = resourceGroup().location

@secure()
@description('SQL Server administrator password')
param sqlAdminPassword string

// ── Shared Variables ────────────────────────────────────────
var resourcePrefix = '${projectName}-${environment}'
var tags = {
  environment: environment
  project: projectName
  managedBy: 'InfraForge'
  blueprint: '3-tier-web'
  deployedAt: utcNow('yyyy-MM-dd')
}

// ══════════════════════════════════════════════════════════════
// LAYER 1: Foundation — Monitoring
// ══════════════════════════════════════════════════════════════

module logAnalytics 'log-analytics.bicep' = {
  name: 'deploy-log-analytics'
  params: {
    projectName: projectName
    environment: environment
    location: location
  }
}

// ══════════════════════════════════════════════════════════════
// LAYER 2: Security — Key Vault
// ══════════════════════════════════════════════════════════════

module keyVault 'key-vault.bicep' = {
  name: 'deploy-key-vault'
  params: {
    projectName: projectName
    environment: environment
    location: location
    logAnalyticsWorkspaceId: logAnalytics.outputs.workspaceId
  }
}

// ══════════════════════════════════════════════════════════════
// LAYER 3: Data — SQL Database
// ══════════════════════════════════════════════════════════════

module sqlDatabase 'sql-database.bicep' = {
  name: 'deploy-sql-database'
  params: {
    projectName: projectName
    environment: environment
    location: location
    sqlAdminPassword: sqlAdminPassword
    logAnalyticsWorkspaceId: logAnalytics.outputs.workspaceId
  }
}

// ══════════════════════════════════════════════════════════════
// LAYER 4: Compute — App Service
// ══════════════════════════════════════════════════════════════

module appService 'app-service-linux.bicep' = {
  name: 'deploy-app-service'
  params: {
    projectName: projectName
    environment: environment
    location: location
    logAnalyticsWorkspaceId: logAnalytics.outputs.workspaceId
  }
}

// ══════════════════════════════════════════════════════════════
// RBAC: Grant App Service access to Key Vault
// ══════════════════════════════════════════════════════════════

// Key Vault Secrets User role
var keyVaultSecretsUserRole = '4633458b-17de-408a-b874-0445c86b69e6'

resource kvRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.outputs.keyVaultName, appService.outputs.principalId, keyVaultSecretsUserRole)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRole)
    principalId: appService.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

// ── Outputs ─────────────────────────────────────────────────
output appServiceUrl string = appService.outputs.appServiceUrl
output sqlServerFqdn string = sqlDatabase.outputs.sqlServerFqdn
output keyVaultUri string = keyVault.outputs.keyVaultUri
output logAnalyticsWorkspace string = logAnalytics.outputs.workspaceName
