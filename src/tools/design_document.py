"""
Design document generator tool.
Produces a stakeholder-ready approval artifact with architecture summary,
diagram, cost estimate, compliance status, and decision log.
"""

from datetime import datetime, timezone
from pydantic import BaseModel, Field
from copilot import define_tool


class DesignResource(BaseModel):
    name: str = Field(description="Resource display name, e.g. 'Order API App Service'")
    type: str = Field(description="Azure resource type, e.g. 'App Service', 'SQL Database', 'Key Vault'")
    sku: str = Field(default="", description="SKU/tier, e.g. 'S1', 'P1v3', 'Standard'")
    region: str = Field(default="", description="Azure region, e.g. 'eastus2'")
    purpose: str = Field(default="", description="Brief description of why this resource is needed")


class DesignDecision(BaseModel):
    topic: str = Field(description="Decision topic, e.g. 'Database Engine', 'Authentication', 'Networking'")
    decision: str = Field(description="What was decided, e.g. 'Use Azure SQL over Cosmos DB'")
    rationale: str = Field(description="Why this decision was made, e.g. 'Relational data model, team SQL expertise, cost predictability'")
    alternatives_considered: str = Field(default="", description="Other options that were evaluated")


class ComplianceItem(BaseModel):
    check: str = Field(description="Policy check name, e.g. 'Required Tags', 'HTTPS Only', 'Managed Identity'")
    status: str = Field(description="'pass', 'fail', or 'warning'")
    details: str = Field(default="", description="Additional context about the result")


class CostLineItem(BaseModel):
    resource: str = Field(description="Resource name")
    sku: str = Field(default="", description="SKU/tier")
    monthly_cost: float = Field(description="Estimated monthly cost in USD")
    notes: str = Field(default="", description="Cost notes, e.g. 'Pay-as-you-go', 'Reserved pricing available'")


class GenerateDesignDocParams(BaseModel):
    project_name: str = Field(description="Project or application name, e.g. 'Order Management System'")
    environment: str = Field(default="production", description="Target environment: 'dev', 'staging', 'production'")
    requested_by: str = Field(default="", description="Name or team that requested the infrastructure")
    business_justification: str = Field(
        description="Brief business justification for the infrastructure, e.g. 'New customer-facing order management portal to replace legacy system'"
    )
    architecture_summary: str = Field(
        description=(
            "High-level architecture description covering the key components, "
            "data flow, and integration points. 2-4 sentences."
        )
    )
    resources: list[DesignResource] = Field(description="List of infrastructure resources in the design")
    decisions: list[DesignDecision] = Field(
        default=[],
        description="Key architectural decisions and their rationale (ADR-style)"
    )
    compliance_results: list[ComplianceItem] = Field(
        default=[],
        description="Results from policy compliance checks"
    )
    cost_items: list[CostLineItem] = Field(
        default=[],
        description="Cost estimate breakdown per resource"
    )
    mermaid_diagram: str = Field(
        default="",
        description="Mermaid diagram code (from generate_architecture_diagram). Include the full mermaid code block."
    )
    security_notes: list[str] = Field(
        default=[],
        description="Security considerations and mitigations, e.g. 'All data encrypted at rest with service-managed keys'"
    )
    risks: list[str] = Field(
        default=[],
        description="Known risks or concerns, e.g. 'Single-region deployment — no DR failover'"
    )
    next_steps: list[str] = Field(
        default=[],
        description="Recommended next steps after approval, e.g. 'Deploy to dev environment', 'Configure DNS records'"
    )
    catalog_templates_used: list[str] = Field(
        default=[],
        description="List of approved catalog template IDs used in this design, e.g. ['app-service-linux', 'sql-database']"
    )


