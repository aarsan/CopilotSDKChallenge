# InfraForge — Architecture Reference

> **Single source of truth** for LLM agents and developers working on this codebase.
> Read this before exploring code. AGENTS.md requires it.

---

## 1. System Overview

InfraForge is an enterprise self-service infrastructure platform. Users describe
infrastructure needs in natural language; the platform searches an approved template
catalog, composes ARM templates from building blocks, validates against governance
policies, estimates costs, and deploys directly to Azure — all without writing IaC
by hand.

### Stack

| Layer        | Technology                          |
|-------------|--------------------------------------|
| Backend      | FastAPI (Python 3.13, uvicorn)      |
| Database     | Azure SQL Database (pyodbc + AAD)   |
| AI Engine    | GitHub Copilot SDK (Python)         |
| Auth         | Microsoft Entra ID (MSAL.js + MSAL Python) |
| Identity IQ  | Work IQ (Microsoft Graph org data)  |
| Analytics    | Fabric IQ (OneLake + Fabric Semantic Models) |
| Frontend     | Vanilla JS SPA (no framework)       |
| Deployment   | ARM SDK (azure-mgmt-resource)       |
| Server Port  | 8080 (configurable via `INFRAFORGE_WEB_PORT`) |

### Key Design Principles

- **Catalog-first** — Always search approved templates before generating from scratch.
- **Governance-first** — Check service approval status before generating any infrastructure.
- **All data in Azure SQL** — No local JSON files for persistent state. Every table uses
  `IF NOT EXISTS` guards for idempotent schema creation.
- **SQL Server syntax** — Use `TOP N` (not `LIMIT`), `COALESCE`, `NVARCHAR`. Never use
  MySQL/PostgreSQL-only syntax.
- **Semantic versioning** — Templates and services track versions with semver strings
  (e.g., `1.2.0`), stored in the `semver` column. Integer `version` is the ordinal
  auto-increment; `semver` is the display version.

---

## 2. Project Structure

