<#
.SYNOPSIS
    InfraForge — First-time setup wizard for a new Azure tenant.

.DESCRIPTION
    Creates all Azure infrastructure required to run InfraForge:
      1. Resource Group
      2. Azure SQL Server + Database (with Azure AD admin)
      3. SQL Firewall rule for current IP
      4. Entra ID (Azure AD) App Registration
      5. Generates .env file with all values populated

    After running this script, just: python web_start.py

.NOTES
    Prerequisites:
      - Azure CLI (az) installed and authenticated: az login
      - Python 3.9+
      - ODBC Driver 18 for SQL Server
      - GitHub Copilot CLI (for the AI features)

.EXAMPLE
    .\scripts\setup.ps1
    .\scripts\setup.ps1 -Location eastus2 -ResourceGroup MyInfraForge
#>

[CmdletBinding()]
param(
    [string]$ResourceGroup = "InfraForge",
    [string]$Location = "eastus2",
    [string]$SqlServerName = "",
    [string]$SqlDatabaseName = "InfraForgeDB",
    [string]$AppName = "InfraForge",
    [int]$WebPort = 8080,
    [switch]$SkipEntraId,
    [switch]$SkipSql,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

function Write-Step { param([string]$Msg) Write-Host "`n━━━ $Msg ━━━" -ForegroundColor Cyan }
function Write-Ok { param([string]$Msg) Write-Host "  ✓ $Msg" -ForegroundColor Green }
function Write-Warn { param([string]$Msg) Write-Host "  ⚠ $Msg" -ForegroundColor Yellow }
function Write-Err { param([string]$Msg) Write-Host "  ✗ $Msg" -ForegroundColor Red }

function Test-Command {
    param([string]$Name)
    $null -ne (Get-Command -Name $Name -ErrorAction SilentlyContinue)
}

function Get-RandomSuffix {
    -join ((97..122) | Get-Random -Count 6 | ForEach-Object { [char]$_ })
}

# ─────────────────────────────────────────────────────────
# Preflight checks
# ─────────────────────────────────────────────────────────

Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║       InfraForge — First-Time Setup Wizard          ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════════════════════╝" -ForegroundColor Cyan

Write-Step "Checking prerequisites"

# Azure CLI
if (-not (Test-Command "az")) {
    Write-Err "Azure CLI (az) not found."
    Write-Host "  Install: https://learn.microsoft.com/en-us/cli/azure/install-azure-cli" -ForegroundColor Gray
    exit 1
}
Write-Ok "Azure CLI found"

# Check az login
$account = az account show 2>&1 | ConvertFrom-Json -ErrorAction SilentlyContinue
if (-not $account) {
    Write-Warn "Not logged in to Azure CLI. Running 'az login'..."
    az login
    $account = az account show | ConvertFrom-Json
}
$subscriptionId = $account.id
$tenantId = $account.tenantId
$userEmail = $account.user.name
Write-Ok "Logged in as $userEmail"
Write-Ok "Subscription: $($account.name) ($subscriptionId)"
Write-Ok "Tenant: $tenantId"

# Python
if (-not (Test-Command "python")) {
    Write-Err "Python not found. Install Python 3.9+."
    exit 1
}
$pyVer = python --version 2>&1
Write-Ok "Python: $pyVer"

# ODBC Driver
$odbcDrivers = Get-ItemProperty "HKLM:\SOFTWARE\ODBC\ODBCINST.INI\ODBC Drivers" -ErrorAction SilentlyContinue
if ($odbcDrivers -and $odbcDrivers."ODBC Driver 18 for SQL Server") {
    Write-Ok "ODBC Driver 18 for SQL Server found"
} else {
    Write-Warn "ODBC Driver 18 for SQL Server not detected."
    Write-Host "  Download: https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server" -ForegroundColor Gray
    $continue = Read-Host "  Continue anyway? (y/N)"
    if ($continue -ne "y") { exit 1 }
}

# Check .env doesn't already exist (unless -Force)
$envFile = Join-Path $PSScriptRoot ".." ".env"
if ((Test-Path $envFile) -and -not $Force) {
    Write-Warn ".env file already exists at: $envFile"
    $overwrite = Read-Host "  Overwrite? (y/N)"
    if ($overwrite -ne "y") {
        Write-Host "  Keeping existing .env. Use -Force to overwrite." -ForegroundColor Gray
        $skipEnvWrite = $true
    }
}

# ─────────────────────────────────────────────────────────
# Generate defaults
# ─────────────────────────────────────────────────────────

if (-not $SqlServerName) {
    $SqlServerName = "infraforge-sql-$(Get-RandomSuffix)"
}

# Session secret
$sessionSecret = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 32 | ForEach-Object { [char]$_ })

