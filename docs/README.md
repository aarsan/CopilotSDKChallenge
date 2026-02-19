# InfraForge — Self-Service Infrastructure Platform

> **Generate once, reuse forever.** Provision production-ready Azure infrastructure from natural
> language — powered by a catalog of pre-approved templates, the GitHub Copilot SDK, and
> organizational governance built in.

## 🎯 Problem → Solution

### The Problem
Enterprise teams face a painful infrastructure bottleneck:
- **App teams wait days** for platform teams to write Bicep/Terraform
- **Platform teams are overwhelmed** with repetitive "just give me an App Service" requests
- **Every team reinvents patterns** — inconsistent naming, missing tags, insecure defaults
- **No reuse** — templates are written once, then lost in repo sprawl
- **Cost surprises** — teams deploy without knowing the price tag

### The Solution
**InfraForge** is a self-service infrastructure platform that lets business and app teams
provision production-ready cloud infrastructure through natural language — while platform teams
retain full control through:

- 📚 **Approved Template Catalog** — Pre-vetted, tested infrastructure modules that teams reuse
- 🔒 **Policy Engine** — Automated governance checks (tags, naming, security, regions)
- 💰 **Cost Transparency** — Cost estimates before deployment, not after
- 🤖 **AI Composition** — The agent searches the catalog first, generates only as a last resort
- 📦 **Register & Reuse** — New templates get registered back for organization-wide benefit

**The workflow: Search → Compose → Generate (if needed) → Validate → Save → Register**

| Before | After |
|---|---|
| App team files Jira ticket | App team asks InfraForge in plain English |
| Platform team writes Bicep (4-8 hours) | InfraForge finds approved template (30 seconds) |
| Back-and-forth on requirements | AI asks clarifying questions interactively |
| Manual policy review | Automated compliance check |
| Cost surprise after deployment | Cost estimate before provisioning |

---

## 📋 Prerequisites

- **Python 3.9+**
- **GitHub Copilot CLI** installed and authenticated
  - [Installation guide](https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli)
- **GitHub Copilot subscription** (or BYOK configuration)
- **Git** for version control

## 🚀 Setup & Installation

```bash
# 1. Clone the repository
git clone https://github.com/<your-org>/infraforge.git
cd infraforge

# 2. Create and activate a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Verify Copilot CLI
copilot --version

# 5. Run InfraForge
python -m src.main
```

### Environment Variables (Optional)

| Variable | Default | Description |
|---|---|---|
| `COPILOT_MODEL` | `gpt-4.1` | Copilot model to use |
| `COPILOT_LOG_LEVEL` | `warning` | SDK log verbosity |
| `INFRAFORGE_OUTPUT_DIR` | `./output` | Directory for saved files |

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                         User (CLI)                           │
│          "I need a web app with SQL and Key Vault"           │
└───────────────────────┬──────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────┐
│                    InfraForge Agent                           │
│              (src/main.py + src/config.py)                   │
│                                                              │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │              GitHub Copilot SDK (Python)                 │ │
│  │         CopilotClient → Session → Events                │ │
│  └────────────────────┬────────────────────────────────────┘ │
│                       │                                      │
│           ┌───── STEP 1: Search ─────┐                       │
│           ▼                          │                       │
│  ┌─────────────────────┐             │                       │
│  │  Template Catalog   │  Azure SQL  │                       │
│  │  ┌───────────────┐  │             │                       │
│  │  │ catalog_      │  │  Pre-approved, tested              │
│  │  │ templates DB  │  │  modules that teams                │
│  │  │ + bicep src   │  │  reuse across projects             │
│  │  └───────────────┘  │             │                       │
│  └─────────┬───────────┘             │                       │
│            │                         │                       │
│       Found? ──── Yes ──→ Compose from catalog               │
│            │                                                 │
│            No                                                │
│            │                                                 │
│           ┌───── STEP 2: Generate (fallback) ────┐           │
│           ▼                                      │           │
│  ┌─────────────────────────────────────────────┐ │           │
│  │            Generation Tools                  │ │           │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────────┐ │ │           │
│  │  │ Bicep    │ │Terraform │ │ GH Actions / │ │ │           │
│  │  │ Gen      │ │ Gen      │ │ ADO Pipeline │ │ │           │
│  │  └──────────┘ └──────────┘ └──────────────┘ │ │           │
│  └─────────────────────────────────────────────┘ │           │
│            │                                     │           │
│           ┌───── STEP 3: Validate ───────────────┘           │
│           ▼                                                  │
│  ┌─────────────────────────────────────────────┐             │
│  │  ┌──────────────┐  ┌──────────────┐         │             │
│  │  │ Policy       │  │ Cost         │         │             │
│  │  │ Checker      │  │ Estimator    │         │             │
│  │  └──────────────┘  └──────────────┘         │             │
│  └─────────────────────────────────────────────┘             │
│            │                                                 │
│           ┌───── STEP 4: Save & Register ────────┐           │
│           ▼                                      ▼           │
│  ┌──────────────┐                    ┌──────────────┐        │
│  │ Save to File │                    │ Register in  │        │
│  │              │                    │ Catalog      │        │
│  └──────────────┘                    └──────────────┘        │
└───────────────────────┬──────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────┐
│                   Copilot CLI (Server)                        │
│              JSON-RPC │ Model: GPT-4.1                       │
└──────────────────────────────────────────────────────────────┘
```

---

## 🔧 Usage Examples

### Catalog-First Flow (Recommended)
```
You: I need a web app with a SQL database and Key Vault for my project

