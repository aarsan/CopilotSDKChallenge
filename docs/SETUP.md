# InfraForge - Setup Guide

Complete guide to running `scripts/setup.ps1` and provisioning InfraForge infrastructure.

---

## Overview

The setup script (`scripts/setup.ps1`) is a PowerShell wizard that provisions all Azure
infrastructure required to run InfraForge. It performs 9 steps:

| Step | Action | Creates / Configures |
|------|--------|----------------------|
| 1 | Resource Group | Azure Resource Group in the target region |
| 2 | Azure SQL | SQL Server (Azure AD-only auth) + Database (Basic tier, ~$5/mo) + firewall rules |
| 3 | Entra ID | App Registration with client secret, redirect URI, optional claims, group claims |
| 4 | RBAC & Providers | Contributor role assignment + 12 resource provider registrations |
| 5 | GitHub | Token + organization detection via `gh` CLI |
| 6 | Fabric IQ | Fabric workspace + Lakehouse for OneLake analytics sync |
| 7 | .env File | Generated configuration file with all values populated |
| 8 | Python | Virtual environment + `pip install -r requirements.txt` |
| 9 | Verify | SQL connectivity test via `DefaultAzureCredential` |

Before provisioning, the script runs comprehensive preflight checks:

- **Tool presence** - Azure CLI, Python, ODBC Driver 18
- **Azure login** - Validates `az account show` and fetches subscription/tenant info
- **Permissions** - Subscription RBAC (Contributor/Owner), Entra ID app registration, role assignment capability
- **Resource providers** - Registers critical providers (Microsoft.Sql, Microsoft.Web) before attempting deployments
- **Region capacity** - Probes SQL Server availability across multiple fallback regions
- **Existing resources** - Detects and offers to reuse existing resource groups, SQL servers, and app registrations
- **GitHub CLI** - Checks for `gh` installation and authentication status
- **Fabric capacity** - Checks for available Fabric capacities (F or P SKU) in the tenant

---

## Prerequisites

### Required