```
CopilotSDKChallenge/
├── AGENTS.md                  # Agent behavior instructions (read first)
├── docs/
│   ├── ARCHITECTURE.md        # THIS FILE — technical reference
│   ├── README.md              # Project overview and setup
│   └── TECHNICAL.md           # Data model and standards system
├── src/
│   ├── __init__.py
│   ├── config.py              # All env vars, app settings, SYSTEM_MESSAGE
│   ├── web.py                 # FastAPI app + remaining endpoints (~9800 lines — see Router Map)
│   ├── web_shared.py          # Shared singletons (copilot_client, active_sessions, etc.)
│   ├── database.py            # Azure SQL backend — schema + CRUD (~4600 lines)
│   ├── pipeline.py            # PipelineRunner framework — step execution, healing, finalizers
│   ├── pipeline_helpers.py    # Shared helpers for pipelines (param defaults, healing, tags, etc.)
│   ├── orchestrator.py        # LLM orchestration — template analysis, composition, healing
│   ├── model_router.py        # Task → LLM model routing (see §7)
│   ├── copilot_helpers.py     # copilot_send(), agent activity tracking
│   ├── auth.py                # Entra ID OAuth2 flow (MSAL)
│   ├── azure_sync.py          # Azure Resource Provider sync engine
│   ├── sql_firewall.py        # Auto-detect IP & update SQL firewall on startup
│   ├── template_engine.py     # ARM template composition and dependency wiring
│   ├── agents.py              # Agent definitions (WEB_CHAT_AGENT, TEMPLATE_HEALER, etc.)
│   ├── governance.py          # Governance policy helpers
│   ├── fabric.py              # Microsoft Fabric analytics sync
│   ├── standards.py           # Organization standards engine (SQL-backed)
│   ├── standards_api.py       # REST API router for standards CRUD
│   ├── standards_import.py    # Bulk standards import utility
│   ├── utils.py               # Helpers: save_to_file, extract_code_blocks
│   ├── routers/               # FastAPI routers extracted from web.py
│   │   ├── auth.py            # Auth, model settings, analytics, activity (17 routes)
│   │   ├── admin.py           # Backup/restore, approvals, governance, fabric (21 routes)
│   │   ├── deployment.py      # Deployments, Azure resources, orchestration (12 routes)
│   │   └── ws.py              # WebSocket endpoints: chat, governance, concierge (3 routes)
│   ├── pipelines/             # Pipeline step handlers
│   │   ├── onboarding.py      # Service onboarding pipeline (9 steps)
│   │   ├── deploy.py          # Deployment-specific pipeline steps
│   │   ├── validation.py      # Template validation pipeline
│   │   └── testing.py         # Infrastructure test pipeline
│   ├── tools/                 # Copilot SDK tool definitions (see §6)
│   │   ├── __init__.py        # Tool registry — all imports
│   │   ├── arm_generator.py   # ARM skeleton registry (~21 resource types)
│   │   ├── catalog_search.py  # Search template catalog (DB-backed)
│   │   ├── catalog_compose.py # Compose templates from services (DB-backed)
│   │   ├── catalog_register.py# Register new templates (DB-backed)
│   │   ├── cost_estimator.py  # Cost estimation
│   │   ├── deploy_engine.py   # ARM SDK deployment (azure-mgmt-resource)
│   │   ├── design_document.py # Markdown design document generator
│   │   ├── diagram_generator.py # Mermaid architecture diagrams
│   │   ├── governance_tools.py# Security standards, compliance, policies
│   │   ├── github_publisher.py# GitHub repo creation and PR publishing
│   │   ├── policy_checker.py  # Policy compliance validation
│   │   ├── policy_deployer.py # Azure Policy deployment
│   │   ├── static_policy_validator.py # Static ARM template validator
│   │   ├── ciso_tools.py      # CISO advisory tools
│   │   ├── save_output.py     # File saver utility
│   │   ├── service_catalog.py # Service approval tools
│   │   ├── bicep_generator.py # Bicep generation (delegates to Copilot SDK)
│   │   ├── terraform_generator.py # Terraform generation
│   │   ├── github_actions_generator.py # GitHub Actions YAML
│   │   └── azure_devops_generator.py   # Azure DevOps YAML
│   └── templates/             # Pattern libraries for code generation
│       ├── bicep_patterns.py
│       ├── terraform_patterns.py
│       └── pipeline_patterns.py
├── static/
│   ├── index.html             # SPA shell (~1500 lines)
│   ├── app.js                 # Frontend logic (~14800 lines)
│   ├── styles.css             # All styling (~16400 lines)
│   └── onboarding-docs.html   # Service onboarding documentation page
├── catalog/
│   └── bicep/                 # Source Bicep files (seeded into DB)
│       ├── app-service-linux.bicep
│       ├── sql-database.bicep
│       ├── key-vault.bicep
│       ├── log-analytics.bicep
│       ├── storage-account.bicep
│       └── blueprints/
│           └── three-tier-web.bicep
├── web_start.py               # Web server launcher (preferred)
├── start.py                   # CLI launcher
├── mcp.json                   # MCP server configuration
├── requirements.txt           # Python dependencies
└── .gitignore
```

### Router Map

Routes are split across `web.py` and `src/routers/`:

| Router file | Prefix / Area | Routes | Key endpoints |
|---|---|---|---|
| `routers/auth.py` | Auth, Settings, Analytics | 17 | `/`, `/api/auth/*`, `/api/settings/*`, `/api/agents/*`, `/api/analytics/usage`, `/api/activity` |
| `routers/admin.py` | Admin, Approvals, Governance, Fabric | 21 | `/api/admin/*`, `/api/approvals/*`, `/api/governance/*`, `/api/analytics/dashboard`, `/api/fabric/*` |
| `routers/deployment.py` | Deployments, Azure, Orchestration | 12 | `/api/deployments/*`, `/api/azure/*`, `/api/orchestration/*` |
| `routers/ws.py` | WebSocket chat | 3 | `/ws/chat`, `/ws/governance-chat`, `/ws/concierge-chat` |
| `web.py` (remaining) | Service catalog, templates, compliance, onboarding | ~60 | `/api/catalog/*`, `/api/services/*`, `/api/templates/*` |

### Shared State (`web_shared.py`)

All mutable singletons are in `src/web_shared.py` so both `web.py` and routers share
the same objects:

- `copilot_client` — Singleton `CopilotClient` instance (lazy-init)
- `ensure_copilot_client()` — Initializer with lock
- `active_sessions` — `dict[session_token, {copilot_session, user_context}]`
- `_active_validations` — `dict[service_id, tracker_dict]`
- `_user_context_to_dict()` — UserContext → dict converter

### What's NOT in the repo (intentionally)

- No `debug_*.py`, `test_*.py`, `fix_*.py`, `check_*.py` scripts.
- No `*_old.*` backup files.
- No local JSON mock data files.
- The `output/` directory is gitignored.

---