InfraForge: I found 4 approved templates in the catalog:
  ✓ app-service-linux (98% match) — Linux App Service with managed identity
  ✓ sql-database (95% match) — Azure SQL with TLS 1.2 and firewall rules
  ✓ key-vault (92% match) — Key Vault with RBAC and network ACLs
  ✓ three-tier-web (blueprint) — Pre-wired composition of all three!

  The "three-tier-web" blueprint already assembles these modules with proper
  wiring. Shall I compose from the blueprint, or build a custom composition?
```

### Compose from Catalog
```
You: Use the three-tier-web blueprint for prod in eastus2

InfraForge: Composing from the approved "three-tier-web" blueprint...
  [Shows complete Bicep with all modules wired together]
  Estimated cost: ~$385/mo | Policy: ✓ Compliant
  Want me to save this and run a full policy check?
```

### Register a New Template
```
You: Register this AKS template I just generated — other teams might need it

InfraForge: Registered "aks-cluster" in the catalog with tags: kubernetes, aks, containers
  Future searches for "kubernetes" or "AKS" will find this template.
```

### Generate (Fallback — No Catalog Match)
```
You: Create Terraform for a Databricks workspace with Unity Catalog

InfraForge: No matching templates found in the catalog. Generating from scratch...
  [Generates Terraform HCL]
  Want me to register this as an approved template for future reuse?
```

### CI/CD Pipelines
```
You: Create a GitHub Actions pipeline for deploying Bicep templates with dev, staging, prod
```

### Cost Estimation
```
You: How much would 2 App Services (S1), a SQL Database (S1), Redis (C1), and Key Vault cost?
```

### Policy Compliance
```
You: Check if my resources comply — App Service in westus with no tags and public access
```

---

## 🛡️ Responsible AI (RAI) Notes

### What InfraForge Does
- Generates IaC templates and pipelines based on user descriptions
- Applies security best practices by default (HTTPS, managed identities, private endpoints)
- Validates against governance policies before deployment
- Provides cost transparency before resource creation

### What InfraForge Does NOT Do
- **Does not deploy resources** — It generates templates for human review before deployment
- **Does not store credentials** — All secrets are parameterized, never hardcoded
- **Does not bypass approval gates** — Generated pipelines include manual approvals for production
- **Does not guarantee cost accuracy** — Estimates are approximate; refer to Azure Pricing Calculator

### Human Oversight
- All generated code should be **reviewed by an engineer** before deployment
- Policy compliance checks are advisory — they do not replace organizational review processes
- Cost estimates are approximate and should be validated against actual Azure pricing

### Data Handling
- InfraForge runs locally and does not persist conversation data
- Infrastructure descriptions are sent to the Copilot model for generation
- No customer data, credentials, or PII are stored or transmitted beyond the session

---

## 📁 Project Structure

```
CopilotSDKChallenge/
├── src/
│   ├── __init__.py
│   ├── main.py              # Entry point — interactive CLI
│   ├── config.py             # Configuration & system prompt
│   ├── utils.py              # Helper utilities
│   ├── tools/
│   │   ├── __init__.py       # Tool registry (10 tools)
│   │   ├── catalog_search.py    # Search approved template catalog
│   │   ├── catalog_compose.py   # Compose from existing templates
│   │   ├── catalog_register.py  # Register new templates
│   │   ├── bicep_generator.py   # Generate Bicep (fallback)
│   │   ├── terraform_generator.py
│   │   ├── github_actions_generator.py
│   │   ├── azure_devops_generator.py
│   │   ├── cost_estimator.py
│   │   ├── policy_checker.py
│   │   └── save_output.py
│   └── templates/
│       ├── __init__.py
│       ├── bicep_patterns.py     # Reference patterns for generation
│       ├── terraform_patterns.py
│       └── pipeline_patterns.py
├── catalog/                  # Approved template catalog (DB-backed)
│   ├── bicep/                # Source Bicep files (content stored in DB)
│   │   ├── app-service-linux.bicep
│   │   ├── sql-database.bicep
│   │   ├── key-vault.bicep
│   │   ├── log-analytics.bicep
│   │   ├── storage-account.bicep
│   │   └── blueprints/
│   │       └── three-tier-web.bicep
│   ├── terraform/            # (extensible)
│   └── pipelines/            # (extensible)
├── docs/
│   └── README.md             # This file
├── output/                   # Generated files (gitignored)
├── AGENTS.md                 # Agent custom instructions
├── mcp.json                  # MCP server configuration
├── requirements.txt          # Python dependencies
├── start.py                  # Quick-start launcher
├── test_agent.py             # Non-interactive test script
└── .gitignore
```

---

## 🚢 Deployment

InfraForge runs as a local CLI tool. For team-wide deployment:

1. **Containerize** with Docker for consistent environments
2. **Publish** as an internal PyPI package
3. **Integrate** into developer onboarding workflows
4. **Extend** with organization-specific template patterns

---

## 📬 Feedback

SDK feedback shared in the Copilot SDK Teams channel. See `/docs/sdk-feedback.md` for details.