| Tool | Purpose | Install |
|------|---------|---------|
| **Azure CLI** (`az`) | All Azure resource provisioning | [Install guide](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) |
| **Python 3.9+** | Runtime, virtual environment, dependencies | [python.org](https://www.python.org/downloads/) |
| **ODBC Driver 18 for SQL Server** | Database connectivity | [Download](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server) |

### Optional

| Tool | Purpose | Install |
|------|---------|---------|
| **GitHub CLI** (`gh`) | Auto-detect token and organization for GitHub integration | [cli.github.com](https://cli.github.com/) |

---

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `-ResourceGroup` | string | `InfraForge` | Name of the Azure Resource Group to create or reuse |
| `-Location` | string | `eastus2` | Azure region. Falls back to other regions if quota is exhausted |
| `-SqlServerName` | string | *(auto-generated)* | SQL Server name (e.g., `infraforge-sql-abcdef`). Auto-generated with random suffix if not provided |
| `-SqlDatabaseName` | string | `InfraForgeDB` | Name of the SQL database to create |
| `-AppName` | string | `InfraForge` | Display name for the Entra ID app registration |
| `-WebPort` | int | `8080` | Port for the web server. Affects the redirect URI (`http://localhost:<port>/api/auth/callback`) |
| `-SkipEntraId` | switch | `$false` | Skip Entra ID app registration. App runs in demo mode without SSO |
| `-SkipSql` | switch | `$false` | Skip SQL Server provisioning. You must set `AZURE_SQL_CONNECTION_STRING` in `.env` manually |
| `-SkipFabric` | switch | `$false` | Skip Fabric workspace provisioning. Fabric IQ analytics will be disabled |
| `-Force` | switch | `$false` | Overwrite `.env` file instead of merging into the existing one |
| `-Cleanup` | switch | `$false` | Tear down resources from a failed setup run (see [Cleanup](#cleanup)) |

---

## Usage Examples

### Basic (all defaults)
```powershell
.\scripts\setup.ps1
```

### Custom region and resource group
```powershell
.\scripts\setup.ps1 -Location westus2 -ResourceGroup MyInfraForge
```

### Skip Entra ID (demo mode, no SSO)
```powershell
.\scripts\setup.ps1 -SkipEntraId
```

### Skip SQL (configure database manually)
```powershell
.\scripts\setup.ps1 -SkipSql
```

### Re-run after partial failure (force overwrite .env)
```powershell
.\scripts\setup.ps1 -Force
```

### Custom web port
```powershell
.\scripts\setup.ps1 -WebPort 3000
```

### Cleanup a failed run
```powershell
.\scripts\setup.ps1 -Cleanup
```

---

## Required Permissions

### Azure Subscription (ARM)

| Permission | Minimum Role | Used In | Purpose |
|-----------|-------------|---------|---------|
| Create resource groups | **Contributor** | Step 1 | `az group create` |
| Create SQL servers and databases | **Contributor** | Step 2 | `az sql server create`, `az sql db create` |
| Create firewall rules | **Contributor** | Step 2 | `az sql server firewall-rule create` |
| Register resource providers | **Contributor** | Preflight + Step 4 | `az provider register` |
| Assign RBAC roles | **Owner** or **User Access Administrator** | Step 4 | `az role assignment create` (optional, warns if unavailable) |

### Entra ID (Azure AD)

| Permission | Source | Used In | Purpose |
|-----------|--------|---------|---------|
| Create app registrations | Tenant setting *"Users can register applications"* **or** Application Developer role | Step 3 | `az ad app create` |
| Update app properties | App owner (auto-granted on creation) | Step 3 | `az ad app update`, optional claims, group claims |
| Grant admin consent | Application Administrator, Cloud Application Administrator, or Global Administrator | Step 3 | `az ad app permission grant` (optional, can be done manually post-setup) |
| Create service principals | Application Developer or Application Administrator | Step 3 | `az ad sp create` |

### GitHub (Optional)

| Permission | Source | Used In | Purpose |
|-----------|--------|---------|---------|
| Read user info | `gh auth login` with default scopes | Step 5 | Auto-detect GitHub account |
| Read org membership | `read:org` scope on GitHub token | Step 5 | List organizations for selection |

---

## Preflight Checks

The setup script validates all of the following **before** prompting you to proceed:

| Check | Behavior |
|-------|----------|
| Azure CLI missing | **Hard fail** - cannot continue |
| Not logged in to Azure | Prompts `az login` |
| No Contributor/Owner on subscription | **Hard fail** - cannot create resources |
| No Owner/User Access Admin | **Warn** - RBAC assignment skipped in Step 4 |
| Cannot verify Entra ID app creation | **Warn + prompt** - may fail at Step 3 |
| `Microsoft.Sql` provider not registered | **Auto-registers** with polling wait |
| No SQL capacity in any region | **Hard fail** (unless `-SkipSql`) |
| Python missing | **Hard fail** |
| ODBC Driver 18 missing | **Warn + prompt** |
| GitHub CLI missing or unauthenticated | **Warn** - GitHub integration skipped |
| No Fabric capacity found | **Warn + prompt** - offers to continue without Fabric |

The preflight summary shows both permission status and resource plan before asking you to confirm.

---

## Cleanup

Running with `-Cleanup` tears down resources from a failed or unwanted setup:

1. **Entra ID app registration** - Deletes the app registration matching `-AppName` (also removes the service principal)
2. **SQL servers** - Deletes all SQL servers matching `infraforge-sql-*` in the resource group
3. **Resource group** - Prompts before deleting (removes ALL resources in the group)
4. **Local `.env` file** - Removes the generated configuration file
5. **GitHub integration** - Checks `gh auth status` and reminds you to run `gh auth logout` or revoke tokens
6. **RBAC role assignment** - Notes that the Contributor role on your subscription was left in place, with the command to remove it manually
7. **Fabric workspace** - Prompts before deleting the `InfraForge-Analytics` workspace (also removes the Lakehouse)

```powershell
.\scripts\setup.ps1 -Cleanup
.\scripts\setup.ps1 -Cleanup -ResourceGroup MyInfraForge  # specify a different RG
```

---

## Troubleshooting

### "No Contributor or Owner role found on subscription"

Your Azure account does not have sufficient permissions on the target subscription.

**Fix**: Ask your subscription admin to assign the Contributor role:
```powershell
az role assignment create --role Contributor `
    --assignee <your-user-object-id> `
    --scope /subscriptions/<subscription-id>
```

### "Failed to create app registration" / Entra ID permission denied

Your tenant restricts application registration to admins only.

**Fix** (choose one):
- Ask your Entra ID admin to enable *"Users can register applications"* in **Entra ID > User Settings**
- Request the **Application Developer** directory role
- Run with `-SkipEntraId` to skip app registration (runs in demo mode without SSO)

### "SQL Server creation failed"

Region quota exhausted, or the `Microsoft.Sql` resource provider is not available.

**Fix**:
- The setup script automatically tries fallback regions (`centralus`, `westus2`, `eastus`, etc.)
- If all regions fail, check your subscription quotas: **Azure Portal > Subscriptions > Usage + quotas**
- Try a different region explicitly: `.\scripts\setup.ps1 -Location centralus`

### "Could not assign Contributor role" / Skipping role assignment

You need the Owner or User Access Administrator role to assign RBAC roles to others.

**Fix**: This is non-blocking for setup. Ask your admin to assign the role post-setup:
```powershell
az role assignment create --role Contributor `
    --assignee <user-oid> `
    --scope /subscriptions/<subscription-id>
```

### "Admin consent could not be auto-granted"

Your tenant requires explicit admin consent for API permissions.

**Fix**: Go to **Azure Portal > Entra ID > App Registrations > InfraForge > API Permissions** and click **Grant admin consent**.

### "SQL connection test failed"

DNS propagation delay, firewall rules not yet active, or Azure AD token issue.

**Fix**:
- Wait 2-3 minutes and try `python web_start.py` - the app retries on startup
- Verify your IP is listed in the SQL Server firewall rules (Azure Portal > SQL Server > Networking)
- Re-authenticate: `az login --tenant <tenant-id>`

### "ODBC Driver 18 not detected"

The ODBC driver is not installed or a different version is present.

**Fix**: Download and install from [Microsoft ODBC Driver for SQL Server](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server).

### "No Fabric capacity found in this tenant"

Your Azure tenant does not have a Microsoft Fabric capacity (F or P SKU).

**Fix** (choose one):
- Start a free Fabric trial at [app.fabric.microsoft.com](https://app.fabric.microsoft.com)
- Ask your tenant admin to provision a Fabric capacity
- Run with `-SkipFabric` to skip Fabric setup (Fabric IQ analytics will be disabled)

### "Could not create Fabric workspace"

The Fabric REST API returned an error when creating the workspace.

**Fix**:
- Ensure your account has permission to create Fabric workspaces
- Verify Fabric capacity is active and not paused (check in the Fabric admin portal)
- Create the workspace manually at [app.fabric.microsoft.com](https://app.fabric.microsoft.com) and set `FABRIC_WORKSPACE_ID` in `.env`

---

## After Setup

```powershell
# Activate the virtual environment
.\.venv\Scripts\Activate.ps1

# Start the web server
python web_start.py

# Open in browser
# http://localhost:8080
```

On first launch, InfraForge automatically:
- Creates all database tables
- Seeds governance data (policies, standards, services)
- Configures SQL firewall for your IP

---

## Environment Variables

The setup script generates a `.env` file with these values:

| Variable | Source | Description |
|----------|--------|-------------|
| `ENTRA_CLIENT_ID` | Step 3 | Entra ID app registration client ID |
| `ENTRA_TENANT_ID` | Azure login | Azure AD tenant ID |
| `ENTRA_CLIENT_SECRET` | Step 3 | Client secret (1-year expiry) |
| `ENTRA_REDIRECT_URI` | `-WebPort` param | OAuth callback URL |
| `GITHUB_TOKEN` | Step 5 | GitHub personal access token from `gh auth` |
| `GITHUB_ORG` | Step 5 | GitHub organization or personal account |
| `COPILOT_MODEL` | Default | `gpt-4.1` |
| `COPILOT_LOG_LEVEL` | Default | `warning` |
| `INFRAFORGE_WEB_HOST` | Default | `0.0.0.0` |
| `INFRAFORGE_WEB_PORT` | `-WebPort` param | `8080` |
| `INFRAFORGE_SESSION_SECRET` | Auto-generated | Random 32-char session secret |
| `INFRAFORGE_OUTPUT_DIR` | Default | `./output` |
| `AZURE_SQL_CONNECTION_STRING` | Step 2 | ODBC connection string for Azure SQL |
| `AZURE_SQL_SERVER` | Step 2 | SQL Server name |
| `AZURE_RESOURCE_GROUP` | `-ResourceGroup` param | Resource group name |
| `AZURE_SUBSCRIPTION_ID` | Azure login | Subscription ID |
| `FABRIC_WORKSPACE_ID` | Step 6 | Fabric workspace GUID |
| `FABRIC_ONELAKE_DFS_ENDPOINT` | Step 6 | OneLake DFS endpoint (`https://onelake.dfs.fabric.microsoft.com`) |
| `FABRIC_LAKEHOUSE_NAME` | Step 6 | Lakehouse display name (`infraforge_lakehouse`) |

When re-running setup with an existing `.env`, managed values (including `FABRIC_*` settings) are updated in-place while
manual customizations are preserved. Use `-Force` to overwrite entirely.
