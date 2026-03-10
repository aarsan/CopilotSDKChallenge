# InfraForge -- Hackathon Demo Guide

> **For judges:** This guide walks you through the core product flow step by step.
> Follow along in order for the best experience. You'll see the Copilot SDK in action
> within the first few minutes.

---

## Prerequisites

- The app should already be running at **http://localhost:8080**
- You should be signed in (Microsoft Entra ID)

---

## Part 1: Set Up the Catalog

This section gets the catalog populated so we can jump straight into the AI features.

### Step 1 -- Navigate and Sync

1. In the left sidebar, click **"Service Catalog"** (second item under Navigation).
2. You'll see an empty catalog -- stats show `---` across the board and the table reads
   **"No services match your filters."**
3. On the **Sync Status** card (rightmost stat card), click the **"Sync"** button.
4. A progress panel shows the live sync pipeline:
   - **"Connecting to Azure..."** → **"Listing Azure resource providers..."** →
     **"Scanned N providers"** → **"Added X / Y services..."** → **"Sync complete!"**
5. The stats update with real numbers (200+ Azure services, 0 approved) and every
   discovered service appears in the table as **"Not Approved."**

> **Key takeaway:** InfraForge discovers real Azure resource types from your subscription
> via the ARM API, then lets the platform team selectively approve and onboard them
> through an AI-powered governance pipeline. Nothing is approved by default.

---

## Part 2: Copilot in Action -- Onboard a Service

This is the core Copilot SDK feature. One click triggers a fully autonomous 12-step
AI pipeline that plans architecture, generates infrastructure code, runs governance
reviews, deploys to Azure, tests the live resources, auto-heals failures, and promotes
the result -- all without a human writing a single line of IaC.

### Step 2 -- Find Virtual Network

1. In the search bar, type **"virtual network"**
2. Find the row: **"Network -- Virtual Networks"** (`Microsoft.Network/virtualNetworks`)
3. It should show status **"Not Approved"**, category **"Networking"**

### Step 3 -- Open the Service Detail Drawer

1. **Click the row** to open the service detail drawer from the right side.
2. The drawer header shows the service name with a close button and an expand toggle.
3. The **meta line** shows:
   - Service ID: `Microsoft.Network/virtualNetworks`
   - Status badge: **"Not Approved"** (red)
   - Category: **Networking**
   - Risk tier: **Medium risk**

### Step 4 -- Start Onboarding

1. Scroll to the **"One-Click Onboarding"** card. You'll see:
   - Description: *"Copilot SDK auto-generates an ARM template, validates against governance policies, deploys to test, and promotes."*
   - A big button: **"Onboard Service"**
2. **Click "Onboard Service"**

### Step 5 -- Watch the Pipeline (the fun part!)

A full-screen **pipeline overlay** opens showing the live AI pipeline. This is the heart
of InfraForge -- spend a moment here and watch the AI work.

#### What to look for in the overlay

- **Flow cards** -- Each of the 12 steps renders as a card. The active card pulses and
  streams live output from the Copilot SDK.
- **AI reasoning** -- Expand any card to see the LLM's chain-of-thought: why it chose
  certain parameters, how it interpreted organization standards, and what trade-offs it
  considered.
- **Model routing** -- Step 1 (Pipeline Setup) shows which LLM model handles each task
  type (planning, generation, fixing, validation). InfraForge routes to the best model
  for each job.
- **Auto-healing in action** -- If any step fails (template validation, ARM deployment,
  policy checks), the pipeline automatically sends the error back to the AI, which fixes
  the template and retries. Watch for retry indicators on steps 7-8.
- **Live Azure deployment** -- During step 8, you'll see real ARM deployment progress
  with resource-level status updates.

#### The 12-step pipeline

| Step | Name | What Happens |
|------|------|-------------|
| 1 | **Pipeline Setup** | Configures model routing (which LLM handles planning vs. generation vs. fixing) |
| 2 | **Dependency Validation Gate** | Checks if VNet has dependencies that need onboarding first (e.g., NSG, Route Table) |
| 3 | **Analyzing Standards** | Scans organization standards that apply to `Microsoft.Network/*` -- things like "No Public Access by Default", "Required Resource Tags", "Allowed Deployment Regions" |
| 4 | **AI Planning Architecture** | Copilot SDK reasons about the architecture -- resources, security, parameters, compliance |
| 5 | **Generating ARM Template** | Copilot SDK generates a production-ready ARM JSON template |
| 6 | **Generating Azure Policy** | Copilot SDK generates an Azure Policy to enforce compliance for this resource type |
| 7 | **Governance Review** | Parallel CISO (security) and CTO (architecture) reviews via Copilot SDK. Can block, conditionally approve, or fully approve. If blocked, the pipeline auto-heals the template. |
| 8 | **Validate & Deploy** | Multi-phase: static policy checks, ARM What-If dry run, deploy to isolated test resource group, verify resources, runtime policy testing. **Includes auto-healing** -- if anything fails, the AI fixes the template and retries (up to 5 attempts). |
| 9 | **Infrastructure Tests** | Copilot SDK generates and runs Python smoke tests against the live deployed resources |
| 10 | **Deploying Policy** | Deploys the generated Azure Policy to Azure |
| 11 | **Cleaning Up** | Deletes the temporary validation resource group and policy |
| 12 | **Publishing Version** | Promotes the validated template to **v1.0.0 Approved** status |

#### Alternative view: the drawer progress bar

You don't have to keep the overlay open the whole time. Close it and check the **drawer**
for a compact progress view:

- **"Onboarding In Progress..."** with a progress bar and percentage
- **Pipeline step chips**: Parse → What-If → Deploy → Verify → Policy → Enforce → Cleanup → Approve
- The active step is highlighted, completed steps show checkmarks
- Phase text updates in real-time (e.g., *"Running ARM What-If analysis..."*)
- Resource group name appears when deployment starts