## 3. Data Model (Azure SQL)

All persistent data lives in Azure SQL Database. Schema is defined in
`database.py::AZURE_SQL_SCHEMA_STATEMENTS` and `standards.py::_STANDARDS_SCHEMA`.
Tables are created automatically on startup via `init_db()`.

### Core Tables

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `services` | Azure service catalog | `id` (resource type), `name`, `category`, `status`, `risk_tier`, `active_version` |
| `service_versions` | Versioned ARM templates per service | `service_id`, `version` (int), `semver` (string), `arm_template`, `status` |
| `service_artifacts` | Approval gate artifacts | `service_id`, `artifact_type` (policy/template), `content` |
| `service_policies` | Per-service policy requirements | `service_id`, `policy_key`, `policy_value` |
| `service_approved_skus` | Allowed SKUs per service | `service_id`, `sku_name` |
| `service_approved_regions` | Allowed regions per service | `service_id`, `region` |
| `catalog_templates` | Composed infrastructure templates | `id`, `name`, `service_ids_json`, `content`, `active_version`, `status`, `template_type` |
| `template_versions` | Version history for templates | `template_id`, `version` (int), `semver` (string), `arm_template`, `status`, `changelog` |
| `deployments` | ARM deployment records | `id`, `service_id`, `status`, `resource_group`, `subscription_id` |
| `approval_requests` | Service approval request lifecycle | `id`, `service_type`, `status`, `business_justification` |
| `user_sessions` | Auth sessions with Entra ID claims | `session_token`, `user_email`, `department`, `cost_center` |
| `chat_messages` | Conversation history per session | `session_token`, `role`, `content` |
| `usage_logs` | Analytics — cost attribution, department tracking | `user_email`, `action`, `department` |
| `projects` | Infrastructure project proposals | `id`, `name`, `description`, `status` |

### Governance Tables

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `org_standards` | Organization governance standards | `id`, `name`, `scope`, `category`, `severity`, `rule_json`, `enabled` |
| `org_standards_history` | Audit trail for standard changes | `standard_id`, `version`, `changed_by` |
| `security_standards` | Machine-readable security rules | `id` (SEC-001..SEC-015), `validation_key`, `validation_value` |
| `governance_policies` | Organization-wide policy rules | `id` (GOV-001..GOV-008), `policy_key`, `policy_value` |
| `compliance_frameworks` | Framework definitions | `id`, `name` (SOC2, CIS Azure, HIPAA) |
| `compliance_controls` | Controls within frameworks | `framework_id`, `control_id`, `description` |
| `compliance_assessments` | Results of compliance checks | `framework_id`, `control_id`, `status` |

### Version Scheme

Both services and templates have a dual version system:

- **`version`** (int) — Auto-incrementing ordinal (1, 2, 3, ...). Used internally.
- **`semver`** (string) — Semantic version for display (`1.0.0`, `1.1.0`, `2.0.0`).
  Computed by `compute_next_semver()` based on `change_type`:
  - `"initial"` → `1.0.0`
  - `"patch"` → bump patch (auto-heal, bugfix)
  - `"minor"` → bump minor (revision, feature)
  - `"major"` → bump major (breaking recompose)

### Service Statuses

```
not_approved → under_review → conditionally_approved → approved
                    ↓
                  denied
```

Plus: `validating`, `validation_failed` (during onboarding pipeline).

### Template Statuses

```
draft → passed → validated → approved (published)
  ↓        ↓         ↓
failed  failed    failed
```

---

## 4. API Surface

All endpoints are in `src/web.py`. The app is a single FastAPI instance on port 8080.

### Auth & Settings

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serve the SPA |
| GET | `/api/version` | App name and version |
| GET | `/api/auth/config` | Entra ID client config for MSAL.js |
| GET | `/api/auth/login` | Initiate Entra ID login |
| GET | `/api/auth/callback` | OAuth2 callback |
| POST | `/api/auth/logout` | Logout |
| GET | `/api/auth/me` | Current user info |
| GET | `/api/settings/model` | Current LLM model |
| GET | `/api/settings/model-routing` | Task→model routing table |
| PUT | `/api/settings/model` | Change active chat model |

