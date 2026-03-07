"""
Service detail tool.

Provides a deep-dive into a specific Azure service in the governance catalog,
including approval status, governance fields, ARM template versions, pipeline
run history, and governance review results.
"""

from pydantic import BaseModel, Field
from copilot import define_tool

from src.database import (
    get_service,
    get_active_service_version,
    get_service_versions,
    get_pipeline_runs,
    get_governance_reviews,
)


class GetServiceDetailsParams(BaseModel):
    service_id: str = Field(
        description=(
            "The Azure resource type ID of the service to look up "
            "(e.g., 'Microsoft.Web/sites', 'Microsoft.Sql/servers'). "
            "Use check_service_approval or list_approved_services first if you "
            "don't know the exact ID."
        ),
    )
    include_versions: bool = Field(
        default=True,
        description="Include the list of ARM template versions (template bodies are stripped for brevity).",
    )
    include_pipeline_runs: bool = Field(
        default=False,
        description="Include recent pipeline runs (onboarding, validation, deployment) for this service.",
    )
    include_governance_reviews: bool = Field(
        default=False,
        description="Include CISO/CTO governance review results for this service.",
    )


@define_tool(
    description=(
        "Get detailed information about an Azure service in the governance catalog, "
        "including its approval status, governance fields, ARM template versions, "
        "active version summary, pipeline run history, and governance review results. "
        "Use this when the user asks about a specific service's onboarding status, "
        "what versions exist, what pipelines have run, or what governance reviews have "
        "been completed. Requires the service's resource type ID "
        "(e.g., 'Microsoft.Web/sites'). Use list_approved_services or "
        "check_service_approval first if you need to find the ID."
    ),
)
async def get_service_details(params: GetServiceDetailsParams) -> str:
    """Return rich detail for a single service."""

    svc = await get_service(params.service_id)
    if not svc:
        return (
            f"**Service not found:** `{params.service_id}`\n\n"
            "Use `list_approved_services` to browse available services, or "
            "`check_service_approval` to search by name."
        )

    # ── Core info ────────────────────────────────────────────
    status = svc.get("status", "unknown")
    status_icon = {
        "approved": "\u2705",
        "conditional": "\u26a0\ufe0f",
        "under_review": "\U0001f50d",
        "not_approved": "\u274c",
        "offboarded": "\u23f8\ufe0f",
    }.get(status, "\u2753")

    lines = [
        f"# {svc.get('name', params.service_id)}",
        "",
        f"- **Resource Type:** `{svc.get('id', params.service_id)}`",
        f"- **Status:** {status_icon} {status}",
        f"- **Category:** {svc.get('category', 'unknown')}",
        f"- **Risk Tier:** {svc.get('risk_tier', 'unknown')}",
    ]

    if svc.get("contact"):
        lines.append(f"- **Contact:** {svc['contact']}")
    if svc.get("documentation"):
        lines.append(f"- **Documentation:** {svc['documentation']}")
    if svc.get("review_notes"):
        lines.append(f"- **Review Notes:** {svc['review_notes']}")

    # Approved SKUs / regions / policies
    skus = svc.get("approved_skus") or []
    regions = svc.get("approved_regions") or []
    policies = svc.get("policies") or []
    conditions = svc.get("conditions") or []

    if regions:
        lines.append(f"- **Approved Regions:** {', '.join(regions)}")
    if skus:
        lines.append(f"- **Approved SKUs:** {', '.join(skus)}")
    if policies:
        lines.append(f"- **Policies:** {', '.join(policies)}")
    if conditions:
        lines.append(f"- **Conditions:** {', '.join(conditions)}")

    # ── Active version summary ───────────────────────────────
    active = await get_active_service_version(params.service_id)
    if active:
        lines.append("")
        lines.append("## Active Version")
        av = active.get("version", "?")
        asv = active.get("semver", "")
        ast = active.get("status", "")
        adate = active.get("created_at", "")
        lines.append(
            f"- **Version:** v{av}"
            + (f" (semver {asv})" if asv else "")
            + (f" \u2014 {ast}" if ast else "")
        )
        if adate:
            lines.append(f"- **Created:** {str(adate)[:19]}")
        # Show parameter count if available
        arm = active.get("arm_template")
        if isinstance(arm, dict):
            param_count = len(arm.get("parameters", {}))
            resource_count = len(arm.get("resources", []))
            lines.append(f"- **Parameters:** {param_count} | **Resources:** {resource_count}")
    else:
        lines.append("")
        lines.append("*No active ARM template version.*")

    # ── Version list ─────────────────────────────────────────
    if params.include_versions:
        versions = await get_service_versions(params.service_id)
        lines.append("")
        if versions:
            lines.append(f"## ARM Template Versions ({len(versions)} total)")
            lines.append("")
            lines.append("| Version | Semver | Status | Created | Size |")
            lines.append("|---------|--------|--------|---------|------|")
            for v in versions:
                ver = v.get("version", "?")
                sem = v.get("semver", "")
                vst = v.get("status", "")
                created = str(v.get("created_at", ""))[:10]
                arm_tpl = v.get("arm_template")
                if isinstance(arm_tpl, (dict, str)):
                    import json as _json
                    raw = arm_tpl if isinstance(arm_tpl, str) else _json.dumps(arm_tpl)
                    size = f"{len(raw) / 1024:.1f} KB"
                else:
                    size = "\u2014"
                lines.append(f"| v{ver} | {sem or '\u2014'} | {vst} | {created} | {size} |")
        else:
            lines.append("## ARM Template Versions")
            lines.append("*No versions found.*")

    # ── Pipeline runs ────────────────────────────────────────
    if params.include_pipeline_runs:
        runs = await get_pipeline_runs(params.service_id, limit=10)
        lines.append("")
        if runs:
            lines.append(f"## Recent Pipeline Runs ({len(runs)})")
            lines.append("")
            lines.append("| Run ID | Pipeline | Status | Started | Duration |")
            lines.append("|--------|----------|--------|---------|----------|")
            for r in runs:
                rid = r.get("run_id", "?")[:12]
                ptype = r.get("pipeline_type", "unknown")
                rst = r.get("status", "?")
                started = str(r.get("started_at", ""))[:19]
                dur = ""
                if r.get("started_at") and r.get("completed_at"):
                    try:
                        from datetime import datetime
                        s = datetime.fromisoformat(str(r["started_at"]))
                        e = datetime.fromisoformat(str(r["completed_at"]))
                        secs = int((e - s).total_seconds())
                        dur = f"{secs // 60}m {secs % 60}s"
                    except Exception:
                        dur = "\u2014"
                lines.append(f"| {rid} | {ptype} | {rst} | {started} | {dur or '\u2014'} |")
        else:
            lines.append("## Recent Pipeline Runs")
            lines.append("*No pipeline runs found.*")

    # ── Governance reviews ───────────────────────────────────
    if params.include_governance_reviews:
        reviews = await get_governance_reviews(params.service_id, limit=10)
        lines.append("")
        if reviews:
            lines.append(f"## Governance Reviews ({len(reviews)})")
            lines.append("")
            for r in reviews:
                agent = r.get("agent", "unknown")
                verdict = r.get("verdict", "unknown")
                confidence = r.get("confidence", "")
                summary = r.get("summary", "")
                risk = r.get("risk_score", "")
                findings = r.get("findings") or []
                ver = r.get("version", "?")
                lines.append(f"### {agent} \u2014 v{ver}")
                lines.append(f"- **Verdict:** {verdict}")
                if confidence:
                    lines.append(f"- **Confidence:** {confidence}")
                if risk:
                    lines.append(f"- **Risk Score:** {risk}")
                if summary:
                    lines.append(f"- **Summary:** {summary}")
                lines.append(f"- **Findings:** {len(findings)} items")
                lines.append("")
        else:
            lines.append("## Governance Reviews")
            lines.append("*No governance reviews found.*")

    return "\n".join(lines)