# ─────────────────────────────────────────────────────────
# Step 1: Resource Group
# ─────────────────────────────────────────────────────────

Write-Step "Step 1/5 — Resource Group"

$rgExists = az group exists --name $ResourceGroup 2>&1
if ($rgExists -eq "true") {
    Write-Ok "Resource group '$ResourceGroup' already exists"
} else {
    Write-Host "  Creating resource group '$ResourceGroup' in $Location..."
    az group create --name $ResourceGroup --location $Location -o none
    Write-Ok "Resource group created"
}

# ─────────────────────────────────────────────────────────
# Step 2: Azure SQL Server + Database
# ─────────────────────────────────────────────────────────

Write-Step "Step 2/5 — Azure SQL Server + Database"

if ($SkipSql) {
    Write-Warn "Skipping SQL setup (-SkipSql). You must set AZURE_SQL_CONNECTION_STRING manually."
} else {
    # Check if server exists
    $existingServer = az sql server list --resource-group $ResourceGroup --query "[?name=='$SqlServerName']" -o json 2>&1 | ConvertFrom-Json -ErrorAction SilentlyContinue
    
    if ($existingServer -and $existingServer.Count -gt 0) {
        Write-Ok "SQL Server '$SqlServerName' already exists"
    } else {
        Write-Host "  Creating SQL Server '$SqlServerName'..."
        Write-Host "  (Azure AD-only authentication — no SQL password needed)" -ForegroundColor Gray
        
        # Get current user's Object ID for Azure AD admin
        $currentUserOid = az ad signed-in-user show --query id -o tsv 2>&1
        
        az sql server create `
            --name $SqlServerName `
            --resource-group $ResourceGroup `
            --location $Location `
            --enable-ad-only-auth `
            --external-admin-principal-type User `
            --external-admin-name $userEmail `
            --external-admin-sid $currentUserOid `
            -o none
        
        Write-Ok "SQL Server created with Azure AD admin: $userEmail"
    }

    # Enable public network access
    Write-Host "  Enabling public network access..."
    az sql server update --name $SqlServerName --resource-group $ResourceGroup --enable-public-network true -o none 2>&1
    Write-Ok "Public network access enabled"

    # Firewall: add current IP
    Write-Host "  Detecting public IP for firewall rule..."
    $publicIp = (Invoke-WebRequest -Uri "https://api.ipify.org" -UseBasicParsing).Content.Trim()
    
    az sql server firewall-rule create `
        --server $SqlServerName `
        --resource-group $ResourceGroup `
        --name "infraforge-setup-$($publicIp -replace '\.', '-')" `
        --start-ip-address $publicIp `
        --end-ip-address $publicIp `
        -o none 2>&1
    Write-Ok "Firewall rule added for IP: $publicIp"

    # Allow Azure services
    az sql server firewall-rule create `
        --server $SqlServerName `
        --resource-group $ResourceGroup `
        --name "AllowAzureServices" `
        --start-ip-address 0.0.0.0 `
        --end-ip-address 0.0.0.0 `
        -o none 2>&1
    Write-Ok "Azure services access enabled"

    # Create database
    $existingDb = az sql db list --server $SqlServerName --resource-group $ResourceGroup --query "[?name=='$SqlDatabaseName']" -o json 2>&1 | ConvertFrom-Json -ErrorAction SilentlyContinue
    
    if ($existingDb -and $existingDb.Count -gt 0) {
        Write-Ok "Database '$SqlDatabaseName' already exists"
    } else {
        Write-Host "  Creating database '$SqlDatabaseName' (Basic tier — ~`$5/mo)..."
        az sql db create `
            --server $SqlServerName `
            --resource-group $ResourceGroup `
            --name $SqlDatabaseName `
            --edition Basic `
            --capacity 5 `
            --max-size 2GB `
            -o none
        Write-Ok "Database created"
    }

    $sqlFqdn = "$SqlServerName.database.windows.net"
    $connectionString = "Driver={ODBC Driver 18 for SQL Server};Server=tcp:$sqlFqdn,1433;Database=$SqlDatabaseName;Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30"
    Write-Ok "Connection string ready"
}

# ─────────────────────────────────────────────────────────
# Step 3: Entra ID App Registration
# ─────────────────────────────────────────────────────────

Write-Step "Step 3/5 — Entra ID App Registration"

$entraClientId = ""
$entraClientSecret = ""
$redirectUri = "http://localhost:${WebPort}/api/auth/callback"

if ($SkipEntraId) {
    Write-Warn "Skipping Entra ID setup (-SkipEntraId). App will run in demo mode."
} else {
    # Check if app registration already exists
    $existingApp = az ad app list --display-name $AppName --query "[0]" -o json 2>&1 | ConvertFrom-Json -ErrorAction SilentlyContinue
    
    if ($existingApp -and $existingApp.appId) {
        Write-Ok "App registration '$AppName' already exists (appId: $($existingApp.appId))"
        $entraClientId = $existingApp.appId
        
        $createNewSecret = Read-Host "  Create a new client secret? (y/N)"
        if ($createNewSecret -eq "y") {
            $secretResult = az ad app credential reset --id $existingApp.id --display-name "InfraForge Setup" --years 1 -o json 2>&1 | ConvertFrom-Json
            $entraClientSecret = $secretResult.password
            Write-Ok "New client secret created (expires in 1 year)"
        } else {
            Write-Warn "Using existing secret. Set ENTRA_CLIENT_SECRET manually in .env if needed."
        }
    } else {
        Write-Host "  Creating app registration '$AppName'..."
        
        # Create the app with redirect URI
        $appResult = az ad app create `
            --display-name $AppName `
            --web-redirect-uris $redirectUri `
            --sign-in-audience AzureADMyOrg `
            --query "{appId:appId, id:id}" `
            -o json 2>&1 | ConvertFrom-Json
        
        $entraClientId = $appResult.appId
        $appObjectId = $appResult.id
        Write-Ok "App registration created (appId: $entraClientId)"

        # Add Microsoft Graph User.Read permission
        # Microsoft Graph appId = 00000003-0000-0000-c000-000000000000
        # User.Read permission ID = e1fe6dd8-ba31-4d61-89e7-88639da4683d
        Write-Host "  Adding Microsoft Graph User.Read permission..."
        az ad app permission add `
            --id $appObjectId `
            --api 00000003-0000-0000-c000-000000000000 `
            --api-permissions e1fe6dd8-ba31-4d61-89e7-88639da4683d=Scope `
            -o none 2>&1
        Write-Ok "User.Read permission added"

        # Create client secret
        Write-Host "  Creating client secret..."
        $secretResult = az ad app credential reset --id $appObjectId --display-name "InfraForge Setup" --years 1 -o json 2>&1 | ConvertFrom-Json
        $entraClientSecret = $secretResult.password
        Write-Ok "Client secret created (expires in 1 year)"

        # Create service principal (needed for sign-in to work)
        Write-Host "  Creating service principal..."
        az ad sp create --id $entraClientId -o none 2>&1
        Write-Ok "Service principal created"
    }
}

