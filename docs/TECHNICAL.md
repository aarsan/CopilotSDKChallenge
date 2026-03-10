# InfraForge — Technical Documentation

## Architecture Overview

InfraForge is a self-service infrastructure platform that enables enterprise teams to provision
production-ready Azure infrastructure through natural language. It combines a FastAPI backend,
Azure SQL Database for all persistent data, and the GitHub Copilot SDK for AI-driven generation.

```
                     ┌───────────────────────┐
                     │   Microsoft Entra ID  │
                     │   (Azure AD Tenant)   │
                     │                       │
                     │  ┌─────────────────┐  │
                     │  │ App Registration │  │
                     │  │ (ENTRA_CLIENT_ID)│  │
                     │  │  + Client Secret │  │
                     │  │  + Redirect URI  │  │
                     │  │  + Group Claims  │  │
                     │  └─────────────────┘  │
                     └───┬──────────┬────────┘
                  Tokens │          │ Graph API
           (MSAL.js +    │          │ (/me, /me/manager)
            MSAL Python) │          │
┌────────────────────────┴──────────┴─────────────────────┐
│                    Web Browser (SPA)                      │
│  index.html + app.js + styles.css                        │
│  ─ MSAL.js handles login → acquires token silently       │
│  ─ Service Catalog  ─ Templates  ─ Governance            │
│  ─ Activity Monitor ─ Infrastructure Designer (Chat)     │
│  ─ Fabric Analytics (dashboard)                          │
└───────────────────────┬──────────────────────────────────┘
                        │ HTTP/WebSocket (Bearer token)
┌───────────────────────▼──────────────────────────────────┐
│               FastAPI Application (web.py)                │
│  ─ REST endpoints    ─ WebSocket chat                     │
│  ─ Auth (Entra ID)   ─ Standards API router               │
│  ─ Static files      ─ Deployment orchestration           │
│  ─ Fabric sync       ─ Work IQ analytics                  │
├──────────┬────────────┬──────────────┬───────────────────┤
│ Copilot  │ ARM Gen    │ Standards    │ Policy Validator   │
│ SDK      │ Engine     │ Engine       │                    │
└──────────┴─────┬──────┼──────────────┴───────────────────┘
                 │      │
        ┌────────┘      └────────┐
        ▼                        ▼
┌──────────────────┐   ┌─────────────────────────────────┐
│ Azure SQL Database│   │  Microsoft Fabric (Fabric IQ)   │
│ (All persistent   │──▶│  ┌───────────────────────────┐  │
│  data lives here) │ETL│  │  OneLake (Lakehouse)       │  │
│                   │   │  │  ─ pipeline_runs.csv       │  │
└───────────────────┘   │  │  ─ governance_reviews.csv  │  │
                        │  │  ─ service_catalog.csv     │  │
                        │  │  ─ template_catalog.csv    │  │
                        │  │  ─ deployments.csv         │  │
                        │  │  ─ compliance.csv          │  │
                        │  └─────────────┬─────────────┘  │
                        │                ▼                 │
                        │   Power BI / Fabric Semantic     │
                        │   Models (analytics dashboards)  │
                        └─────────────────────────────────┘
```

## Data Storage — Azure SQL Database

**All data lives in Azure SQL Database.** There are no local files for persistent state.
Authentication uses Azure AD tokens via `DefaultAzureCredential`.

### Core Tables

| Table | Purpose |
|-------|---------|
| `user_sessions` | Auth sessions with Entra ID claims |
| `chat_messages` | Conversation history per session |
| `usage_logs` | Work IQ analytics — cost attribution, department tracking |
| `services` | Azure service catalog (approval status, risk tier, active version) |
| `service_versions` | Versioned ARM templates per service (v1, v2, ...) |
| `service_artifacts` | Approval gate artifacts (policy, template) |
| `service_policies` | Per-service policy requirements |
| `service_approved_skus` | Allowed SKUs per service |
| `service_approved_regions` | Allowed regions per service |
| `catalog_templates` | Composed infrastructure templates (blueprints) |
| `deployments` | ARM deployment records with status tracking |
| `projects` | Infrastructure project proposals |
| `approval_requests` | Service approval request lifecycle |

