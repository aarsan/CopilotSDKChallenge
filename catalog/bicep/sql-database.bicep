// ──────────────────────────────────────────────────────────────
// InfraForge Approved Template: Azure SQL Database
// Category: Database | Format: Bicep
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

@secure()
@description('SQL Server administrator password')
param sqlAdminPassword string

@description('SQL Server administrator login name')
param sqlAdminLogin string = 'sqladmin'

@description('Log Analytics workspace ID for diagnostic logging')
param logAnalyticsWorkspaceId string = ''

// ── Naming & Tags ───────────────────────────────────────────
var resourcePrefix = '${projectName}-${environment}'
var tags = {
  environment: environment
  project: projectName
  managedBy: 'InfraForge'
}

// ── SKU mapping per environment ─────────────────────────────
var skuMap = {
  dev: { name: 'Basic', tier: 'Basic' }
  staging: { name: 'S0', tier: 'Standard' }
  prod: { name: 'S1', tier: 'Standard' }
}

// ── SQL Server ──────────────────────────────────────────────
resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: '${resourcePrefix}-sql'
  location: location
  tags: tags
  properties: {
    administratorLogin: sqlAdminLogin
    administratorLoginPassword: sqlAdminPassword
    minimalTlsVersion: '1.2'
    publicNetworkAccess: environment == 'prod' ? 'Disabled' : 'Enabled'
  }
}

// ── Database ────────────────────────────────────────────────
resource sqlDb 'Microsoft.Sql/servers/databases@2023-08-01-preview' = {
  parent: sqlServer
  name: '${projectName}db'
  location: location
  tags: tags
  sku: skuMap[environment]
  properties: {
    collation: 'SQL_Latin1_General_CP1_CI_AS'
    maxSizeBytes: environment == 'prod' ? 268435456000 : 2147483648 // 250GB prod, 2GB dev
    zoneRedundant: environment == 'prod'
  }
}

// ── Allow Azure services (non-prod only) ────────────────────
resource firewallRule 'Microsoft.Sql/servers/firewallRules@2023-08-01-preview' = if (environment != 'prod') {
  parent: sqlServer
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// ── Diagnostic Logging ──────────────────────────────────────
resource sqlDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (!empty(logAnalyticsWorkspaceId)) {
  name: '${sqlDb.name}-diag'
  scope: sqlDb
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      { category: 'SQLSecurityAuditEvents'; enabled: true }
      { category: 'QueryStoreRuntimeStatistics'; enabled: environment == 'prod' }
    ]
    metrics: [
      { category: 'AllMetrics'; enabled: true }
    ]
  }
}

// ── Outputs ─────────────────────────────────────────────────
output sqlServerFqdn string = sqlServer.properties.fullyQualifiedDomainName
output sqlServerName string = sqlServer.name
output databaseName string = sqlDb.name
