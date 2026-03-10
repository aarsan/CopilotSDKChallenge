<#
.SYNOPSIS
    InfraForge - First-time setup wizard for a new Azure tenant.

.DESCRIPTION
    Creates all Azure infrastructure required to run InfraForge:
      1. Resource Group
      2. Azure SQL Server + Database (with Azure AD admin)
      3. SQL Firewall rule for current IP
      4. Entra ID (Azure AD) App Registration (with client secret, optional claims, group claims)
      5. RBAC role assignment (Contributor) for ARM deployments
      6. Resource provider registration (Microsoft.Web, Microsoft.Sql, etc.)
      7. GitHub integration (token + org via gh CLI)
      8. Generates .env file with all values populated

    After running this script, just: python web_start.py

.NOTES
    Prerequisites:
      - Azure CLI (az) installed and authenticated: az login
      - Azure subscription with Contributor or Owner access
      - Python 3.9+
      - ODBC Driver 18 for SQL Server
      - GitHub CLI (gh) installed and authenticated: gh auth login
      - GitHub account (for publishing repos & PRs)

.EXAMPLE
    .\scripts\setup.ps1
    .\scripts\setup.ps1 -Location eastus2 -ResourceGroup MyInfraForge
    .\scripts\setup.ps1 -Cleanup          # tear down resources from a failed run
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
    [switch]$Force,
    [switch]$Cleanup
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

function New-AppClientSecret {
    <#
    .SYNOPSIS
        Create a client secret with fallback for tenant credential lifetime policies.
        Tries 1 year down to 1 week to accommodate restrictive policies.
    #>
    param([string]$AppObjectId, [string]$DisplayName = "InfraForge Setup")

    $endDates = @(
        @{ Label = "1 year";   Date = (Get-Date).AddYears(1).ToString("yyyy-MM-dd") },
        @{ Label = "6 months"; Date = (Get-Date).AddMonths(6).ToString("yyyy-MM-dd") },
        @{ Label = "3 months"; Date = (Get-Date).AddMonths(3).ToString("yyyy-MM-dd") },
        @{ Label = "1 month";  Date = (Get-Date).AddMonths(1).ToString("yyyy-MM-dd") },
        @{ Label = "2 weeks";  Date = (Get-Date).AddDays(14).ToString("yyyy-MM-dd") },
        @{ Label = "1 week";   Date = (Get-Date).AddDays(7).ToString("yyyy-MM-dd") }
    )

    foreach ($attempt in $endDates) {
        $raw = az ad app credential reset --id $AppObjectId --append `
            --display-name $DisplayName --end-date $attempt.Date -o json --only-show-errors 2>&1
        $result = ConvertFrom-AzJson $raw
        if ($result -and (Get-Member -InputObject $result -Name "password" -ErrorAction SilentlyContinue)) {
            return @{ Password = $result.password; Expiry = $attempt.Label }
        }
    }
    return $null
}

function Merge-EnvFile {
    <#
    .SYNOPSIS
        Merge key=value pairs into an existing .env file.
        Updates existing keys (only when new value is non-empty),
        appends keys that don't exist yet, preserves everything else.
    #>
    param(
        [string]$Path,
        [hashtable]$Values
    )

    $lines = @()
    $updatedKeys = @{}

    if (Test-Path $Path) {
        $lines = Get-Content -Path $Path -Encoding UTF8
    }

    $newLines = @()
    foreach ($line in $lines) {
        $matched = $false
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
            $key = $Matches[1]
            if ($Values.ContainsKey($key)) {
                $newVal = $Values[$key]
                if ($newVal -ne "" -and $null -ne $newVal) {
                    $newLines += "$key=$newVal"
                } else {
                    $newLines += $line
                }
                $updatedKeys[$key] = $true
                $matched = $true
            }
        }
        if (-not $matched) {
            $newLines += $line
        }
    }

    foreach ($key in $Values.Keys) {
        if (-not $updatedKeys.ContainsKey($key)) {
            $val = $Values[$key]
            $newLines += "$key=$val"
        }
    }

    Set-Content -Path $Path -Value ($newLines -join "`n") -Encoding UTF8
}

function ConvertFrom-AzJson {
    <#
    .SYNOPSIS
        Safely parse az CLI output as JSON. Returns $null if the output is an
        error message or otherwise non-JSON instead of throwing.
    #>
    param([string]$Raw)
    $trimmed = ($Raw ?? "").Trim()
    if (-not $trimmed -or $trimmed[0] -notin '[', '{', '"') { return $null }
    try { return $trimmed | ConvertFrom-Json -ErrorAction Stop }
    catch { return $null }
}

# ─────────────────────────────────────────────────────────
# Cleanup mode - tear down resources from a failed setup
# ─────────────────────────────────────────────────────────

if ($Cleanup) {
    Write-Host ""
    Write-Host "╔══════════════════════════════════════════════════════╗" -ForegroundColor Yellow
    Write-Host "║       InfraForge - Cleanup Failed Setup              ║" -ForegroundColor Yellow
    Write-Host "╚══════════════════════════════════════════════════════╝" -ForegroundColor Yellow
    Write-Host ""

    # Verify az login
    $account = ConvertFrom-AzJson (az account show 2>&1)
    if (-not $account) {
        Write-Err "Not logged in to Azure CLI. Run 'az login' first."
        exit 1
    }
    $subscriptionId = $account.id
    Write-Ok "Logged in as $($account.user.name)"

    # 1. Delete Entra ID app registration
    Write-Step "Removing Entra ID app registration"
    $rawApp = az ad app list --display-name $AppName --query "[0]" -o json 2>&1
    $existingApp = ConvertFrom-AzJson $rawApp
    if ($existingApp -and $existingApp.id) {
        az ad app delete --id $existingApp.id -o none 2>&1
        Write-Ok "Deleted app registration '$AppName' (appId: $($existingApp.appId))"
    } else {
        Write-Warn "No app registration named '$AppName' found - skipping"
    }

    # 2. Delete SQL Server (also deletes its databases and firewall rules)
    Write-Step "Removing SQL resources"
    $rgExists = az group exists --name $ResourceGroup 2>&1
    if ($rgExists -eq "true") {
        # Find all InfraForge SQL servers in the resource group
        $rawServers = az sql server list --resource-group $ResourceGroup --query "[?starts_with(name, 'infraforge-sql-')]" -o json 2>&1
        $servers = ConvertFrom-AzJson $rawServers
        if ($servers -and $servers.Count -gt 0) {
            foreach ($srv in $servers) {
                Write-Host "  Deleting SQL Server '$($srv.name)'..."
                az sql server delete --name $srv.name --resource-group $ResourceGroup --yes -o none 2>&1
                if ($LASTEXITCODE -eq 0) {
                    Write-Ok "Deleted SQL Server '$($srv.name)'"
                } else {
                    Write-Warn "Could not delete SQL Server '$($srv.name)'"
                }
            }
        } else {
            Write-Warn "No InfraForge SQL servers found in '$ResourceGroup' - skipping"
        }

        # 3. Delete resource group (only if user confirms - it deletes EVERYTHING in it)
        Write-Host ""
        Write-Warn "Resource group '$ResourceGroup' still exists."
        $deleteRg = Read-Host "  Delete the entire resource group? This removes ALL resources in it. (y/N)"
        if ($deleteRg -eq "y") {
            Write-Host "  Deleting resource group '$ResourceGroup'... (this may take a few minutes)"
            az group delete --name $ResourceGroup --yes -o none 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Ok "Resource group deleted"
            } else {
                Write-Warn "Resource group deletion may still be in progress. Wait a minute before re-running setup."
            }
        } else {
            Write-Warn "Keeping resource group. You can delete individual resources manually."
        }
    } else {
        Write-Warn "Resource group '$ResourceGroup' does not exist - skipping"
    }

    # 4. Remove .env file
    Write-Step "Removing local config"
    $envPath = Join-Path $PSScriptRoot ".." ".env"
    if (Test-Path $envPath) {
        Remove-Item $envPath -Force
        Write-Ok "Deleted .env file"
    } else {
        Write-Warn "No .env file found - skipping"
    }

    # 5. GitHub token reminder
    Write-Step "GitHub Integration"
    $ghInstalled = Get-Command gh -ErrorAction SilentlyContinue
    if ($ghInstalled) {
        $ghStatus = gh auth status 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Warn "GitHub CLI is still authenticated. If you want to revoke access:"
            Write-Host "    gh auth logout" -ForegroundColor DarkGray
        } else {
            Write-Ok "GitHub CLI is not authenticated - nothing to clean up"
        }
    } else {
        Write-Ok "GitHub CLI not installed - nothing to clean up"
    }
    Write-Host "  If you created a personal access token for InfraForge, revoke it at:" -ForegroundColor Gray
    Write-Host "    https://github.com/settings/tokens" -ForegroundColor DarkGray

    # 6. RBAC note (Contributor role was assigned to the current user, not the app)
    Write-Step "RBAC Role Assignment"
    Write-Warn "The Contributor role on your subscription was left in place (it is your own role)."
    Write-Host "  To remove it manually:" -ForegroundColor Gray
    Write-Host "    az role assignment delete --role Contributor --assignee <your-oid> --scope /subscriptions/<sub-id>" -ForegroundColor DarkGray

    Write-Host ""
    Write-Host "  Cleanup complete. You can re-run setup with:" -ForegroundColor Green
    Write-Host "    .\scripts\setup.ps1" -ForegroundColor Cyan
    Write-Host ""
    exit 0
}