# ─────────────────────────────────────────────────────────
# Step 4: Generate .env file
# ─────────────────────────────────────────────────────────

Write-Step "Step 4/5 — Generate .env file"

if (-not $skipEnvWrite) {
    $envContent = @"
# InfraForge — Environment Configuration
# Generated by setup.ps1 on $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")

# Entra ID (Azure AD) Authentication
ENTRA_CLIENT_ID=$entraClientId
ENTRA_TENANT_ID=$tenantId
ENTRA_CLIENT_SECRET=$entraClientSecret
ENTRA_REDIRECT_URI=$redirectUri

# GitHub Integration (service-level — users don't need GitHub accounts)
# Create a PAT at https://github.com/settings/tokens with 'repo' scope
GITHUB_TOKEN=
GITHUB_ORG=

# Copilot SDK
COPILOT_MODEL=gpt-4.1
COPILOT_LOG_LEVEL=warning

# Web Server
INFRAFORGE_WEB_HOST=0.0.0.0
INFRAFORGE_WEB_PORT=$WebPort
INFRAFORGE_SESSION_SECRET=$sessionSecret

# Output
INFRAFORGE_OUTPUT_DIR=./output

# Database — Azure SQL with Azure AD auth (pyodbc + DefaultAzureCredential)
AZURE_SQL_CONNECTION_STRING=$connectionString
AZURE_SUBSCRIPTION_ID=$subscriptionId

# Microsoft Fabric Integration (Optional — leave blank to disable)
FABRIC_WORKSPACE_ID=
FABRIC_ONELAKE_DFS_ENDPOINT=
FABRIC_LAKEHOUSE_NAME=
"@

    $envPath = Join-Path $PSScriptRoot ".." ".env"
    Set-Content -Path $envPath -Value $envContent -Encoding UTF8
    Write-Ok ".env written to: $envPath"
} else {
    Write-Warn "Skipped .env write (file already exists)"
}

