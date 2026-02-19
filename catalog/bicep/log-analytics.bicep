// ──────────────────────────────────────────────────────────────
// InfraForge Approved Template: Log Analytics Workspace
// Category: Monitoring | Format: Bicep
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
var resourcePrefix = '${projectName}-${environment}'
var tags = {
  environment: environment
  project: projectName
  managedBy: 'InfraForge'
}

// ── Retention per environment ───────────────────────────────
var retentionMap = {
  dev: 30
  staging: 60
  prod: 90
}

// ── Log Analytics Workspace ─────────────────────────────────
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${resourcePrefix}-law'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: retentionMap[environment]
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

// ── Outputs ─────────────────────────────────────────────────
output workspaceId string = logAnalytics.id
output workspaceName string = logAnalytics.name