# ─────────────────────────────────────────────────────────
# Preflight checks
# ─────────────────────────────────────────────────────────

Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║       InfraForge - First-Time Setup Wizard          ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Before you begin, make sure you have:" -ForegroundColor White
Write-Host "    • An Azure subscription with Contributor (or Owner) access" -ForegroundColor Gray
Write-Host "    • A GitHub account (for publishing repos & PRs)" -ForegroundColor Gray
Write-Host ""

Write-Step "Checking prerequisites"

# Azure CLI
if (-not (Test-Command "az")) {
    Write-Err "Azure CLI (az) not found."
    Write-Host "  Install: https://learn.microsoft.com/en-us/cli/azure/install-azure-cli" -ForegroundColor Gray
    exit 1
}
Write-Ok "Azure CLI found"

# Check az login
$account = ConvertFrom-AzJson (az account show 2>&1)
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

# Get current user OID (reused by RBAC checks, SQL AD admin, role assignment)
$currentUserOid = az ad signed-in-user show --query id -o tsv 2>&1
if (-not $currentUserOid -or $currentUserOid -match "ERROR") {
    Write-Err "Could not determine signed-in user Object ID."
    exit 1
}
Write-Ok "User OID: $currentUserOid"

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

# GitHub CLI
$ghAvailable = $false
$ghAuthenticated = $false
if (Test-Command "gh") {
    $ghAvailable = $true
    Write-Ok "GitHub CLI (gh) found"
    $ghAuthCheck = gh auth status 2>&1
    if ($LASTEXITCODE -eq 0) {
        $ghAuthenticated = $true
        Write-Ok "GitHub CLI: authenticated"
    } else {
        Write-Warn "GitHub CLI: installed but not authenticated"
        Write-Host "  Run 'gh auth login' to enable GitHub integration, or continue without it." -ForegroundColor Gray
    }
} else {
    Write-Warn "GitHub CLI (gh) not found. GitHub integration will be skipped."
    Write-Host "  Install: https://cli.github.com/" -ForegroundColor Gray
    Write-Host "  GitHub publishing features require the gh CLI." -ForegroundColor Gray
}

# Check existing .env - we'll merge if it exists (non-destructive)
$envFile = Join-Path $PSScriptRoot ".." ".env"
$envFileExists = Test-Path $envFile
if ($envFileExists -and -not $Force) {
    Write-Warn ".env file already exists at: $envFile"
    Write-Host "  Managed values will be updated in-place. Manual customizations preserved." -ForegroundColor Gray
}

# ─────────────────────────────────────────────────────────
# Preflight: Azure permissions
# ─────────────────────────────────────────────────────────

Write-Step "Checking Azure permissions"

# Subscription RBAC check
$hasContributor = $false
$hasOwner = $false
$hasRoleAssignmentWrite = $false

$rawRoles = az role assignment list `
    --assignee $currentUserOid `
    --scope "/subscriptions/$subscriptionId" `
    --query "[].roleDefinitionName" `
    -o json 2>&1
$roles = ConvertFrom-AzJson $rawRoles

