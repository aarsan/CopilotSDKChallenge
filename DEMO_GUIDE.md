# InfraForge -- Hackathon Demo Guide

> **For judges:** This guide walks you through the core product flow step by step.
> Follow along in order for the best experience.

---

## Prerequisites

- The app should already be running at **http://localhost:8080**
- You should be signed in (Microsoft Entra ID)

---

## Part 1: Discover the Empty Catalog

### Step 1 -- Navigate to Service Catalog

1. In the left sidebar, click **"Service Catalog"** (second item under Navigation).
2. You'll land on the catalog page with a **stats panel** across the top and an empty table below.

### Step 2 -- Notice the Empty State

Take a look at the stats panel. You should see:

| Stat Card       | Value           |
|-----------------|-----------------|
| Azure Services  | `---`           |
| Cached in System| `---`           |
| Approved        | `---`           |
| Onboarding      | `0`             |
| Sync Status     | **Never synced**|

The table below shows **"No services match your filters"** -- there's nothing in the system yet.

> **Key takeaway:** InfraForge starts as a blank slate. The platform team must explicitly sync and approve services before anyone can provision infrastructure.

---

## Part 2: Sync Azure Services

### Step 3 -- Click the Sync Button

1. On the Sync Status card (rightmost stat card), click the **"Sync"** button.
2. A progress panel appears below the stats showing the sync pipeline:
   - **"Connecting to Azure..."** -- authenticating to Azure ARM API
   - **"Listing Azure resource providers..."** -- scanning your subscription
   - **"Scanned N resource providers"** -- filtering out noise (management types, deprecated resources, etc.)
   - **"Added X / Y services..."** -- inserting discovered services into the InfraForge catalog
   - **"Sync complete!"** -- done

3. You'll see a toast notification: **"Synced! N new services discovered (total total)"**

### Step 4 -- Explore the Discovered Services

After sync completes, the catalog is now populated:

- **Stats panel** updates with real numbers (e.g., 200+ Azure services, all cached, 0 approved)
- The **table** fills with every Azure service type discovered from your subscription
- Each service shows as **"Not Approved"** -- nothing is onboarded yet

### Step 5 -- Try the Filters

Play with the filtering controls:

- **Search bar** -- type "network" or "virtual" to narrow the list
- **Category pills** -- click **"Networking"** to see only networking services. Other categories include Compute, Database, Storage, Security, AI, Monitoring, Messaging, and more.
- **Status pills** -- since all services are "Not Approved", clicking **"Not Approved"** shows them all

> **Key takeaway:** InfraForge discovers real Azure resource types from your subscription via the ARM API, then lets the platform team review and selectively onboard them through a governance pipeline.

---

## Part 3: Onboard a Virtual Network

Now for the main event -- running the full AI-driven onboarding pipeline.

### Step 6 -- Find Virtual Network

1. In the search bar, type **"virtual network"**
2. Find the row: **"Network -- Virtual Networks"** (`Microsoft.Network/virtualNetworks`)
3. It should show status **"Not Approved"**, category **"Networking"**

### Step 7 -- Open the Service Detail Drawer

1. **Click the row** to open the service detail drawer from the right side.
2. The drawer header shows the service name with a close button and an expand toggle.
3. The **meta line** shows:
   - Service ID: `Microsoft.Network/virtualNetworks`
   - Status badge: **"Not Approved"** (red)
   - Category: **Networking**
   - Risk tier: **Medium risk**

### Step 8 -- Start Onboarding

1. Scroll to the **"One-Click Onboarding"** card. You'll see:
   - Description: *"Copilot SDK auto-generates an ARM template, validates against governance policies, deploys to test, and promotes."*
   - A big button: **"Onboard Service"**
2. **Click "Onboard Service"**

### Step 9 -- Watch the Pipeline Overlay

A full-screen **pipeline overlay** opens showing the live onboarding pipeline. This is the heart of InfraForge -- a 12-step AI-powered pipeline:

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

Each step renders as a **flow card** in the overlay with live progress, AI reasoning output, and status indicators.

### Step 10 -- Watch the Drawer Progress

While the overlay is running, you can close it and check the drawer. It shows:

- **"Onboarding In Progress..."** with a progress bar and percentage
- **Pipeline step chips**: Parse → What-If → Deploy → Verify → Policy → Enforce → Cleanup → Approve
- The active step is highlighted, completed steps show checkmarks
- Phase text updates in real-time (e.g., *"Running ARM What-If analysis..."*)
- Resource group name appears when deployment starts

### Step 11 -- Pipeline Completes

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

> **Key takeaway:** One click triggers a fully autonomous pipeline -- AI architecture planning, code generation, governance review, real Azure deployment + testing, auto-healing, and promotion. No human had to write a line of IaC.

---

## Tips for Judges

- **Expand the drawer** -- click the expand toggle (⛶) in the drawer header to see the full detail view
- **View the template** -- click "View Template" on the published version to see the generated ARM JSON
- **Check governance** -- navigate to the Governance page (sidebar) to see the organization standards the pipeline validated against
- **Try Infrastructure Designer** -- click "Infrastructure Designer" in the sidebar to chat with InfraForge's AI agent about generating infrastructure

---

## What You Just Saw

1. **Service Discovery** -- Real Azure resource types synced from the ARM API
2. **Governance-First Onboarding** -- Services start as "Not Approved" and go through a rigorous pipeline
3. **AI-Powered Pipeline** -- Copilot SDK handles planning, generation, review, and testing
4. **Auto-Healing** -- If the template fails validation or deployment, the AI fixes it automatically
5. **Real Azure Deployment** -- Templates are actually deployed to Azure and tested against live resources
6. **Policy Enforcement** -- Azure Policy is generated and tested alongside the template
7. **Version Management** -- Approved templates are versioned and tracked in the catalog