### Governance Tables

| Table | Purpose |
|-------|---------|
| `org_standards` | Organization-wide governance standards (formal rules) |
| `org_standards_history` | Version history for every standard change |
| `security_standards` | Machine-readable security rules (SEC-001..SEC-015) |
| `governance_policies` | Organization-wide policy rules (GOV-001..GOV-008) |
| `compliance_frameworks` | Framework definitions (SOC2, CIS Azure, HIPAA) |
| `compliance_controls` | Individual controls within frameworks |
| `compliance_assessments` | Results of compliance checks |

### Schema Management

All table schemas are defined in `AZURE_SQL_SCHEMA_STATEMENTS` (database.py) and the
standards extension in `_STANDARDS_SCHEMA` (standards.py). Both use `IF NOT EXISTS` guards
for idempotent creation. Tables are created automatically during server startup via `init_db()`.

## Organization Standards System

The standards system provides formal, declarative governance that drives policy generation,
ARM template hardening, and compliance checks automatically.

### How It Works

1. **Standards are stored in SQL** — the `org_standards` table holds each standard with:
   - A scope pattern (glob) that determines which Azure resource types it applies to
   - A JSON rule definition specifying the exact requirement
   - Severity level (critical, high, medium, low)
   - Enabled/disabled flag

2. **Scope matching** — When generating policies or templates for a service, the engine
   filters standards by matching the service's resource type against each standard's scope:
   - `*` — matches all services
   - `Microsoft.Storage/*` — matches all storage types
   - `Microsoft.Sql/*,Microsoft.DBforPostgreSQL/*` — matches SQL + PostgreSQL

3. **Prompt context building** — The standards engine generates formatted text blocks that
   are injected into Copilot SDK prompts, ensuring AI-generated policies and ARM templates
   comply with organization governance.

4. **Version history** — Every update to a standard creates a version record in
   `org_standards_history`, enabling full audit trails.

### Default Standards (Seeded on First Run)

