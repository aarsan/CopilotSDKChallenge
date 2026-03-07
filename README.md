# InfraForge — AI-Powered Azure Service Onboarding

Enterprise developers are routinely blocked from using Azure services because IT hasn't
vetted, secured, and automated deployment for each service — a manual process involving
Terraform, Bicep, ARM templates, and CI/CD pipelines that takes weeks per service.
**InfraForge eliminates this bottleneck by letting AI do the onboarding.**

Using the GitHub Copilot SDK, InfraForge provides three capabilities:
**1) CISO Agent** — IT and Security define organizational policies through natural language
chat, not JSON or YAML manifests.
**2) AI Service Onboarding** — AI writes production-grade ARM/Bicep templates to security
specifications, validates compliance with IT policy, tests the templates, and versions
everything — no human IaC authoring required.
**3) Template Composition** — Developers build reusable infrastructure templates from
onboarded services: landing zones, web apps, multi-resource setups — without writing code.

AI writes the policies. AI writes the templates. AI tests them. Zero code required.

Built with: Python · FastAPI · GitHub Copilot SDK · Azure SQL · Microsoft Entra ID

---

📖 **[Full Documentation](docs/README.md)** — Problem/solution, prerequisites, setup,
deployment, architecture, and Responsible AI notes

🏗️ **[Architecture Reference](docs/ARCHITECTURE.md)** — Data model, API surface, SDK patterns

🤖 **[Agent Instructions](AGENTS.md)** — Custom agent behavior and tool definitions

🎬 **[Demo Video](#)** — 3-minute walkthrough *(update link after recording)*

📊 **[Presentation Deck](presentations/InfraForge.html)** — Business value and architecture

## Quick Start

```bash
git clone https://github.com/aharsan/CopilotSDKChallenge.git
cd CopilotSDKChallenge
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python web_start.py
# Open http://localhost:8080
```