### Service Catalog

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/catalog/services` | List all services with hydrated policies/SKUs/regions |
| POST | `/api/catalog/services` | Add a new service |
| PATCH | `/api/catalog/services/{id}` | Update service status/metadata |
| DELETE | `/api/catalog/services/{id}` | Remove a service |
| GET | `/api/catalog/services/approved-for-templates` | Services with active ARM templates |
| GET | `/api/catalog/services/sync` | Trigger Azure resource provider sync |
| GET | `/api/catalog/services/sync/status` | Sync progress (SSE stream) |
| GET | `/api/catalog/services/sync/stats` | Last sync statistics |
| GET | `/api/services/{id}/versions` | List all versions of a service |
| GET | `/api/services/{id}/versions/{ver}` | Get specific version |
| POST | `/api/services/{id}/versions/{ver}/mark-active` | Set active version |
| GET | `/api/services/{id}/artifacts` | Get approval gate artifacts |
| PUT | `/api/services/{id}/artifacts/{type}` | Save an artifact |
| POST | `/api/services/{id}/artifacts/{type}/generate` | Generate artifact via LLM |
| POST | `/api/services/{id}/artifacts/{type}/validate` | Validate an artifact |
| POST | `/api/services/{id}/validate-deployment` | Deploy to Azure for validation |
| POST | `/api/services/{id}/onboard` | Full onboarding pipeline (NDJSON stream) |
| POST | `/api/services/{id}/artifacts/{type}/heal` | Auto-heal a failed artifact |

### Template Catalog

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/catalog/templates` | List all templates |
| POST | `/api/catalog/templates` | Register a template |
| DELETE | `/api/catalog/templates/{id}` | Remove a template |
| POST | `/api/catalog/templates/compose` | Compose from approved services |
| POST | `/api/catalog/templates/compose-from-prompt` | Compose from natural language |
| GET | `/api/catalog/templates/{id}/composition` | Service dependencies with semver |
| GET | `/api/catalog/templates/{id}/versions` | Version history |
| POST | `/api/catalog/templates/{id}/versions` | Create a new version |
| POST | `/api/catalog/templates/{id}/promote` | Promote a version |
| POST | `/api/catalog/templates/{id}/test` | Run structural tests |
| POST | `/api/catalog/templates/{id}/auto-heal` | Auto-heal failed tests |
| POST | `/api/catalog/templates/{id}/recompose` | Recompose with updated services |
| POST | `/api/catalog/templates/{id}/validate` | Full validation pipeline (NDJSON) |
| POST | `/api/catalog/templates/{id}/publish` | Publish to catalog |
| POST | `/api/catalog/templates/{id}/deploy` | Deploy to Azure |
| POST | `/api/catalog/templates/{id}/feedback` | Analyze user feedback for revision |
| POST | `/api/catalog/templates/{id}/revision/policy-check` | Policy check before revision |
| POST | `/api/catalog/templates/{id}/revise` | Apply a revision (add services or code edit) |

### Template Analysis

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/templates/types` | Template type definitions |
| GET | `/api/templates/known-dependencies` | Resource dependency map |
| POST | `/api/templates/analyze-dependencies` | Analyze dependencies for resource types |
| GET | `/api/templates/discover/{resource_type}` | Discover ARM API version for a type |
| GET | `/api/templates/discover-subnets` | Discover existing subnets in a VNET |

### Governance

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/governance/security-standards` | All security standards |
| GET | `/api/governance/compliance-frameworks` | Compliance frameworks + controls |
| GET | `/api/governance/policies` | All governance policies |
| GET | `/api/approvals` | All approval requests |
| GET | `/api/approvals/{id}` | Single approval request |
| POST | `/api/approvals/{id}/review` | Review (approve/deny) a request |

### Standards API (mounted via `standards_api.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/standards` | List (filter: `?category=`, `?enabled_only=`) |
| POST | `/api/standards` | Create |
| GET | `/api/standards/categories` | Distinct categories |
| GET | `/api/standards/{id}` | Get one |
| PUT | `/api/standards/{id}` | Update (creates version history) |
| DELETE | `/api/standards/{id}` | Delete + history |
| GET | `/api/standards/{id}/history` | Version history |
| GET | `/api/standards/for-service/{service_id}` | Standards matching a service |
| GET | `/api/standards/context/policy/{service_id}` | Policy prompt context |
| GET | `/api/standards/context/arm/{service_id}` | ARM prompt context |

### Deployments & Activity

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/deployments` | All deployment records |
| GET | `/api/deployments/{id}` | Single deployment |
| GET | `/api/deployments/{id}/stream` | Deployment progress (SSE) |
| GET | `/api/activity` | Activity feed |
| GET | `/api/analytics/usage` | Usage analytics |

### Orchestration

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/orchestration/processes` | Active orchestration processes |
| GET | `/api/orchestration/processes/{id}` | Process detail |
| GET | `/api/orchestration/processes/{id}/playbook` | Process playbook |