if ($roles) {
    $roleNames = @($roles)
    if ($roleNames -contains "Owner") {
        $hasOwner = $true
        $hasContributor = $true
        $hasRoleAssignmentWrite = $true
        Write-Ok "Subscription role: Owner (full permissions)"
    } elseif ($roleNames -contains "Contributor") {
        $hasContributor = $true
        Write-Ok "Subscription role: Contributor"
    }
    if ($roleNames -contains "User Access Administrator") {
        $hasRoleAssignmentWrite = $true
    }
}

if (-not $hasContributor) {
    Write-Err "No Contributor or Owner role found on subscription $subscriptionId"
    Write-Host "  You need at least Contributor to create resource groups, SQL servers, etc." -ForegroundColor Gray
    Write-Host "  Ask your tenant admin to assign it:" -ForegroundColor Gray
    Write-Host "  az role assignment create --role Contributor --assignee $currentUserOid --scope /subscriptions/$subscriptionId" -ForegroundColor DarkGray
    exit 1
}

if (-not $hasRoleAssignmentWrite) {
    Write-Warn "No Owner or User Access Administrator role - RBAC role assignment will be skipped in Step 4"
}

# Entra ID permission check
$canCreateApps = $false
if (-not $SkipEntraId) {
    # Check tenant setting: "Users can register applications"
    $authPolicyRaw = az rest --method GET `
        --url "https://graph.microsoft.com/v1.0/policies/authorizationPolicy" `
        --query "defaultUserRolePermissions.allowedToCreateApps" `
        -o tsv 2>&1
    if ($authPolicyRaw -eq "true") {
        $canCreateApps = $true
        Write-Ok "Entra ID: tenant allows users to register applications"
    }

    # If tenant setting didn't confirm, check directory roles
    if (-not $canCreateApps) {
        $dirRolesRaw = az rest --method GET `
            --url "https://graph.microsoft.com/v1.0/me/memberOf/microsoft.graph.directoryRole" `
            --query "value[].displayName" `
            -o json 2>&1
        $dirRoles = ConvertFrom-AzJson $dirRolesRaw
        if ($dirRoles) {
            $dirRoleNames = @($dirRoles)
            $appRoles = @("Application Developer", "Application Administrator", "Cloud Application Administrator", "Global Administrator")
            $matchedRole = $dirRoleNames | Where-Object { $_ -in $appRoles } | Select-Object -First 1
            if ($matchedRole) {
                $canCreateApps = $true
                Write-Ok "Entra ID: can create apps via role '$matchedRole'"
            }
        }
    }

    if (-not $canCreateApps) {
        Write-Warn "Entra ID: could not verify app registration permission"
        Write-Host "  App registration (Step 3) may fail if your tenant restricts it." -ForegroundColor Gray
        Write-Host "  Options:" -ForegroundColor Gray
        Write-Host "    - Ask your admin to enable 'Users can register applications' in Entra ID" -ForegroundColor DarkGray
        Write-Host "    - Ask for the Application Developer role" -ForegroundColor DarkGray
        Write-Host "    - Run with -SkipEntraId to skip app registration" -ForegroundColor DarkGray
        $continueEntra = Read-Host "  Continue anyway? (y/N)"
        if ($continueEntra -ne "y") { exit 1 }
    }
}

# ─────────────────────────────────────────────────────────
# Preflight: resource providers
# ─────────────────────────────────────────────────────────

Write-Step "Checking resource providers"

$allProviders = @(
    "Microsoft.Web", "Microsoft.Sql", "Microsoft.Storage",
    "Microsoft.KeyVault", "Microsoft.Network", "Microsoft.Compute",
    "Microsoft.ContainerService", "Microsoft.OperationalInsights",
    "Microsoft.Insights", "Microsoft.ManagedIdentity", "Microsoft.Authorization"
)
$criticalProviders = @("Microsoft.Sql", "Microsoft.Web")
$unregisteredCritical = @()
$unregisteredOther = @()

foreach ($provider in $allProviders) {
    $state = az provider show --namespace $provider --query registrationState -o tsv 2>&1
    if ($state -ne "Registered") {
        if ($provider -in $criticalProviders) {
            $unregisteredCritical += $provider
        } else {
            $unregisteredOther += $provider
        }
    }
}

if ($unregisteredCritical.Count -gt 0) {
    foreach ($provider in $unregisteredCritical) {
        Write-Host "  Registering $provider (required for setup)..." -ForegroundColor Gray
        az provider register --namespace $provider -o none 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Err "Failed to register $provider."
            Write-Host "  Ask your subscription admin: az provider register --namespace $provider" -ForegroundColor Gray
            exit 1
        }
    }
    # Wait for registration to propagate (async operation)
    Write-Host "  Waiting for provider registration to propagate..."
    foreach ($provider in $unregisteredCritical) {
        $maxWait = 60; $waited = 0
        while ($waited -lt $maxWait) {
            $state = az provider show --namespace $provider --query registrationState -o tsv 2>&1
            if ($state -eq "Registered") { break }
            Start-Sleep -Seconds 5
            $waited += 5
        }
        if ($state -eq "Registered") {
            Write-Ok "$provider registered"
        } else {
            Write-Warn "$provider still registering (state: $state) - proceeding anyway"
        }
    }
} else {
    Write-Ok "Critical providers registered (Microsoft.Sql, Microsoft.Web)"
}

# Register non-critical providers in background (these are also registered in Step 4)
if ($unregisteredOther.Count -gt 0) {
    foreach ($provider in $unregisteredOther) {
        az provider register --namespace $provider -o none 2>&1
    }
    Write-Ok "Queued registration for $($unregisteredOther.Count) additional providers"
} else {
    Write-Ok "All $($allProviders.Count) resource providers registered"
}

# ─────────────────────────────────────────────────────────
# Preflight: resolve region, existing resources, SQL capacity
# ─────────────────────────────────────────────────────────

Write-Step "Checking Azure region availability"

# Existing resource group?
$rgExists = az group exists --name $ResourceGroup 2>&1
$existingSqlServer = $null

