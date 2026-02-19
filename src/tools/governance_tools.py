"""
Governance & compliance tools.

Provides tools for security standards management, compliance framework
browsing, and compliance assessments. All data lives in the InfraForge
Fabric SQL Database.
"""

from pydantic import BaseModel, Field
from copilot import define_tool

from src.database import (
    get_security_standards,
    get_compliance_frameworks,
    get_governance_policies,
    get_compliance_assessment,
)


# ── Tool: List Security Standards ────────────────────────────

class ListSecurityStandardsParams(BaseModel):
    category: str = Field(
        default="",
        description=(
            "Filter by category: encryption, identity, network, monitoring, "
            "data_protection, compute. Leave empty to list all categories."
        ),
    )


@define_tool(description=(
    "List all security standards that resources must comply with. "
    "Security standards are machine-readable rules (HTTPS required, TLS 1.2+, "
    "managed identity, encryption at rest, etc.) that drive automated compliance checks. "
    "Each standard has a validation key and value used to evaluate resources programmatically. "
    "Filter by category to see standards for specific domains."
))
async def list_security_standards(params: ListSecurityStandardsParams) -> str:
    """List security standards from the database."""
    standards = await get_security_standards(
        category=params.category or None,
        enabled_only=True,
    )

    if not standards:
        if params.category:
            return f"No security standards found for category '{params.category}'."
        return "No security standards found. Database may not be seeded yet."

    # Group by category
    by_category: dict[str, list] = {}
    for std in standards:
        cat = std.get("category", "other")
        by_category.setdefault(cat, []).append(std)

    severity_icons = {
        "critical": "🔴",
        "high": "🟠",
        "medium": "🟡",
        "low": "🟢",
    }

    lines = [
        "# 🔒 Security Standards\n",
        f"**Total:** {len(standards)} active standards\n",
    ]

    for cat in sorted(by_category.keys()):
        stds = by_category[cat]
        lines.append(f"## {cat.replace('_', ' ').title()}\n")

        for std in stds:
            sev = std.get("severity", "medium")
            icon = severity_icons.get(sev, "⚪")
            lines.append(f"### {icon} {std['id']}: {std['name']}")
            lines.append(f"- **Severity:** {sev.title()}")
            lines.append(f"- **Description:** {std.get('description', '')}")
            lines.append(f"- **Validation:** `{std['validation_key']}` = `{std.get('validation_value', '')}`")
            if std.get("remediation"):
                lines.append(f"- **Remediation:** {std['remediation']}")
            lines.append("")

    return "\n".join(lines)


# ── Tool: List Compliance Frameworks ─────────────────────────

class ListComplianceFrameworksParams(BaseModel):
    framework_id: str = Field(
        default="",
        description=(
            "Specific framework ID to show controls for (e.g., 'CIS-AZURE-2.0', "
            "'SOC2-TYPE2', 'HIPAA'). Leave empty to list all frameworks."
        ),
    )


@define_tool(description=(
    "List compliance frameworks (CIS Azure Benchmark, SOC2, HIPAA) and their controls. "
    "Each control maps to one or more security standards, creating a traceable compliance chain: "
    "Framework → Control → Security Standard → Automated Validation. "
    "Use this to understand what compliance obligations apply and which security standards "
    "satisfy each control."
))
async def list_compliance_frameworks(params: ListComplianceFrameworksParams) -> str:
    """List compliance frameworks and their controls."""
    frameworks = await get_compliance_frameworks(enabled_only=True)

    if not frameworks:
        return "No compliance frameworks found. Database may not be seeded yet."

    # Filter to a specific framework if requested
    if params.framework_id:
        frameworks = [fw for fw in frameworks if fw["id"] == params.framework_id]
        if not frameworks:
            return f"No compliance framework found with ID '{params.framework_id}'."

    lines = ["# 📋 Compliance Frameworks\n"]

    for fw in frameworks:
        controls = fw.get("controls", [])
        lines.append(f"## {fw['name']}")
        lines.append(f"- **ID:** {fw['id']}")
        lines.append(f"- **Version:** {fw.get('version', 'N/A')}")
        lines.append(f"- **Description:** {fw.get('description', '')}")
        lines.append(f"- **Controls:** {len(controls)}\n")

        if controls:
            lines.append("| Control | Name | Category | Mapped Standards |")
            lines.append("|---------|------|----------|------------------|")
            for ctrl in controls:
                std_ids = ctrl.get("security_standard_ids", [])
                std_str = ", ".join(std_ids) if std_ids else "—"
                lines.append(
                    f"| {ctrl['control_id']} | {ctrl['name']} | "
                    f"{ctrl.get('category', '')} | {std_str} |"
                )
        lines.append("")

    # Traceability summary
    all_std_ids = set()
    for fw in frameworks:
        for ctrl in fw.get("controls", []):
            all_std_ids.update(ctrl.get("security_standard_ids", []))

    lines.extend([
        "---\n",
        f"**Traceability:** These frameworks reference **{len(all_std_ids)}** unique "
        "security standards. Each standard has automated validation rules that are "
        "checked during policy compliance scans.",
    ])

    return "\n".join(lines)


# ── Tool: List Governance Policies ───────────────────────────

class ListGovernancePoliciesParams(BaseModel):
    category: str = Field(
        default="",
        description=(
            "Filter by category: tagging, geography, security, network, operations, cost. "
            "Leave empty to list all."
        ),
    )


@define_tool(description=(
    "List organization-wide governance policies that apply to all infrastructure deployments. "
    "These policies define rules like required tags, allowed regions, HTTPS enforcement, "
    "managed identity requirements, private endpoint mandates, and cost thresholds. "
    "Each policy has an enforcement level (block or warn) that determines whether violations "
    "prevent deployment or generate warnings."
))
async def list_governance_policies(params: ListGovernancePoliciesParams) -> str:
    """List governance policies from the database."""
    policies = await get_governance_policies(
        category=params.category or None,
        enabled_only=True,
    )

    if not policies:
        if params.category:
            return f"No governance policies found for category '{params.category}'."
        return "No governance policies found. Database may not be seeded yet."

    enforcement_icons = {
        "block": "🚫",
        "warn": "⚠️",
    }

    lines = [
        "# 🏛️ Governance Policies\n",
        f"**Total:** {len(policies)} active policies\n",
        "| ID | Policy | Category | Enforcement | Severity |",
        "|----|--------|----------|-------------|----------|",
    ]

    for pol in policies:
        enf = pol.get("enforcement", "warn")
        icon = enforcement_icons.get(enf, "❓")
        lines.append(
            f"| {pol['id']} | {pol['name']} | {pol.get('category', '')} | "
            f"{icon} {enf.title()} | {pol.get('severity', 'N/A').title()} |"
        )

    lines.append("\n---\n")

    # Show details
    for pol in policies:
        enf = pol.get("enforcement", "warn")
        icon = enforcement_icons.get(enf, "❓")
        lines.append(f"### {icon} {pol['id']}: {pol['name']}")
        lines.append(f"- **Description:** {pol.get('description', '')}")
        lines.append(f"- **Category:** {pol.get('category', '')}")
        lines.append(f"- **Rule:** `{pol['rule_key']}` = `{pol.get('rule_value', '')}`")
        lines.append(f"- **Enforcement:** {enf.title()} — "
                      + ("violations **block deployment**" if enf == "block"
                         else "violations generate **warnings** only"))
        lines.append("")

    return "\n".join(lines)