### WebSocket

| Path | Description |
|------|-------------|
| `ws://localhost:8080/ws/chat` | Infrastructure Designer chat (Copilot SDK agent) |

---

## 5. Frontend Architecture

The frontend is a vanilla JavaScript SPA with no build step.

### Files

| File | Lines | Purpose |
|------|-------|---------|
| `index.html` | ~940 | SPA shell with all page containers, modals, drawers |
| `app.js` | ~6200 | All application logic, API calls, rendering |
| `styles.css` | ~7200 | All styling (dark theme, component styles) |

### Cache Busting

Static files are loaded with a version query parameter: `app.js?v=66`.
**Bump this version** in `index.html` after every change to JS or CSS.
Current cache version: **v66**.

### Key Patterns

- **Navigation**: `navigateTo(page)` — shows/hides `.page` divs, updates nav state.
- **Data loading**: `loadAllData()` — fetches services, templates, approvals in parallel.
- **Template detail**: Full-page overlay drawer (`#template-detail-drawer`) with sidebar
  showing composition info and version history.
- **Service detail**: Drawer (`#service-detail-drawer`) with approval gates and artifacts.
- **Validation**: NDJSON streaming via `fetch()` with `ReadableStream`. Progress is tracked
  in `_activeTemplateValidations` global, which persists across panel close/reopen.
- **Chat**: WebSocket connection to `/ws/chat` with markdown rendering and tool call display.
- **HTML escaping**: All user content is escaped via `escapeHtml()` before `innerHTML`.

### CSS Naming Conventions

| Prefix | Scope |
|--------|-------|
| `tmpl-*` | Template-related components |
| `comp-*` | Composition sidebar components |
| `svc-*` | Service catalog components |
| `ver-*` | Version pipeline components |
| `nav-*` | Navigation components |
| `stat-*` | Dashboard stat cards |

---

## 6. Copilot SDK Tools

Tools are defined in `src/tools/` and registered via `src/tools/__init__.py`.
Each tool uses `@define_tool` from the Copilot SDK with Pydantic input models.

### Registered Tools (26 total)

| Tool | File | Data Source |
|------|------|-------------|
| `search_template_catalog` | catalog_search.py | Database |
| `compose_from_catalog` | catalog_compose.py | Database |
| `register_template` | catalog_register.py | Database |
| `generate_bicep` | bicep_generator.py | Copilot SDK |
| `generate_terraform` | terraform_generator.py | Copilot SDK |
| `generate_github_actions_pipeline` | github_actions_generator.py | Patterns |
| `generate_azure_devops_pipeline` | azure_devops_generator.py | Patterns |
| `generate_architecture_diagram` | diagram_generator.py | Copilot SDK |
| `generate_design_document` | design_document.py | Copilot SDK |
| `estimate_azure_cost` | cost_estimator.py | **Hard-coded** (see §10) |
| `check_policy_compliance` | policy_checker.py | Database |
| `save_output_to_file` | save_output.py | Local filesystem |
| `publish_to_github` | github_publisher.py | GitHub API |
| `check_service_approval` | service_catalog.py | Database |
| `request_service_approval` | service_catalog.py | Database |
| `list_approved_services` | service_catalog.py | Database |
| `get_approval_request_status` | service_catalog.py | Database |
| `review_approval_request` | service_catalog.py | Database |
| `list_security_standards` | governance_tools.py | Database |
| `list_compliance_frameworks` | governance_tools.py | Database |
| `list_governance_policies` | governance_tools.py | Database |
| `validate_deployment` | deploy_engine.py | Azure ARM SDK |
| `deploy_infrastructure` | deploy_engine.py | Azure ARM SDK |
| `get_deployment_status` | deploy_engine.py | Azure ARM SDK + Database |

---

## 7. Model Router

`src/model_router.py` routes each LLM task to the optimal model. This is separate
from the user's chat model preference.

### Task → Model Mapping

| Task | Model | Rationale |
|------|-------|-----------|
| `PLANNING` | o3-mini | Deep reasoning for architecture decisions |
| `VALIDATION_ANALYSIS` | o3-mini | Reasoning about errors and fixes |
| `CODE_GENERATION` | claude-sonnet-4 | Precise ARM/Bicep/Terraform generation |
| `POLICY_GENERATION` | claude-sonnet-4 | Precise policy JSON structure |
| `CODE_FIXING` | gpt-4.1 | Surgical template healing |
| `CHAT` | (user-selected) | Interactive conversation |
| `QUICK_CLASSIFY` | gpt-4.1-nano | Fast classification and routing |
| `DESIGN_DOCUMENT` | gpt-4.1 | Clear technical prose |