> **Tip:** You can re-open the full pipeline overlay at any time by clicking
> the pipeline run in the drawer.

### Step 6 -- Pipeline Completes

When the pipeline finishes successfully:

- Toast: **"Virtual Networks v1.0.0 approved!"**
- The service status changes from "Not Approved" → **"Approved"** (green badge)
- The stats panel updates: Approved count goes from 0 to 1
- The drawer shows:
  - **"Onboarded -- v1.0.0"** card with *"Validated ARM template approved for deployment"*
  - **Published Versions** section with v1.0.0 details (template size, deployment tracking, download button)
  - **Pipeline Runs** section with the completed run
  - **Governance Reviews** section showing CISO and CTO verdicts
  - Options to **"View Template"** or **"Download"** the ARM JSON

> **Key takeaway:** One click triggers a fully autonomous pipeline -- AI architecture
> planning, code generation, governance review, real Azure deployment + testing,
> auto-healing, and promotion. No human had to write a line of IaC. Every step is
> powered by the Copilot SDK.

---

## Part 3: Infrastructure Designer

Now that a service is onboarded, let's use the Copilot SDK chat agent.

### Step 7 -- Open the Infrastructure Designer

1. In the left sidebar, click **"Infrastructure Designer"** (the first item under Navigation).
2. You'll see a chat interface with **suggestion chips** at the bottom.

### Step 8 -- Try Designing Infrastructure

1. Click the suggestion chip **"Design a web app"** -- or type your own prompt:
   > *I need a production web app with SQL database, Key Vault, and monitoring*
2. Watch the **tool activity spinners** as the Copilot SDK agent works in real time:
   - **"Checking service approval…"** -- verifies each Azure service against the governance catalog
   - **"Searching template catalog…"** -- looks for existing approved templates
   - **"Generating Bicep template…"** or **"Composing from catalog…"** -- builds the infrastructure
3. The agent responds with a full architecture: generated code, design decisions, cost considerations, and follow-up suggestions.

### Step 9 -- Check Service Approval

1. Click **"Check service approval"** -- or ask:
   > *Is Azure Kubernetes Service approved for use?*
2. The agent calls `check_service_approval` and returns the real governance status -- it doesn't hallucinate, it checks the actual catalog.

> **Key takeaway:** The Copilot SDK agent has access to real governance data and infrastructure tools. It checks approvals, searches catalogs, generates code, and estimates costs -- all through natural conversation.

---

## Part 4: Work IQ — M365 Organizational Intelligence

InfraForge connects to **Microsoft Work IQ** to query your organization's M365 data
(emails, meetings, SharePoint, OneDrive, Teams) via natural language — directly from
the Infrastructure Designer chat.

### Step 10 -- Try the Sidebar Shortcuts

1. In the left sidebar, find the **"🏢 Org Intelligence"** section (below "Review").
2. Click **"🔍 Search Org Knowledge"**.
3. This auto-navigates to the Infrastructure Designer chat and sends a prompt asking
   the AI to search your organization's M365 data.
4. Watch the **tool activity spinner** — it shows **"🏢 Searching org knowledge via Work IQ"**
   while the agent queries your tenant.
5. The agent returns real results from your M365 data: architecture discussions from
   emails, meeting notes, SharePoint documents, and Teams messages.

### Step 11 -- Find Subject Matter Experts

1. In the chat, click the **cyan suggestion chip** labeled **"👥 Find org experts"**
   (or use the sidebar shortcut **"👥 Find Experts"**).
2. The agent calls `find_subject_matter_experts` to search across your M365 activity
   for people who have worked on similar infrastructure topics.
3. Results include names, roles, and context about their relevant experience — pulled
   from emails, meetings, and document collaboration history.

### Step 12 -- Find Related Documents

1. Click the **"📄 Find org docs"** suggestion chip or sidebar shortcut.
2. The agent searches SharePoint and OneDrive for architecture specs, runbooks, and
   design documents related to the infrastructure topic.
3. Results include document titles, locations, and summaries with links.

> **Key takeaway:** Work IQ enriches infrastructure decisions with organizational
> context — the agent finds prior art, identifies experts, and discovers related
> documentation from your M365 tenant, all through natural language. No manual search
> across SharePoint, Outlook, and Teams required.

---

## Tips for Judges

- **Expand the drawer** -- click the expand toggle (⛶) in the drawer header to see the full detail view
- **View the template** -- click "View Template" on the published version to see the generated ARM JSON
- **Check governance** -- navigate to the Governance page (sidebar) to see the organization standards the pipeline validated against
- **Try the filters** -- back in the Service Catalog, try the search bar, category pills, and status pills to explore the full catalog
- **Replay a pipeline** -- in the service drawer, click a past pipeline run to replay the full overlay visualization

---

## What You Just Saw

1. **Service Discovery** -- Real Azure resource types synced from the ARM API
2. **AI-Powered Onboarding** -- One click triggers a 12-step Copilot SDK pipeline: planning, generation, governance review, deployment, testing, and promotion
3. **Auto-Healing** -- If the template fails validation or deployment, the AI fixes it automatically
4. **Real Azure Deployment** -- Templates are actually deployed to Azure and tested against live resources
5. **Policy Enforcement** -- Azure Policy is generated and tested alongside the template
6. **Copilot SDK Chat Agent** -- An AI assistant that checks governance status, searches catalogs, and generates infrastructure through natural conversation
7. **Version Management** -- Approved templates are versioned and tracked in the catalog
8. **M365 Org Intelligence** -- Work IQ searches your tenant's emails, meetings, docs, and Teams for prior architecture context, related documents, and subject matter experts