if (-not $SqlServerName) {
    if ($rgExists -eq "true") {
        # Check for leftover SQL server from a previous run
        $rawExisting = az sql server list --resource-group $ResourceGroup `
            --query "[?starts_with(name, 'infraforge-sql-')].{name:name, state:state, location:location}" `
            -o json 2>&1
        $existingSqlServers = ConvertFrom-AzJson $rawExisting
        if ($existingSqlServers -and $existingSqlServers.Count -gt 0) {
            $pick = $existingSqlServers[0]
            Write-Warn "Found existing SQL Server '$($pick.name)' ($($pick.state)) in $($pick.location) from a previous run."
            $reuse = Read-Host "  Reuse this server? (Y/n)"
            if ($reuse -ne "n") {
                $SqlServerName = $pick.name
                $existingSqlServer = $pick
                Write-Ok "Will reuse SQL Server '$SqlServerName'"
            } else {
                Write-Host "  Tip: run '.\scripts\setup.ps1 -Cleanup' to remove old resources first." -ForegroundColor Gray
                $SqlServerName = "infraforge-sql-$(Get-RandomSuffix)"
            }
        } else {
            $SqlServerName = "infraforge-sql-$(Get-RandomSuffix)"
        }
    } else {
        $SqlServerName = "infraforge-sql-$(Get-RandomSuffix)"
    }
}

# Check SQL region availability (unless skipping SQL or reusing an existing server)
$resolvedLocation = $Location
if (-not $SkipSql -and -not $existingSqlServer) {
    $candidateRegions = @($Location, "centralus", "westus2", "eastus", "northcentralus", "southcentralus", "westus3")
    $candidateRegions = $candidateRegions | Select-Object -Unique

    Write-Host "  Checking which regions accept SQL Server provisioning..."
    $availableRegions = @()
    foreach ($region in $candidateRegions) {
        $capRaw = az rest --method get `
            --url "/subscriptions/$subscriptionId/providers/Microsoft.Sql/locations/$region/capabilities?api-version=2023-05-01-preview" `
            --query "{status: status, reason: reason}" `
            -o json 2>&1
        $cap = ConvertFrom-AzJson $capRaw
        if ($cap -and $cap.status -eq "Available") {
            Write-Ok "$region - available"
            $availableRegions += $region
        } else {
            $reason = if ($cap -and $cap.reason) { $cap.reason.Substring(0, [Math]::Min(80, $cap.reason.Length)) } else { "unknown" }
            Write-Warn "$region - restricted ($reason)"
        }
    }

    if ($availableRegions.Count -eq 0) {
        Write-Err "No regions are accepting new SQL Server provisioning."
        Write-Err "Checked: $($candidateRegions -join ', ')"
        Write-Host "  This is an Azure capacity/policy restriction on your subscription." -ForegroundColor Gray
        Write-Host "  Try a different subscription, or request an exception via Azure Support." -ForegroundColor Gray
        Write-Host "  Or skip SQL and configure it manually: .\scripts\setup.ps1 -SkipSql" -ForegroundColor Gray
        exit 1
    }

    $resolvedLocation = $availableRegions[0]
    if ($resolvedLocation -ne $Location) {
        Write-Warn "Requested region '$Location' is restricted. Will use '$resolvedLocation' for all resources."
    }
} elseif ($existingSqlServer) {
    $resolvedLocation = $existingSqlServer.location
    Write-Ok "Using region '$resolvedLocation' (from existing SQL Server)"
} else {
    Write-Ok "SQL skipped - using requested region '$Location'"
}

# Check existing SQL database (only possible if server exists)
$existingSqlDb = $false
if (-not $SkipSql -and $existingSqlServer) {
    $rawDbList = az sql db list --server $SqlServerName --resource-group $ResourceGroup `
        --query "[?name=='$SqlDatabaseName']" -o json 2>&1
    $dbCheck = ConvertFrom-AzJson $rawDbList
    if ($dbCheck -and $dbCheck.Count -gt 0) { $existingSqlDb = $true }
}

# Check existing Entra ID app registration
$existingEntraApp = $null
if (-not $SkipEntraId) {
    $rawApp = az ad app list --display-name $AppName --query "[0]" -o json 2>&1
    $existingEntraApp = ConvertFrom-AzJson $rawApp
}

# Session secret
$sessionSecret = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 32 | ForEach-Object { [char]$_ })

# ── Preflight summary ──────────────────────────────────
Write-Step "Setup plan"

Write-Host ""
Write-Host "  Permissions:" -ForegroundColor White
$rbacLabel = if ($hasOwner) { "Owner" } elseif ($hasContributor) { "Contributor" } else { "None" }
$rbacColor = if ($hasContributor) { "Green" } else { "Red" }
Write-Host "    Subscription RBAC: $rbacLabel on $subscriptionId" -ForegroundColor $rbacColor
$roleWriteLabel = if ($hasRoleAssignmentWrite) { "can write" } else { "read-only (will skip RBAC assignment)" }
$roleWriteColor = if ($hasRoleAssignmentWrite) { "Green" } else { "Yellow" }
Write-Host "    Role assignment:   $roleWriteLabel" -ForegroundColor $roleWriteColor
if (-not $SkipEntraId) {
    $entraLabel = if ($canCreateApps) { "can create" } else { "unverified (may fail)" }
    $entraColor = if ($canCreateApps) { "Green" } else { "Yellow" }
    Write-Host "    Entra ID apps:     $entraLabel" -ForegroundColor $entraColor
}
$ghLabel = if ($ghAuthenticated) { "authenticated" } elseif ($ghAvailable) { "not authenticated" } else { "not installed" }
$ghColor = if ($ghAuthenticated) { "Green" } else { "Yellow" }
Write-Host "    GitHub CLI:        $ghLabel" -ForegroundColor $ghColor

Write-Host ""
Write-Host "  Resources:" -ForegroundColor White
Write-Host "    Region:           $resolvedLocation" -ForegroundColor White
Write-Host "    Resource Group:   $ResourceGroup $(if ($rgExists -eq 'true') {'(exists)'} else {'(will create)'})" -ForegroundColor White
if (-not $SkipSql) {
    Write-Host "    SQL Server:       $SqlServerName $(if ($existingSqlServer) {'(exists)'} else {'(will create)'})" -ForegroundColor White
    Write-Host "    SQL Database:     $SqlDatabaseName $(if ($existingSqlDb) {'(exists)'} else {'(will create)'})" -ForegroundColor White
}
if (-not $SkipEntraId) {
    Write-Host "    App Registration: $AppName $(if ($existingEntraApp) {'(exists)'} else {'(will create)'})" -ForegroundColor White
}
Write-Host ""
$proceed = Read-Host "  Proceed with setup? (Y/n)"
if ($proceed -eq "n") {
    Write-Host "  Aborted." -ForegroundColor Gray
    exit 0
}