### Task Enum

```python
from src.model_router import Task

Task.PLANNING            # NOT "Task.GENERATION"
Task.CODE_GENERATION     # The correct enum for code gen
Task.CODE_FIXING
Task.POLICY_GENERATION
Task.VALIDATION_ANALYSIS
Task.CHAT
Task.QUICK_CLASSIFY
Task.DESIGN_DOCUMENT
```

---

## 8. Copilot SDK Patterns

### Session API

```python
from copilot import CopilotClient, CopilotSession

# Creating a session
session: CopilotSession = await copilot_client.create_session(model=model_id)

# Event handling — session.on() returns an UNSUBSCRIBE function
unsub = session.on(on_event)
try:
    response = await asyncio.wait_for(session.send_message(...), timeout=90)
finally:
    unsub()  # Always clean up
```

**CRITICAL**: The correct API is `session.on(callback)` which returns an unsubscribe
function. There is **no** `session.on_event()` method. Always use the pattern above.

---

## 9. Template Revision Flow

When users request changes to a template, there are two paths:

### Path 1: Add Services (`should_recompose=True`)
User asks for new resource types → recompose the template with additional services.

### Path 2: Modify Existing (`needs_code_edit=True`)
User asks to change existing resources (reduce count, change SKU, modify config) →
`apply_template_code_edit()` sends current ARM JSON + instruction to LLM for direct editing.

### Detection

`analyze_template_feedback()` in orchestrator.py classifies the request:
1. LLM analysis (primary) — returns `category: "add_services"` or `"modify_existing"`
2. Heuristic fallback — detects modification-signal words ("reduce", "should be",
   "too many", "change", "provisioning 2") and routes to code edit.

---

## 10. Known Hard-coded Data

### Cost Estimator (`cost_estimator.py`)

Contains ~40 hard-coded Azure price points in `AZURE_PRICING` dict. This is a **known
limitation** — prices are approximate and not sourced from the Azure Retail Prices API
or the database. Environment multipliers (dev=0.5×, staging=0.75×, prod=1.0×) are
also static.

### ARM Skeleton Registry (`arm_generator.py`)

~21 registered ARM skeleton generator functions producing full ARM template dicts for
common Azure resource types. These are intentional built-in fallbacks for when the
Copilot SDK is unavailable.

### Resource Dependency Map (`orchestrator.py`)

`RESOURCE_DEPENDENCIES` dict maps Azure resource types to their dependencies. This is
hard-coded as a performance optimization — looking up dependencies in the DB for every
composition would add latency.

### Category Inference

Category inference uses `NAMESPACE_CATEGORY_MAP` from `azure_sync.py` — a ~55-entry
dict mapping Azure provider namespaces to categories. The orchestrator's `_infer_category`
delegates to this map (unified, no longer duplicated).

---

## 11. Server Management

### Start the server

```powershell
# Preferred method (detached, won't die when terminal closes):
$env:PYTHONIOENCODING="utf-8"
Start-Process -FilePath ".\.venv\Scripts\python.exe" -ArgumentList "web_start.py" `
  -WorkingDirectory "C:\Users\aharsan\projects\CopilotSDKChallenge" `
  -NoNewWindow -RedirectStandardOutput "server.log" -RedirectStandardError "server_err.log"
```

### Stop the server

```powershell
Get-Process -Name python -ErrorAction SilentlyContinue | Stop-Process -Force
```

### Restart pattern (full)

```powershell
Get-Process -Name python -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
$env:PYTHONIOENCODING="utf-8"
Start-Process -FilePath ".\.venv\Scripts\python.exe" -ArgumentList "web_start.py" `
  -WorkingDirectory "C:\Users\aharsan\projects\CopilotSDKChallenge" `
  -NoNewWindow -RedirectStandardOutput "server.log" -RedirectStandardError "server_err.log"
Start-Sleep -Seconds 5
(Invoke-WebRequest -Uri http://localhost:8080/ -UseBasicParsing).StatusCode
```

---

## 12. Development Conventions

### SQL

- Always use `TOP N` — never `LIMIT` (SQL Server)
- Use parameterized queries with `?` placeholders
- JSON columns end in `_json` suffix and are parsed in Python

