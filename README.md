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

```powershell
git clone https://github.com/aharsan/CopilotSDKChallenge.git
cd CopilotSDKChallenge
.\scripts\setup.ps1      # provisions Azure SQL, Entra ID, .env, venv, and dependencies
python web_start.py       # open http://localhost:8080
```

The setup script is an interactive wizard that handles everything — Azure resources,
app registration, environment config, Python venv, and dependency installation.
See **[Setup Guide](docs/SETUP.md)** for parameters and options.

> **Already have infrastructure?** Create a `.env` file manually
> ([template](docs/SETUP.md#env-file)), install dependencies with
> `pip install -r requirements.txt`, and run `python web_start.py`.