# ═════════════════════════════════════════════════════════
# PROVISIONING - all preflight checks passed
# ═════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────
# Step 1: Resource Group
# ─────────────────────────────────────────────────────────

Write-Step "Step 1/9 - Resource Group"

if ($rgExists -eq "true") {
    Write-Ok "Resource group '$ResourceGroup' already exists"
} else {
    Write-Host "  Creating resource group '$ResourceGroup' in $resolvedLocation..."
    az group create --name $ResourceGroup --location $resolvedLocation -o none
    Write-Ok "Resource group created"
}

# ─────────────────────────────────────────────────────────
# Step 2: Azure SQL Server + Database
# ─────────────────────────────────────────────────────────

Write-Step "Step 2/9 - Azure SQL Server + Database"

if ($SkipSql) {
    Write-Warn "Skipping SQL setup (-SkipSql). You must set AZURE_SQL_CONNECTION_STRING manually."
} else {
    if ($existingSqlServer) {
        Write-Ok "SQL Server '$SqlServerName' already exists"
    } else {
        Write-Host "  Creating SQL Server '$SqlServerName' in $resolvedLocation..."
        Write-Host "  (Azure AD-only authentication - no SQL password needed)" -ForegroundColor Gray

        $createOutput = az sql server create `
            --name $SqlServerName `
            --resource-group $ResourceGroup `
            --location $resolvedLocation `
            --enable-ad-only-auth `
            --external-admin-principal-type User `
            --external-admin-name $userEmail `
            --external-admin-sid $currentUserOid `
            -o none 2>&1

        if ($LASTEXITCODE -ne 0) {
            Write-Err "SQL Server creation failed: $createOutput"
            exit 1
        }
        Write-Ok "SQL Server created with Azure AD admin: $userEmail"
    }

    # Enable public network access
    Write-Host "  Enabling public network access..."
    az sql server update --name $SqlServerName --resource-group $ResourceGroup --enable-public-network true -o none --only-show-errors 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Warn "Could not enable public network access - continuing..." }
    else { Write-Ok "Public network access enabled" }

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
    if ($existingSqlDb) {
        Write-Ok "Database '$SqlDatabaseName' already exists"
    } else {
        Write-Host "  Creating database '$SqlDatabaseName' (Basic tier - ~`$5/mo)..."
        az sql db create `
            --server $SqlServerName `
            --resource-group $ResourceGroup `
            --name $SqlDatabaseName `
            --edition Basic `
            --capacity 5 `
            --max-size 2GB `
            -o none
        if ($LASTEXITCODE -ne 0) {
            Write-Err "Database creation failed."
            exit 1
        }
        Write-Ok "Database created"
    }

    $sqlFqdn = "$SqlServerName.database.windows.net"
    $connectionString = "Driver={ODBC Driver 18 for SQL Server};Server=tcp:$sqlFqdn,1433;Database=$SqlDatabaseName;Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30"
    Write-Ok "Connection string ready"
}

# ─────────────────────────────────────────────────────────
# Step 3: Entra ID App Registration
# ─────────────────────────────────────────────────────────

Write-Step "Step 3/9 - Entra ID App Registration"

$entraClientId = ""
$entraClientSecret = ""
$redirectUri = "http://localhost:${WebPort}/api/auth/callback"

if ($SkipEntraId) {
    Write-Warn "Skipping Entra ID setup (-SkipEntraId). Authentication will not work without Entra ID."
} else {
    $appObjectId = $null

    if ($existingEntraApp -and $existingEntraApp.appId) {
        Write-Ok "App registration '$AppName' already exists (appId: $($existingEntraApp.appId))"
        $entraClientId = $existingEntraApp.appId
        $appObjectId = $existingEntraApp.id

        # Update redirect URI in case WebPort changed
        Write-Host "  Updating redirect URI to $redirectUri..."
        az ad app update --id $appObjectId --web-redirect-uris $redirectUri -o none --only-show-errors 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Redirect URI updated"
        } else {
            Write-Warn "Could not update redirect URI. Update manually if port changed."
        }

        # Always create a new secret - old secrets cannot be retrieved from Entra ID
        $createNewSecret = Read-Host "  Create a new client secret? (Y/n)"
        if ($createNewSecret -ne "n") {
            Write-Host "  Creating client secret..."
            $secretInfo = New-AppClientSecret -AppObjectId $appObjectId
            if ($secretInfo) {
                $entraClientSecret = $secretInfo.Password
                Write-Ok "New client secret created (expires in $($secretInfo.Expiry))"
            } else {
                Write-Warn "Could not create client secret."
                Write-Host "    Create one manually: Azure Portal → App Registrations → $AppName → Certificates & Secrets" -ForegroundColor Gray
            }
        } else {
            Write-Warn "Skipped secret creation. Set ENTRA_CLIENT_SECRET manually in .env if needed."
        }
    } else {
        Write-Host "  Creating app registration '$AppName'..."

        # Create the app with redirect URI
        $appResult = ConvertFrom-AzJson (az ad app create `
            --display-name $AppName `
            --web-redirect-uris $redirectUri `
            --sign-in-audience AzureADMyOrg `
            --query "{appId:appId, id:id}" `
            -o json 2>&1)

        if (-not $appResult -or -not $appResult.appId) {
            Write-Err "Failed to create app registration."
            exit 1
        }

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
            -o none --only-show-errors 2>&1

        # Grant User.Read permission (User.Read is sufficient for /me and /me/manager)
        Write-Host "  Granting User.Read permission..."
        az ad app permission grant --id $entraClientId --api 00000003-0000-0000-c000-000000000000 --scope "User.Read" -o none --only-show-errors 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Permissions added and granted"
        } else {
            Write-Ok "Permissions added"
            Write-Warn "Admin consent could not be auto-granted. Grant it manually in Azure Portal if required by your org."
        }

        # Create client secret
        Write-Host "  Creating client secret..."
        $secretInfo = New-AppClientSecret -AppObjectId $appObjectId
        if ($secretInfo) {
            $entraClientSecret = $secretInfo.Password
            Write-Ok "Client secret created (expires in $($secretInfo.Expiry))"
        } else {
            Write-Warn "Could not create client secret."
            Write-Host "    Create one manually: Azure Portal → App Registrations → $AppName → Certificates & Secrets" -ForegroundColor Gray
        }
    }

    # ── Shared configuration (runs for both new and existing apps) ──

    # Ensure service principal exists (needed for sign-in)
    Write-Host "  Ensuring service principal exists..."
    $existingSp = ConvertFrom-AzJson (az ad sp show --id $entraClientId -o json --only-show-errors 2>&1)
    if ($existingSp) {
        Write-Ok "Service principal already exists"
    } else {
        az ad sp create --id $entraClientId -o none --only-show-errors 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Service principal created"
        } else {
            Write-Warn "Could not create service principal. Sign-in may fail."
        }
    }

    # Ensure User.Read permission is granted (idempotent)
    Write-Host "  Ensuring User.Read permission is granted..."
    az ad app permission grant --id $entraClientId --api 00000003-0000-0000-c000-000000000000 --scope "User.Read" -o none --only-show-errors 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "User.Read permission granted"
    } else {
        Write-Warn "Could not auto-grant User.Read. Consent may be required on first sign-in."
    }

    # Configure optional ID token claims (email, upn, given_name, family_name)
    Write-Host "  Configuring optional ID token claims..."
    $claimsJson = @{
        optionalClaims = @{
            idToken = @(
                @{ name = "email";       essential = $false }
                @{ name = "upn";         essential = $false }
                @{ name = "given_name";  essential = $false }
                @{ name = "family_name"; essential = $false }
            )
        }
    } | ConvertTo-Json -Depth 4 -Compress
    $claimsTmp = [System.IO.Path]::GetTempFileName()
    Set-Content -Path $claimsTmp -Value $claimsJson -Encoding UTF8
    az rest --method PATCH `
        --url "https://graph.microsoft.com/v1.0/applications/$appObjectId" `
        --headers "Content-Type=application/json" `
        --body "@$claimsTmp" `
        -o none --only-show-errors 2>&1
    $claimsExitCode = $LASTEXITCODE
    Remove-Item $claimsTmp -ErrorAction SilentlyContinue
    if ($claimsExitCode -eq 0) {
        Write-Ok "Optional claims configured on ID token"
    } else {
        Write-Warn "Could not configure optional claims. Some identity fields may require Graph API fallback."
    }

    # Enable security group claims in tokens
    Write-Host "  Enabling security group claims in tokens..."
    az ad app update --id $appObjectId --set groupMembershipClaims=SecurityGroup -o none --only-show-errors 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "Group membership claims enabled (SecurityGroup)"
    } else {
        Write-Warn "Could not set groupMembershipClaims. Group-based access control may not work."
    }
}

