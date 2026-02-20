# InfraForge — Technical Documentation

## Architecture Overview

InfraForge is a self-service infrastructure platform that enables enterprise teams to provision
production-ready Azure infrastructure through natural language. It combines a FastAPI backend,
Azure SQL Database for all persistent data, and the GitHub Copilot SDK for AI-driven generation.

```
┌─────────────────────────────────────────────────────────┐
│                    Web Browser (SPA)                     │
│  index.html + app.js + styles.css                       │
│  ─ Service Catalog  ─ Templates  ─ Governance           │
│  ─ Activity Monitor ─ Infrastructure Designer (Chat)    │
└───────────────────────┬─────────────────────────────────┘
                        │ HTTP/WebSocket
┌───────────────────────▼─────────────────────────────────┐
│               FastAPI Application (web.py)               │
│  ─ REST endpoints    ─ WebSocket chat                    │
│  ─ Auth (Entra ID)   ─ Standards API router              │
│  ─ Static files      ─ Deployment orchestration          │
├──────────┬────────────┬──────────────┬──────────────────┤
│ Copilot  │ ARM Gen    │ Standards    │ Policy Validator  │
│ SDK      │ Engine     │ Engine       │                   │
└──────────┴────────────┼──────────────┴──────────────────┘
                        │
              ┌─────────▼──────────┐
              │  Azure SQL Database │
              │  (All persistent    │
              │   data lives here)  │
              └────────────────────┘
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
| DELETE | `/api/catalog/templates/{id}` | Remove a template |

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

```
src/
  web.py              — FastAPI app, all REST/WebSocket endpoints
  database.py         — Azure SQL backend, schema, CRUD functions
  standards.py        — Organization standards engine (SQL-backed)
  standards_api.py    — REST API router for standards CRUD
  auth.py             — Entra ID authentication
  config.py           — Environment configuration
  tools/              — Copilot SDK tool definitions
    arm_generator.py  — ARM template skeleton registry
    ...
static/
  index.html          — SPA shell
  app.js              — Frontend JavaScript
  styles.css          — UI styles
docs/
  TECHNICAL.md        — This file
  README.md           — Project overview
```

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
