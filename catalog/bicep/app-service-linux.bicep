// ──────────────────────────────────────────────────────────────
// InfraForge Approved Template: App Service (Linux)
// Category: Compute | Format: Bicep
// ──────────────────────────────────────────────────────────────

@description('Project name used for resource naming (2-12 chars)')
@minLength(2)
@maxLength(12)
param projectName string

@description('Target environment')
@allowed(['dev', 'staging', 'prod'])
param environment string

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Log Analytics workspace ID for diagnostic logging')
param logAnalyticsWorkspaceId string = ''

// ── Naming & Tags ───────────────────────────────────────────
var resourcePrefix = '${projectName}-${environment}'
var tags = {
  environment: environment
  project: projectName
  managedBy: 'InfraForge'
  deployedAt: utcNow('yyyy-MM-dd')
}

// ── SKU mapping per environment ─────────────────────────────
var skuMap = {
  dev: { name: 'B1', tier: 'Basic' }
  staging: { name: 'S1', tier: 'Standard' }
  prod: { name: 'P1v3', tier: 'PremiumV3' }
}

// ── App Service Plan ────────────────────────────────────────
resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${resourcePrefix}-asp'
  location: location
  tags: tags
  sku: skuMap[environment]
  properties: {
    reserved: true // Linux
  }
}

// ── Web App ─────────────────────────────────────────────────
resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: '${resourcePrefix}-app'
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      alwaysOn: environment == 'prod'
      healthCheckPath: '/health'
    }
  }
}

// ── Diagnostic Logging (if workspace provided) ──────────────
resource appDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (!empty(logAnalyticsWorkspaceId)) {
  name: '${webApp.name}-diag'
  scope: webApp
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      { category: 'AppServiceHTTPLogs'; enabled: true }
      { category: 'AppServiceConsoleLogs'; enabled: true }
      { category: 'AppServiceAppLogs'; enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics'; enabled: true }
    ]
  }
}

// ── Outputs ─────────────────────────────────────────────────
output appServiceUrl string = 'https://${webApp.properties.defaultHostName}'
output appServiceName string = webApp.name
output principalId string = webApp.identity.principalId