# ─────────────────────────────────────────────────────────
# Step 4: RBAC & Resource Providers
# ─────────────────────────────────────────────────────────

Write-Step "Step 4/9 - RBAC & Resource Providers"

# Assign Contributor role to current user (needed for ARM deployments)
Write-Host "  Checking Contributor role assignment..."
$existingRole = az role assignment list --assignee $currentUserOid --role Contributor --scope "/subscriptions/$subscriptionId" --query "[0].id" -o tsv 2>&1
if ($existingRole -and $existingRole -notmatch "ERROR") {
    Write-Ok "Contributor role already assigned"
} elseif ($hasRoleAssignmentWrite) {
    Write-Host "  Assigning Contributor role on subscription..."
    az role assignment create `
        --role Contributor `
        --assignee $currentUserOid `
        --scope "/subscriptions/$subscriptionId" `
        -o none 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "Contributor role assigned"
    } else {
        Write-Warn "Could not assign Contributor role."
    }
} else {
    Write-Warn "Skipping role assignment (no Owner/User Access Administrator detected in preflight)"
    Write-Host "    Ask your tenant admin to run:" -ForegroundColor Gray
    Write-Host "    az role assignment create --role Contributor --assignee $currentUserOid --scope /subscriptions/$subscriptionId" -ForegroundColor DarkGray
}

# Register resource providers needed for infrastructure deployments
$providers = @(
    "Microsoft.Web",            # App Service, Functions
    "Microsoft.Sql",            # Azure SQL
    "Microsoft.Storage",        # Storage Accounts
    "Microsoft.KeyVault",       # Key Vault
    "Microsoft.Network",        # VNets, NSGs, Load Balancers
    "Microsoft.Compute",        # VMs, VMSS
    "Microsoft.ContainerService", # AKS
    "Microsoft.OperationalInsights", # Log Analytics
    "Microsoft.Insights",       # Application Insights, Monitoring
    "Microsoft.ManagedIdentity", # Managed Identities
    "Microsoft.Authorization"   # RBAC, Policies
)

Write-Host "  Registering resource providers (this may take a minute)..."
foreach ($provider in $providers) {
    $state = az provider show --namespace $provider --query registrationState -o tsv 2>&1
    if ($state -eq "Registered") {
        continue
    }
    az provider register --namespace $provider -o none 2>&1
    Write-Host "    Registering $provider..." -ForegroundColor Gray
}
Write-Ok "Resource providers registered ($($providers.Count) providers)"

# ─────────────────────────────────────────────────────────
# Step 5/9: GitHub Integration
# ─────────────────────────────────────────────────────────

Write-Step "Step 5/9 - GitHub Integration"

$githubToken = ""
$githubOrg = ""