| ID | Name | Category | Severity | Scope |
|----|------|----------|----------|-------|
| STD-ENCRYPT-TLS | Require TLS 1.2 Minimum | encryption | critical | * |
| STD-ENCRYPT-HTTPS | HTTPS Required | encryption | critical | Microsoft.Web/*, Microsoft.Storage/* |
| STD-ENCRYPT-REST | Encryption at Rest Required | encryption | critical | Microsoft.Sql/*, Microsoft.Storage/* |
| STD-IDENTITY-MI | Managed Identity Required | identity | high | * |
| STD-IDENTITY-AAD | Azure AD Authentication Required | identity | high | Microsoft.Sql/* |
| STD-NETWORK-PUBLIC | No Public Access by Default | network | high | * |
| STD-NETWORK-PE | Private Endpoints Required (Prod) | network | high | Microsoft.Sql/*, Microsoft.Storage/* |
| STD-MONITOR-DIAG | Diagnostic Logging Required | monitoring | high | * |
| STD-TAG-REQUIRED | Required Resource Tags | tagging | high | * |
| STD-REGION-ALLOWED | Allowed Deployment Regions | geography | critical | * |
| STD-COST-THRESHOLD | Cost Approval Threshold | cost | medium | * |

### Rule Types

Standards support multiple rule types in their JSON rule definition:

- **`property`** — Require a specific ARM property value
  ```json
  { "type": "property", "key": "minTlsVersion", "operator": ">=", "value": "1.2" }
  ```

- **`tags`** — Require specific resource tags
  ```json
  { "type": "tags", "required_tags": ["environment", "owner", "costCenter", "project"] }
  ```

- **`allowed_values`** — Restrict a property to allowed values
  ```json
  { "type": "allowed_values", "key": "location", "values": ["eastus2", "westus2", "westeurope"] }
  ```

- **`cost_threshold`** — Set maximum cost limits
  ```json
  { "type": "cost_threshold", "max_monthly_usd": 5000 }
  ```

## API Endpoints

### Standards API (`/api/standards`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/standards` | List all standards (filter: ?category=, ?enabled_only=) |
| POST | `/api/standards` | Create a new standard |
| GET | `/api/standards/categories` | Get distinct categories |
| GET | `/api/standards/{id}` | Get a single standard |
| PUT | `/api/standards/{id}` | Update a standard (creates version history) |
| DELETE | `/api/standards/{id}` | Delete a standard and history |
| GET | `/api/standards/{id}/history` | Get version history |
| GET | `/api/standards/for-service/{service_id}` | Get standards matching a service type |
| GET | `/api/standards/context/policy/{service_id}` | Get policy generation prompt context |
| GET | `/api/standards/context/arm/{service_id}` | Get ARM generation prompt context |

### Service Catalog API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/catalog/services` | List all services with hydrated policies/SKUs/regions |
| POST | `/api/catalog/services` | Add a new service |
| PATCH | `/api/catalog/services/{id}` | Update service status/policies |
| DELETE | `/api/catalog/services/{id}` | Remove a service |
| GET | `/api/catalog/services/approved-for-templates` | Services with active ARM templates |
| GET | `/api/catalog/services/sync` | Trigger Azure resource provider sync |

### Template Catalog API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/catalog/templates` | List all templates |
| POST | `/api/catalog/templates` | Register a template |
| POST | `/api/catalog/templates/compose` | Compose template from approved services |
| POST | `/api/catalog/templates/compose-from-prompt` | Compose from natural language |
| DELETE | `/api/catalog/templates/{id}` | Remove a template |
| GET | `/api/catalog/templates/{id}/composition` | Service dependencies with semver |
| GET | `/api/catalog/templates/{id}/versions` | Version history |
| POST | `/api/catalog/templates/{id}/test` | Run structural tests |
| POST | `/api/catalog/templates/{id}/validate` | Full validation pipeline (NDJSON stream) |
| POST | `/api/catalog/templates/{id}/publish` | Publish to catalog |
| POST | `/api/catalog/templates/{id}/deploy` | Deploy to Azure via ARM SDK |
| POST | `/api/catalog/templates/{id}/feedback` | Analyze user feedback for revision |
| POST | `/api/catalog/templates/{id}/revise` | Apply revision (add services or code edit) |

> **Full API reference:** See `docs/ARCHITECTURE.md` §4 for the complete list of 65+ endpoints.

## Service Approval Workflow (2-Gate)

Services go through a formal approval process before they can be used in templates:

```
not_approved → [Policy Gate] → [Template Gate] → validating → approved
```

1. **Policy Gate** — Define policies, security requirements, allowed SKUs/regions
2. **Template Gate** — Generate and validate an ARM template
3. **Validation** — Deploy the ARM template via What-If analysis to verify it's valid
4. **Approved** — Service is ready for use in catalog templates

## Template Composition

Templates are composed from approved services — no manual IaC authoring required:

1. Select approved services from the catalog
2. Set quantity per service (e.g., 2 SQL databases)
3. Choose which parameters to expose in the template
4. The compose endpoint merges ARM skeletons into a single template
5. Standard parameters (resourceName, location, environment, etc.) are shared

## File Structure

> **Full project structure:** See `docs/ARCHITECTURE.md` §2 for the complete file tree.

```
src/
  web.py              — FastAPI app, all REST/WebSocket endpoints (~8800 lines)
  database.py         — Azure SQL backend, schema, CRUD functions (~3500 lines)
  orchestrator.py     — LLM orchestration: template analysis, composition, healing
  model_router.py     — Task → LLM model routing (8 task types, 6 models)
  template_engine.py  — ARM template composition and dependency wiring
  standards.py        — Organization standards engine (SQL-backed)
  standards_api.py    — REST API router for standards CRUD
  auth.py             — Entra ID authentication
  azure_sync.py       — Azure Resource Provider sync engine
  config.py           — Environment configuration, system message
  tools/              — Copilot SDK tool definitions (26 tools)
    arm_generator.py  — ARM template skeleton registry (~21 resource types)
    deploy_engine.py  — ARM SDK deployment (azure-mgmt-resource)
    service_catalog.py — Service approval tools (5 tools)
    ...
static/
  index.html          — SPA shell
  app.js              — Frontend JavaScript (~6200 lines)
  styles.css          — UI styles (~7200 lines)
docs/
  ARCHITECTURE.md     — Architecture reference (this project's single source of truth)
  TECHNICAL.md        — This file
  README.md           — Project overview
```

## Microsoft Entra ID — App Registration & Auth Flow

InfraForge uses Microsoft Entra ID (Azure AD) for enterprise authentication with
identity-aware infrastructure provisioning. This requires an **App Registration**
in your Azure AD tenant.

### App Registration Requirements

The setup script (`scripts/setup.ps1` Step 3) creates the App Registration with:

| Setting | Value | Purpose |
|---------|-------|---------|
| Display Name | `InfraForge` | Visible in Entra ID portal |
| Client Secret | Auto-generated (1 year) | Backend token exchange |
| Redirect URI | `http://localhost:8080/api/auth/callback` | OAuth2 callback |
| Optional Claims | `email`, `upn` (ID token) | User identity |
| Group Claims | `SecurityGroup` | Role-based access (PlatformTeam, Admin) |

### OAuth2 Authorization Code Flow

```
┌──────────┐     1. Login click       ┌──────────────────┐
│  Browser  │ ──────────────────────▶ │  Microsoft Entra  │
│ (MSAL.js) │                         │    ID (Azure AD)   │
│           │ ◀────────────────────── │                    │
│           │  2. Auth code (redirect) │  App Registration  │
└─────┬─────┘                         │  ─ Client ID       │
      │                               │  ─ Tenant ID       │
      │ 3. Auth code                  │  ─ Group claims    │
      │    POST /api/auth/callback    └──────────┬─────────┘
      ▼                                          │
┌───────────────────────┐   4. Exchange code      │
│   FastAPI Backend     │      for tokens ────────┘
│  (MSAL Python)        │
│  ─ ConfidentialClient │   5. Call Graph API
│  ─ Token cache        │ ────────────────────▶ Microsoft Graph
│  ─ Session store      │                       /me + /me/manager
│                       │ ◀────────────────────
│                       │   6. Org data (dept, manager, cost center)
└───────────────────────┘
```

### Work IQ — Identity-Aware Intelligence

When authenticated via Entra ID, InfraForge enriches the user context through
Microsoft Graph API calls (`src/auth.py::_fetch_graph_profile`):

| Graph Data | Source | Used For |
|------------|--------|----------|
| Display name, email | ID token claims | Session identity |
| Job title, department | `/me` | Cost attribution, role context |
| Office location | `/me` | Regional defaults |
| Cost center | `/me` (extension attr) | Chargeback analytics |
| Manager chain | `/me/manager` | Approval routing |
| Group memberships | Token group claims | Role-based access (PlatformTeam, Admin) |

This profile data is stored in the `user_sessions` table and attached to all
`usage_logs` entries, enabling per-department cost attribution and organizational
analytics — the foundation of **Work IQ**.

### Required Entra ID Permissions

| Permission | Type | Purpose |
|------------|------|---------|
| Create app registrations | Entra ID | Setup creates the app |
| Grant admin consent | Entra ID | Group claims require consent |
| `User.Read` | Delegated (Graph) | Read authenticated user profile |
| `User.ReadBasic.All` | Delegated (Graph) | Read manager chain |

## Fabric IQ — Analytics Data Pipeline

InfraForge integrates with Microsoft Fabric to provide enterprise analytics
through OneLake. The `src/fabric.py` module implements the full ETL pipeline.

The Fabric workspace and Lakehouse are **auto-provisioned** by `scripts/setup.ps1`
(Step 6/9). The script creates a workspace (`InfraForge-Analytics`) and Lakehouse
(`infraforge_lakehouse`) via the Fabric REST API, populating the three `FABRIC_*`
environment variables automatically. Use `-SkipFabric` to skip if no capacity exists.

### Architecture

```
┌───────────────────┐       ┌────────────────────────────────────────┐
│  Azure SQL (OLTP) │       │       Microsoft Fabric (Fabric IQ)     │
│                   │       │                                        │
│  ─ pipeline_runs  │  ETL  │  ┌──────────────────────────────────┐  │
│  ─ governance     │──────▶│  │    OneLake Lakehouse             │  │
│  ─ services       │ Sync  │  │    (DFS endpoint)                │  │
│  ─ templates      │       │  │                                  │  │
│  ─ deployments    │       │  │  Tables/                         │  │
│  ─ compliance     │       │  │    pipeline_runs.csv             │  │
└───────────────────┘       │  │    governance_reviews.csv        │  │
                            │  │    service_catalog.csv           │  │
                            │  │    template_catalog.csv          │  │
                            │  │    deployments.csv               │  │
                            │  │    compliance_assessments.csv    │  │
                            │  └───────────────┬──────────────────┘  │
                            │                  ▼                     │
                            │  ┌──────────────────────────────────┐  │
                            │  │  Fabric Semantic Models           │  │
                            │  │  ─ Power BI dashboards            │  │
                            │  │  ─ Cross-org analytics            │  │
                            │  │  ─ Cost trend reporting           │  │
                            │  └──────────────────────────────────┘  │
                            └────────────────────────────────────────┘
```

### Components

| Class | File | Purpose |
|-------|------|---------|
| `FabricClient` | `src/fabric.py` | REST API client for Fabric workspace and OneLake DFS |
| `FabricSyncEngine` | `src/fabric.py` | Syncs 6 analytics tables from Azure SQL to OneLake CSV |
| `AnalyticsEngine` | `src/fabric.py` | Computes dashboard metrics directly from SQL |

### Fabric Environment Variables

| Variable | Purpose |
|----------|---------|
| `FABRIC_WORKSPACE_ID` | Target Fabric workspace (auto-provisioned by `setup.ps1` Step 6) |
| `FABRIC_ONELAKE_DFS_ENDPOINT` | OneLake DFS endpoint URL (auto-provisioned by `setup.ps1` Step 6) |
| `FABRIC_LAKEHOUSE_NAME` | OneLake lakehouse name (auto-provisioned by `setup.ps1` Step 6) |

### Analytics Provided

- **Pipeline analytics** — Success rates, failure trends, healing effectiveness
- **Governance analytics** — CISO/CTO review verdicts, policy compliance rates
- **Service analytics** — Adoption metrics, status distribution, onboarding velocity
- **Deployment analytics** — Regional distribution, resource group usage
- **Compliance analytics** — Framework score distribution, control pass rates

Authentication to Fabric uses `DefaultAzureCredential` for both the Fabric REST
API scope and OneLake DFS scope.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `AZURE_SQL_CONNECTION_STRING` | Azure SQL Database connection string |
| `COPILOT_MODEL` | Model for Copilot SDK |
| `SESSION_SECRET` | Session middleware secret |
| `ENTRA_CLIENT_ID` | Microsoft Entra ID app client ID |
| `ENTRA_TENANT_ID` | Azure AD tenant ID |
| `ENTRA_CLIENT_SECRET` | Entra ID client secret |
| `ENTRA_REDIRECT_URI` | Auth callback URL |
| `FABRIC_WORKSPACE_ID` | Fabric workspace ID (auto-provisioned by `setup.ps1`) |
| `FABRIC_ONELAKE_DFS_ENDPOINT` | OneLake DFS endpoint (auto-provisioned by `setup.ps1`) |
| `FABRIC_LAKEHOUSE_NAME` | OneLake lakehouse name (auto-provisioned by `setup.ps1`) |
