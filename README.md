# InfraForge — Self-Service Infrastructure Platform

**InfraForge** enables enterprise teams to provision production-ready Azure infrastructure
through natural language — powered by the GitHub Copilot SDK. Instead of waiting days for
platform teams to hand-write Bicep or Terraform, app teams describe what they need in plain
English and InfraForge handles the rest.

The agent searches a **pre-approved template catalog** first, composes from existing modules,
and falls back to AI generation only when no match exists. Every request passes through an
automated **policy engine** (tags, naming, security, regions) and a **cost estimator** before
anything is deployed. Generated templates are registered back into the catalog for
organization-wide reuse.

**Key capabilities:** catalog-first search, Bicep/Terraform generation, GitHub Actions & Azure
DevOps pipelines, architecture diagrams, design documents, ARM SDK deployment with What-If
preview, cost estimation, policy compliance, and service governance — all through conversation.

Built with: Python · FastAPI · GitHub Copilot SDK · Azure SQL · Microsoft Entra ID

---

📖 **[Full Documentation](docs/README.md)** — Problem/solution, prerequisites, setup,
deployment, architecture, and Responsible AI notes

🏗️ **[Architecture Reference](docs/ARCHITECTURE.md)** — Data model, API surface, SDK patterns

🤖 **[Agent Instructions](AGENTS.md)** — Custom agent behavior and tool definitions

🎬 **[Demo Video](#)** — 3-minute walkthrough *(update link after recording)*

📊 **[Presentation Deck](presentations/InfraForge.pptx)** — Business value and architecture

## Quick Start

```bash
git clone https://github.com/<your-org>/infraforge.git
cd infraforge
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python web_start.py
# Open http://localhost:8080
```