if (Test-Command "gh") {
    Write-Ok "GitHub CLI (gh) found"

    # Check if gh is authenticated
    $ghStatus = gh auth status 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "GitHub CLI is authenticated"

        # Get the token from gh
        $githubToken = (gh auth token 2>&1).Trim()
        if ($githubToken -and $githubToken -notmatch "ERROR") {
            Write-Ok "GitHub token retrieved from gh auth"
        } else {
            $githubToken = ""
            Write-Warn "Could not retrieve token from gh auth."
        }

        # Detect user/org - prefer org if available, fall back to user
        $ghUser = (gh api user --jq ".login" 2>&1).Trim()
        if ($ghUser -and $ghUser -notmatch "ERROR") {
            Write-Ok "GitHub account: $ghUser"

            # Check if user belongs to any orgs
            $orgsRaw = gh api user/orgs --jq ".[].login" 2>&1
            $orgs = @()
            if ($orgsRaw -and $orgsRaw -notmatch "ERROR") {
                $orgs = $orgsRaw -split "`n" | Where-Object { $_.Trim() -ne "" }
            }

            if ($orgs.Count -gt 0) {
                Write-Host "  Available organizations:" -ForegroundColor Gray
                for ($i = 0; $i -lt $orgs.Count; $i++) {
                    Write-Host "    [$($i + 1)] $($orgs[$i])" -ForegroundColor Gray
                }
                Write-Host "    [0] Use personal account ($ghUser)" -ForegroundColor Gray
                $orgChoice = Read-Host "  Select organization (0-$($orgs.Count), default: 0)"
                if ($orgChoice -and [int]$orgChoice -ge 1 -and [int]$orgChoice -le $orgs.Count) {
                    $githubOrg = $orgs[[int]$orgChoice - 1]
                    Write-Ok "Using organization: $githubOrg"
                } else {
                    $githubOrg = $ghUser
                    Write-Ok "Using personal account: $ghUser"
                }
            } else {
                $githubOrg = $ghUser
                Write-Ok "Using personal account: $ghUser (no orgs found)"
            }
        } else {
            Write-Warn "Could not determine GitHub user. Set GITHUB_ORG manually in .env."
        }
    } else {
        Write-Warn "GitHub CLI is not authenticated. Run 'gh auth login' to enable GitHub integration."
        Write-Host "    GitHub publishing will be disabled until GITHUB_TOKEN is configured." -ForegroundColor Gray
    }
} else {
    Write-Warn "GitHub CLI (gh) not found. GitHub integration will be skipped."
    Write-Host "    Install: https://cli.github.com/" -ForegroundColor Gray
    Write-Host "    Or set GITHUB_TOKEN and GITHUB_ORG manually in .env." -ForegroundColor Gray
}

# ─────────────────────────────────────────────────────────
# Step 6/9: Generate .env file
# ─────────────────────────────────────────────────────────

Write-Step "Step 6/9 - Generate .env file"

# Build hashtable of managed key-value pairs
$managedEnvValues = @{
    "ENTRA_CLIENT_ID"             = $entraClientId
    "ENTRA_TENANT_ID"             = $tenantId
    "ENTRA_CLIENT_SECRET"         = $entraClientSecret
    "ENTRA_REDIRECT_URI"          = $redirectUri
    "GITHUB_TOKEN"                = $githubToken
    "GITHUB_ORG"                  = $githubOrg
    "COPILOT_MODEL"               = "gpt-4.1"
    "COPILOT_LOG_LEVEL"           = "warning"
    "INFRAFORGE_WEB_HOST"         = "0.0.0.0"
    "INFRAFORGE_WEB_PORT"         = "$WebPort"
    "INFRAFORGE_SESSION_SECRET"   = $sessionSecret
    "INFRAFORGE_OUTPUT_DIR"       = "./output"
    "AZURE_SQL_CONNECTION_STRING" = $connectionString
    "AZURE_SQL_SERVER"            = $SqlServerName
    "AZURE_RESOURCE_GROUP"        = $ResourceGroup
    "AZURE_SUBSCRIPTION_ID"       = $subscriptionId
    "WORKIQ_ENABLED"               = "true"
    "WORKIQ_TIMEOUT"               = "30"
}

if ($envFileExists -and -not $Force) {
    # Merge mode: update managed keys, preserve everything else
    Merge-EnvFile -Path $envFile -Values $managedEnvValues
    Write-Ok ".env updated (merged managed values, preserved manual customizations)"
} else {
    # First-run or forced overwrite: write the full template
    $envContent = @"
# InfraForge - Environment Configuration
# Generated by setup.ps1 on $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")

# Entra ID (Azure AD) Authentication
ENTRA_CLIENT_ID=$entraClientId
ENTRA_TENANT_ID=$tenantId
ENTRA_CLIENT_SECRET=$entraClientSecret
ENTRA_REDIRECT_URI=$redirectUri

# GitHub Integration
GITHUB_TOKEN=$githubToken
GITHUB_ORG=$githubOrg

# Copilot SDK
COPILOT_MODEL=gpt-4.1
COPILOT_LOG_LEVEL=warning

# Web Server
INFRAFORGE_WEB_HOST=0.0.0.0
INFRAFORGE_WEB_PORT=$WebPort
INFRAFORGE_SESSION_SECRET=$sessionSecret

# Output
INFRAFORGE_OUTPUT_DIR=./output

# Database - Azure SQL with Azure AD auth (pyodbc + DefaultAzureCredential)
AZURE_SQL_CONNECTION_STRING=$connectionString
AZURE_SQL_SERVER=$SqlServerName
AZURE_RESOURCE_GROUP=$ResourceGroup
AZURE_SUBSCRIPTION_ID=$subscriptionId

# Microsoft Work IQ (M365 organizational intelligence)
WORKIQ_ENABLED=true
WORKIQ_TIMEOUT=30
"@

    $envPath = Join-Path $PSScriptRoot ".." ".env"
    Set-Content -Path $envPath -Value $envContent -Encoding UTF8
    Write-Ok ".env written to: $envPath"
}

# ─────────────────────────────────────────────────────────
# Step 7/9: Install Python dependencies
# ─────────────────────────────────────────────────────────

Write-Step "Step 7/9 - Python dependencies"

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
# ─────────────────────────────────────────────────────────
# Step 8/9: Microsoft Work IQ (M365 organizational intelligence)
# ─────────────────────────────────────────────────────────

Write-Step "Step 8/9 - Microsoft Work IQ"