@define_tool(description=(
    "Generate a comprehensive design document for stakeholder review and approval. "
    "This produces a markdown document that serves as the approval artifact in the "
    "enterprise infrastructure workflow. It includes: project overview, business "
    "justification, architecture summary, architecture diagram (Mermaid), resource "
    "inventory, architectural decisions (ADR-style), policy compliance results, "
    "cost estimates, security considerations, risks, and next steps. "
    "Use this tool AFTER generating/composing infrastructure, creating the architecture "
    "diagram, running cost estimation, and checking policy compliance. The output is a "
    "complete document ready to attach to a ticket or send for stakeholder sign-off."
))
async def generate_design_document(params: GenerateDesignDocParams) -> str:
    """Generate a stakeholder-ready design document."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    doc = []

    # ── Header ────────────────────────────────────────────────
    doc.append(f"# Infrastructure Design Document")
    doc.append(f"")
    doc.append(f"| Field | Value |")
    doc.append(f"|---|---|")
    doc.append(f"| **Project** | {params.project_name} |")
    doc.append(f"| **Environment** | {params.environment.upper()} |")
    if params.requested_by:
        doc.append(f"| **Requested By** | {params.requested_by} |")
    doc.append(f"| **Generated** | {now} |")
    doc.append(f"| **Status** | PENDING APPROVAL |")
    doc.append(f"| **Generator** | InfraForge v0.1.0 |")
    doc.append(f"")

    # Template provenance
    if params.catalog_templates_used:
        template_list = ", ".join(f"`{t}`" for t in params.catalog_templates_used)
        doc.append(f"> **Catalog Templates Used:** {template_list}")
        doc.append(f"> These are pre-approved, tested infrastructure modules from the organization's template catalog.")
        doc.append(f"")

    doc.append(f"---")
    doc.append(f"")

    # ── Business Justification ─────────────────────────────────
    doc.append(f"## 1. Business Justification")
    doc.append(f"")
    doc.append(f"{params.business_justification}")
    doc.append(f"")

    # ── Architecture Summary ───────────────────────────────────
    doc.append(f"## 2. Architecture Summary")
    doc.append(f"")
    doc.append(f"{params.architecture_summary}")
    doc.append(f"")

    # ── Architecture Diagram ───────────────────────────────────
    if params.mermaid_diagram:
        doc.append(f"## 3. Architecture Diagram")
        doc.append(f"")
        # If the diagram already has ```mermaid fences, use as-is
        if "```mermaid" in params.mermaid_diagram:
            doc.append(params.mermaid_diagram)
        else:
            doc.append(f"```mermaid")
            doc.append(params.mermaid_diagram)
            doc.append(f"```")
        doc.append(f"")

    # ── Resource Inventory ─────────────────────────────────────
    doc.append(f"## 4. Resource Inventory")
    doc.append(f"")
    doc.append(f"| # | Resource | Type | SKU | Region | Purpose |")
    doc.append(f"|---|---|---|---|---|---|")
    for i, r in enumerate(params.resources, 1):
        doc.append(
            f"| {i} | {r.name} | {r.type} | {r.sku or '—'} "
            f"| {r.region or '—'} | {r.purpose or '—'} |"
        )
    doc.append(f"")

    # ── Architectural Decisions ────────────────────────────────
    if params.decisions:
        doc.append(f"## 5. Architectural Decisions")
        doc.append(f"")
        for i, d in enumerate(params.decisions, 1):
            doc.append(f"### ADR-{i:03d}: {d.topic}")
            doc.append(f"")
            doc.append(f"- **Decision:** {d.decision}")
            doc.append(f"- **Rationale:** {d.rationale}")
            if d.alternatives_considered:
                doc.append(f"- **Alternatives Considered:** {d.alternatives_considered}")
            doc.append(f"")

    # ── Policy Compliance ──────────────────────────────────────
    if params.compliance_results:
        pass_count = sum(1 for c in params.compliance_results if c.status == "pass")
        fail_count = sum(1 for c in params.compliance_results if c.status == "fail")
        warn_count = sum(1 for c in params.compliance_results if c.status == "warning")
        total = len(params.compliance_results)

        overall = "COMPLIANT" if fail_count == 0 else "NON-COMPLIANT"
        emoji = "✅" if fail_count == 0 else "❌"

        doc.append(f"## 6. Policy Compliance")
        doc.append(f"")
        doc.append(f"**Overall Status: {emoji} {overall}** "
                   f"({pass_count}/{total} passed"
                   f"{f', {warn_count} warnings' if warn_count else ''}"
                   f"{f', {fail_count} failures' if fail_count else ''})")
        doc.append(f"")
        doc.append(f"| Check | Status | Details |")
        doc.append(f"|---|---|---|")
        for c in params.compliance_results:
            status_icon = {"pass": "✅", "fail": "❌", "warning": "⚠️"}.get(c.status, "❓")
            doc.append(f"| {c.check} | {status_icon} {c.status.upper()} | {c.details or '—'} |")
        doc.append(f"")

    # ── Cost Estimate ──────────────────────────────────────────
    if params.cost_items:
        total_cost = sum(item.monthly_cost for item in params.cost_items)
        annual_cost = total_cost * 12

        doc.append(f"## 7. Cost Estimate")
        doc.append(f"")
        doc.append(f"| Resource | SKU | Monthly (USD) | Notes |")
        doc.append(f"|---|---|---:|---|")
        for item in params.cost_items:
            doc.append(
                f"| {item.resource} | {item.sku or '—'} "
                f"| ${item.monthly_cost:,.2f} | {item.notes or '—'} |"
            )
        doc.append(f"| **TOTAL** | | **${total_cost:,.2f}** | |")
        doc.append(f"")
        doc.append(f"**Projected Annual Cost: ${annual_cost:,.2f}**")
        doc.append(f"")
        doc.append(f"> *Estimates are approximate. Actual costs may vary based on usage, "
                   f"reserved instance pricing, and regional pricing differences. "
                   f"Refer to [Azure Pricing Calculator](https://azure.microsoft.com/pricing/calculator/) "
                   f"for precise quotes.*")
        doc.append(f"")

    # ── Security Considerations ────────────────────────────────
    if params.security_notes:
        doc.append(f"## 8. Security Considerations")
        doc.append(f"")
        for note in params.security_notes:
            doc.append(f"- {note}")
        doc.append(f"")

    # ── Risks ──────────────────────────────────────────────────
    if params.risks:
        doc.append(f"## 9. Known Risks")
        doc.append(f"")
        for risk in params.risks:
            doc.append(f"- ⚠️ {risk}")
        doc.append(f"")

    # ── Next Steps ─────────────────────────────────────────────
    if params.next_steps:
        doc.append(f"## 10. Next Steps")
        doc.append(f"")
        for i, step in enumerate(params.next_steps, 1):
            doc.append(f"{i}. {step}")
        doc.append(f"")

    # ── Approval Section ───────────────────────────────────────
    doc.append(f"---")
    doc.append(f"")
    doc.append(f"## Approval")
    doc.append(f"")
    doc.append(f"| Role | Name | Date | Signature |")
    doc.append(f"|---|---|---|---|")
    doc.append(f"| **Requestor** | {'_' * 20} | {'_' * 12} | {'_' * 15} |")
    doc.append(f"| **Tech Lead** | {'_' * 20} | {'_' * 12} | {'_' * 15} |")
    doc.append(f"| **Security** | {'_' * 20} | {'_' * 12} | {'_' * 15} |")
    doc.append(f"| **Platform Team** | {'_' * 20} | {'_' * 12} | {'_' * 15} |")
    doc.append(f"| **Cost Approver** | {'_' * 20} | {'_' * 12} | {'_' * 15} |")
    doc.append(f"")
    doc.append(f"---")
    doc.append(f"*Generated by InfraForge — Self-Service Infrastructure Platform*")

    return "\n".join(doc)