# ─────────────────────────────────────────────────────────
# Step 5: Install Python dependencies + first run
# ─────────────────────────────────────────────────────────

Write-Step "Step 5/5 — Python dependencies"

$projectRoot = Join-Path $PSScriptRoot ".."
$venvPath = Join-Path $projectRoot ".venv"
$requirementsPath = Join-Path $projectRoot "requirements.txt"

if (-not (Test-Path $venvPath)) {
    Write-Host "  Creating virtual environment..."
    python -m venv $venvPath
    Write-Ok "Virtual environment created at .venv/"
} else {
    Write-Ok "Virtual environment already exists"
}

$pipPath = Join-Path $venvPath "Scripts" "pip.exe"
if (-not (Test-Path $pipPath)) {
    $pipPath = Join-Path $venvPath "bin" "pip"
}

Write-Host "  Installing dependencies..."
& $pipPath install -r $requirementsPath --quiet 2>&1 | Out-Null
Write-Ok "Dependencies installed"

# ─────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────

Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║              Setup Complete!                         ║" -ForegroundColor Green
Write-Host "╚══════════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "  Resources created:" -ForegroundColor White
Write-Host "    Resource Group:  $ResourceGroup" -ForegroundColor Gray
if (-not $SkipSql) {
    Write-Host "    SQL Server:      $SqlServerName.database.windows.net" -ForegroundColor Gray
    Write-Host "    SQL Database:    $SqlDatabaseName" -ForegroundColor Gray
}
if (-not $SkipEntraId -and $entraClientId) {
    Write-Host "    App Registration: $AppName (appId: $entraClientId)" -ForegroundColor Gray
}
Write-Host "    .env file:       $((Resolve-Path $envPath -ErrorAction SilentlyContinue) ?? $envPath)" -ForegroundColor Gray
Write-Host ""
Write-Host "  Remaining manual steps:" -ForegroundColor Yellow
Write-Host "    1. Set GITHUB_TOKEN and GITHUB_ORG in .env (optional — for GitHub publishing)" -ForegroundColor Gray
Write-Host "    2. Grant admin consent for User.Read in Azure Portal if required by your org" -ForegroundColor Gray
Write-Host "       Azure Portal → Entra ID → App Registrations → $AppName → API Permissions → Grant admin consent" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Start InfraForge:" -ForegroundColor White
Write-Host "    .\.venv\Scripts\Activate.ps1" -ForegroundColor Cyan
Write-Host "    python web_start.py" -ForegroundColor Cyan
Write-Host "    # Open http://localhost:${WebPort}" -ForegroundColor Gray
Write-Host ""
Write-Host "  On first launch, InfraForge will automatically:" -ForegroundColor White
Write-Host "    • Create all database tables" -ForegroundColor Gray
Write-Host "    • Seed governance data (policies, standards, services)" -ForegroundColor Gray
Write-Host "    • Configure SQL firewall for your IP" -ForegroundColor Gray
Write-Host ""