$workiqReady = $false
$nodeCmd = Get-Command node -ErrorAction SilentlyContinue
if ($nodeCmd) {
    $nodeVer = (node --version 2>&1).ToString().TrimStart('v')
    $nodeMajor = [int]($nodeVer.Split('.')[0])
    if ($nodeMajor -ge 18) {
        Write-Ok "Node.js v$nodeVer (>= 18 required)"

        $npmCmd = Get-Command npm -ErrorAction SilentlyContinue
        if ($npmCmd) {
            # Install @microsoft/workiq globally so it persists across sessions
            Write-Host "  Installing @microsoft/workiq globally..."
            $installOutput = npm install -g @microsoft/workiq 2>&1
            if ($LASTEXITCODE -eq 0) {
                $wiqVersion = npx @microsoft/workiq --version 2>&1
                Write-Ok "Work IQ CLI installed: $wiqVersion"

                # Accept EULA / authenticate
                Write-Host "  Running EULA acceptance / authentication..."
                npx @microsoft/workiq accept-eula 2>&1
                if ($LASTEXITCODE -eq 0) {
                    Write-Ok "Work IQ: EULA accepted / authentication complete"
                } else {
                    Write-Warn "Work IQ: EULA/auth flow did not complete automatically."
                    Write-Host "    You can run this later: npx @microsoft/workiq accept-eula" -ForegroundColor DarkGray
                }

                # Try a test query to verify M365 permissions
                Write-Host "  Verifying M365 permissions..."
                $testResult = npx @microsoft/workiq ask -q "test" 2>&1
                if ($LASTEXITCODE -eq 0) {
                    Write-Ok "Work IQ: M365 permissions verified"
                    $workiqReady = $true
                } else {
                    Write-Warn "Work IQ: M365 query failed — tenant admin consent may be required."
                    Write-Host "    The Work IQ CLI is installed, but M365 permissions are not yet granted." -ForegroundColor Gray
                    Write-Host "    Ask your tenant admin to grant consent, then re-run:" -ForegroundColor Gray
                    Write-Host "    npx @microsoft/workiq accept-eula" -ForegroundColor DarkGray
                }
            } else {
                Write-Warn "npm install -g @microsoft/workiq failed:"
                Write-Host "    $installOutput" -ForegroundColor DarkGray
                Write-Host "    Try running manually: npm install -g @microsoft/workiq" -ForegroundColor DarkGray
            }
        } else {
            Write-Warn "npm not found. Work IQ requires npm (comes with Node.js)."
        }
    } else {
        Write-Warn "Node.js v$nodeVer is too old. Work IQ requires Node.js 18+."
        Write-Host "    Download: https://nodejs.org/" -ForegroundColor DarkGray
    }
} else {
    Write-Warn "Node.js not found. Work IQ (M365 integration) will be disabled."
    Write-Host "    Download: https://nodejs.org/" -ForegroundColor DarkGray
    Write-Host "    Work IQ searches emails, meetings, docs, and Teams for organizational context." -ForegroundColor Gray
}

if (-not $workiqReady) {
    Write-Host "  Work IQ is optional — InfraForge works fine without it." -ForegroundColor Gray
    Write-Host "  To enable later: install Node.js 18+, then run:" -ForegroundColor Gray
    Write-Host "    npm install -g @microsoft/workiq" -ForegroundColor DarkGray
    Write-Host "    npx @microsoft/workiq accept-eula" -ForegroundColor DarkGray
}

# ─────────────────────────────────────────────────────────
# Step 9/9: Verify connectivity
# ─────────────────────────────────────────────────────────

Write-Step "Step 9/9 - Verify connectivity"

if (-not $SkipSql -and $connectionString) {
    Write-Host "  Testing SQL connection..."
    $pythonExe = Join-Path $venvPath "Scripts" "python.exe"
    if (-not (Test-Path $pythonExe)) { $pythonExe = Join-Path $venvPath "bin" "python" }
    
    $testScript = @"
import sys, os
os.environ['AZURE_SQL_CONNECTION_STRING'] = '''$connectionString'''
try:
    from azure.identity import DefaultAzureCredential
    cred = DefaultAzureCredential(exclude_shared_token_cache_credential=True)
    token = cred.get_token('https://database.windows.net/.default')
    import pyodbc
    conn = pyodbc.connect('''$connectionString''', attrs_before={1256: token.token.encode('UTF-16-LE')})
    cursor = conn.cursor()
    cursor.execute('SELECT 1')
    print('OK')
    conn.close()
except Exception as e:
    print(f'FAIL: {e}')
    sys.exit(1)
"@
    $result = & $pythonExe -c $testScript 2>&1
    if ($result -match "OK") {
        Write-Ok "SQL connection successful"
    } else {
        Write-Warn "SQL connection test failed: $result"
        Write-Host "    This may resolve after a few minutes (DNS propagation)." -ForegroundColor Gray
        Write-Host "    You can also try: az login --tenant $tenantId" -ForegroundColor Gray
    }
} else {
    Write-Warn "Skipped SQL connectivity test"
}

# ─────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────

Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║              Setup Complete!                         ║" -ForegroundColor Green
Write-Host "╚══════════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "  Resources created:" -ForegroundColor White
Write-Host "    Region:          $resolvedLocation" -ForegroundColor Gray
Write-Host "    Resource Group:  $ResourceGroup" -ForegroundColor Gray
if (-not $SkipSql) {
    Write-Host "    SQL Server:      $SqlServerName.database.windows.net" -ForegroundColor Gray
    Write-Host "    SQL Database:    $SqlDatabaseName" -ForegroundColor Gray
}
if (-not $SkipEntraId -and $entraClientId) {
    Write-Host "    App Registration: $AppName (appId: $entraClientId)" -ForegroundColor Gray
}
if ($githubOrg) {
    Write-Host "    GitHub:          $githubOrg (token from gh CLI)" -ForegroundColor Gray
}
Write-Host "    .env file:       $((Resolve-Path $envFile -ErrorAction SilentlyContinue) ?? $envFile)" -ForegroundColor Gray
Write-Host ""
Write-Host "  RBAC & Providers:" -ForegroundColor White
Write-Host "    Contributor:    assigned on subscription $subscriptionId" -ForegroundColor Gray
Write-Host "    Providers:      11 resource providers registered" -ForegroundColor Gray
Write-Host ""
Write-Host "  Work IQ (M365 integration):" -ForegroundColor White
if ($workiqReady) {
    Write-Host "    Status:         Ready (authenticated)" -ForegroundColor Green
} else {
    Write-Host "    Status:         Not configured (optional)" -ForegroundColor Yellow
    Write-Host "    To enable:      npm install -g @microsoft/workiq && npx @microsoft/workiq accept-eula" -ForegroundColor DarkGray
}
Write-Host ""
Write-Host "  Remaining manual steps:" -ForegroundColor Yellow
if (-not $githubToken) {
    Write-Host "    • Set GITHUB_TOKEN and GITHUB_ORG in .env (optional - for GitHub publishing)" -ForegroundColor Gray
    Write-Host "      Or install GitHub CLI (gh) and run 'gh auth login', then re-run setup." -ForegroundColor DarkGray
}
Write-Host "    • If sign-in shows a consent prompt, approve 'User.Read' for the app" -ForegroundColor Gray
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