### Versioning

- Templates and services use `semver` column for display
- Integer `version` is for ordering and internal references
- Use `compute_next_semver(current, change_type)` for version bumps

### Git

- Conventional commits: `fix:`, `feat:`, `refactor:`, `chore:`, `docs:`
- Branch per change: `fix/description`, `feat/description`, `chore/description`
- Merge with `--no-ff` to preserve branch history
- Commit after every logical change

### Frontend

- Bump `?v=N` in `index.html` after every JS/CSS change
- Run `node --check static/app.js` before committing
- Use `escapeHtml()` for all user-generated content in innerHTML

### Python

- Server restart after every backend change
- Check `server_err.log` for import/syntax errors after restart
- Use `Task.CODE_GENERATION` not `Task.GENERATION` (the latter doesn't exist)

---

## 13. Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AZURE_SQL_CONNECTION_STRING` | Yes | — | Azure SQL Database connection string |
| `COPILOT_MODEL` | No | `gpt-4.1` | Default Copilot model |
| `COPILOT_LOG_LEVEL` | No | `warning` | SDK log verbosity |
| `SESSION_SECRET` | No | dev default | Session middleware secret |
| `ENTRA_CLIENT_ID` | No | — | Microsoft Entra ID app client ID |
| `ENTRA_TENANT_ID` | No | — | Azure AD tenant ID |
| `ENTRA_CLIENT_SECRET` | No | — | Entra ID client secret |
| `ENTRA_REDIRECT_URI` | No | localhost:8080 | Auth callback URL |
| `GITHUB_TOKEN` | No | — | GitHub API token for publishing |
| `GITHUB_ORG` | No | — | GitHub org for repo creation |
| `INFRAFORGE_OUTPUT_DIR` | No | `./output` | Directory for saved files |
| `INFRAFORGE_WEB_HOST` | No | `0.0.0.0` | Server bind host |
| `INFRAFORGE_WEB_PORT` | No | `8080` | Server port |
| `FABRIC_WORKSPACE_ID` | No | — | Microsoft Fabric workspace ID |
| `FABRIC_ONELAKE_DFS_ENDPOINT` | No | — | OneLake DFS endpoint URL |
| `FABRIC_LAKEHOUSE_NAME` | No | — | OneLake lakehouse name |

---

## 14. Entra ID — App Registration & Authentication

InfraForge authenticates users via Microsoft Entra ID using the OAuth2 authorization
code flow. The setup script (`scripts/setup.ps1` Step 3) creates the required App
Registration automatically.

### App Registration Configuration

| Setting | Value |
|---------|-------|
| Display Name | `InfraForge` |
| Sign-in audience | Single tenant (org directory only) |
| Redirect URI | `http://localhost:8080/api/auth/callback` (Web) |
| Client Secret | Auto-generated, 1-year expiry |
| Optional Claims (ID token) | `email`, `upn` |
| Group Claims | `SecurityGroup` — emitted in both ID and access tokens |

### Authentication Flow

```
                                ┌─────────────────────────┐
                                │   Microsoft Entra ID     │
                                │   ┌───────────────────┐  │
   ┌──────────────┐   1. Auth   │   │ App Registration   │  │
   │  Browser     │─────────────│──▶│ ENTRA_CLIENT_ID    │  │
   │  (MSAL.js)   │   request   │   │ + Client Secret    │  │
   │              │◀────────────│───│ + Redirect URI     │  │
   │              │ 2. Code +   │   │ + Group Claims     │  │
   │              │    redirect  │   └───────────────────┘  │
   └──────┬───────┘             └─────────────────────────┘
          │ 3. POST /api/auth/callback (auth code)
          ▼
   ┌──────────────────────────────────────────────────┐
   │  FastAPI Backend (src/auth.py)                    │
   │                                                   │
   │  4. MSAL ConfidentialClientApplication            │
   │     acquire_token_by_authorization_code()         │
   │     → ID token + Access token                     │
   │                                                   │
   │  5. Microsoft Graph API enrichment (Work IQ)      │
   │     GET /me → job title, department, cost center  │
   │     GET /me/manager → manager display name        │
   │                                                   │
   │  6. Build UserContext (dataclass)                  │
   │     → user_id, email, department, cost_center,    │
   │       manager, groups, roles, is_platform_team    │
   │                                                   │
   │  7. Store session in Azure SQL (user_sessions)    │
   └──────────────────────────────────────────────────┘
```

### Key Auth Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /api/auth/config` | Returns Entra ID client config for MSAL.js (client ID, tenant, scopes) |
| `GET /api/auth/login` | Initiates OAuth2 login (requires Entra ID) |
| `GET /api/auth/callback` | OAuth2 redirect — exchanges code for tokens |
| `POST /api/auth/logout` | Clears session |
| `GET /api/auth/me` | Returns current user context |

### Work IQ — Identity Intelligence

Entra ID provides the foundation for **Work IQ**, which enriches every user session
with organizational context from Microsoft Graph:

- **Identity-aware tagging** — Resources are automatically tagged with the user's
  email, department, cost center, and manager
- **Role-based access** — PlatformTeam group membership grants full catalog access;
  standard users work with approved templates
- **Cost attribution** — Every action logged in `usage_logs` with department/cost center
- **Approval routing** — Design documents routed based on manager chain from Graph API

### Required Permissions

| Permission | Scope | Purpose |
|------------|-------|---------|
| App registration creation | Entra ID | Setup script creates the app |
| Admin consent for group claims | Entra ID | SecurityGroup claims in tokens |
| `User.Read` | Delegated (MS Graph) | Read the signed-in user's profile |
| `User.ReadBasic.All` | Delegated (MS Graph) | Read manager chain |

---

## 15. Fabric IQ — Enterprise Analytics

InfraForge integrates with Microsoft Fabric to provide cross-organization analytics
via OneLake. The `src/fabric.py` module implements the sync engine, REST client,
and analytics computations.

### Data Pipeline Architecture

```
┌───────────────────────┐       ┌──────────────────────────────────────┐
│   Azure SQL (OLTP)    │       │      Microsoft Fabric (Fabric IQ)    │
│                       │       │                                      │
│ ┌───────────────────┐ │  ETL  │ ┌──────────────────────────────────┐ │
│ │ pipeline_runs     │─│──────▶│ │  OneLake Lakehouse               │ │
│ │ governance_reviews│─│──────▶│ │  (FABRIC_ONELAKE_DFS_ENDPOINT)   │ │
│ │ services          │─│──────▶│ │                                  │ │
│ │ catalog_templates │─│──────▶│ │  Tables/                         │ │
│ │ deployments       │─│──────▶│ │    pipeline_runs.csv             │ │
│ │ compliance_assess │─│──────▶│ │    governance_reviews.csv        │ │
│ └───────────────────┘ │ Sync  │ │    service_catalog.csv           │ │
│                       │       │ │    template_catalog.csv          │ │
│                       │       │ │    deployments.csv               │ │
│                       │       │ │    compliance_assessments.csv    │ │
│                       │       │ └───────────────┬──────────────────┘ │
│                       │       │                 ▼                    │
│                       │       │ ┌──────────────────────────────────┐ │
│                       │       │ │  Power BI / Semantic Models      │ │
│                       │       │ │  ─ Pipeline success dashboards   │ │
│                       │       │ │  ─ Governance compliance trends  │ │
│                       │       │ │  ─ Cost attribution by dept      │ │
│                       │       │ │  ─ Service adoption metrics      │ │
│                       │       │ └──────────────────────────────────┘ │
│                       │       └──────────────────────────────────────┘
└───────────────────────┘
```

### Components

| Class | Purpose |
|-------|---------|
| `FabricClient` | REST client for Fabric workspace management and OneLake DFS file operations |
| `FabricSyncEngine` | ETL engine — reads 6 tables from Azure SQL and writes CSV to OneLake |
| `AnalyticsEngine` | Computes real-time dashboard analytics directly from SQL |

### Analytics Capabilities

| Domain | Metrics |
|--------|---------|
| Pipeline | Success/failure rates, healing effectiveness, execution trends |
| Governance | CISO/CTO review verdicts, policy compliance rates |
| Services | Adoption metrics, status distribution, onboarding velocity |
| Deployments | Regional distribution, resource group usage, ARM SDK outcomes |
| Compliance | Framework scores (SOC2, CIS, HIPAA), control pass rates |

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/fabric/health` | Fabric connectivity status |
| `POST` | `/api/fabric/sync` | Trigger ETL sync to OneLake |
| `GET` | `/api/analytics/dashboard` | Real-time analytics dashboard data |

### Authentication

Fabric uses `DefaultAzureCredential` (the same credential used for Azure SQL)
to authenticate to both the Fabric REST API and OneLake DFS endpoints. No additional
app registration is required — the service principal or managed identity needs
Fabric workspace Contributor access.
