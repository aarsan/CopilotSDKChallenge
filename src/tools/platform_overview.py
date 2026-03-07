"""
Platform overview tool.

Provides dashboard-level statistics for the InfraForge platform including
service catalog counts, template catalog counts, deployment success rates,
and pending approval requests.
"""

from pydantic import BaseModel, Field
from copilot import define_tool

from src.database import (
    get_all_services,
    get_all_templates,
    get_deployments,
    get_approval_requests,
    get_usage_stats,
)


class GetPlatformOverviewParams(BaseModel):
    include_usage_stats: bool = Field(
        default=False,
        description="Include usage analytics (request counts, catalog reuse rate, cost totals).",
    )


@define_tool(
    description=(
        "Get a high-level overview of the InfraForge platform including service "
        "catalog statistics, template catalog counts, deployment success rates, "
        "and pending approval requests. Use this when the user asks about platform "
        "health, adoption metrics, or wants a summary of the current state of "
        "infrastructure governance. Examples: 'how is the platform doing?', "
        "'give me a platform summary', 'how many services are approved?'."
    ),
)
async def get_platform_overview(params: GetPlatformOverviewParams) -> str:
    """Return a dashboard-level summary of the platform."""

    lines = ["# InfraForge Platform Overview", ""]

    # ── Service Catalog ──────────────────────────────────────
    services = await get_all_services()
    svc_by_status: dict[str, int] = {}
    svc_by_category: dict[str, int] = {}
    for s in services:
        st = s.get("status", "unknown")
        svc_by_status[st] = svc_by_status.get(st, 0) + 1
        cat = s.get("category", "unknown")
        svc_by_category[cat] = svc_by_category.get(cat, 0) + 1

    lines.append("## Service Catalog")
    lines.append(f"- **Total Services:** {len(services)}")
    if svc_by_status:
        status_parts = [f"{k}: {v}" for k, v in sorted(svc_by_status.items())]
        lines.append(f"- **By Status:** {' | '.join(status_parts)}")
    if svc_by_category:
        cat_parts = sorted(svc_by_category.items(), key=lambda x: -x[1])
        cat_str = ", ".join(f"{k} ({v})" for k, v in cat_parts)
        lines.append(f"- **By Category:** {cat_str}")
    lines.append("")

    # ── Template Catalog ─────────────────────────────────────
    templates = await get_all_templates()
    tmpl_by_format: dict[str, int] = {}
    tmpl_by_category: dict[str, int] = {}
    blueprint_count = 0
    for t in templates:
        fmt = t.get("format", "unknown")
        tmpl_by_format[fmt] = tmpl_by_format.get(fmt, 0) + 1
        cat = t.get("category", "unknown")
        tmpl_by_category[cat] = tmpl_by_category.get(cat, 0) + 1
        if t.get("template_type") == "blueprint":
            blueprint_count += 1

    lines.append("## Template Catalog")
    lines.append(f"- **Total Templates:** {len(templates)}")
    if tmpl_by_format:
        fmt_parts = [f"{k}: {v}" for k, v in sorted(tmpl_by_format.items())]
        lines.append(f"- **By Format:** {' | '.join(fmt_parts)}")
    if tmpl_by_category:
        cat_parts = sorted(tmpl_by_category.items(), key=lambda x: -x[1])
        cat_str = ", ".join(f"{k} ({v})" for k, v in cat_parts)
        lines.append(f"- **By Category:** {cat_str}")
    if blueprint_count:
        lines.append(f"- **Blueprints:** {blueprint_count}")
    lines.append("")

    # ── Deployments ──────────────────────────────────────────
    deployments = await get_deployments(limit=50)
    dep_by_status: dict[str, int] = {}
    for d in deployments:
        st = d.get("status", "unknown")
        dep_by_status[st] = dep_by_status.get(st, 0) + 1

    lines.append("## Deployments (Recent)")
    lines.append(f"- **Total:** {len(deployments)}")
    if dep_by_status:
        dep_parts = [f"{k}: {v}" for k, v in sorted(dep_by_status.items())]
        lines.append(f"- **By Status:** {' | '.join(dep_parts)}")
        succeeded = dep_by_status.get("succeeded", 0)
        total_finished = succeeded + dep_by_status.get("failed", 0)
        if total_finished > 0:
            rate = (succeeded / total_finished) * 100
            lines.append(f"- **Success Rate:** {rate:.0f}%")
    lines.append("")

    # ── Approval Requests ────────────────────────────────────
    approvals = await get_approval_requests()
    app_by_status: dict[str, int] = {}
    for a in approvals:
        st = a.get("status", "unknown")
        app_by_status[st] = app_by_status.get(st, 0) + 1

    pending_count = app_by_status.get("submitted", 0) + app_by_status.get("in_review", 0)
    resolved_count = sum(v for k, v in app_by_status.items() if k not in ("submitted", "in_review"))

    lines.append("## Approval Requests")
    lines.append(f"- **Total:** {len(approvals)}")
    lines.append(f"- **Pending:** {pending_count}")
    lines.append(f"- **Resolved:** {resolved_count}")
    if app_by_status:
        app_parts = [f"{k}: {v}" for k, v in sorted(app_by_status.items())]
        lines.append(f"- **Breakdown:** {' | '.join(app_parts)}")

    # ── Usage stats (optional) ───────────────────────────────
    if params.include_usage_stats:
        lines.append("")
        try:
            stats = await get_usage_stats()
            lines.append("## Usage Analytics")
            lines.append(f"- **Total Requests:** {stats.get('total_requests', 0)}")
            if stats.get("catalog_reuse_rate") is not None:
                lines.append(f"- **Catalog Reuse Rate:** {stats['catalog_reuse_rate']:.1f}%")
            if stats.get("estimated_monthly_cost") is not None:
                lines.append(f"- **Estimated Monthly Cost:** ${stats['estimated_monthly_cost']:,.2f}")
            if stats.get("top_resource_types"):
                top = stats["top_resource_types"][:5]
                lines.append("- **Top Resource Types:** " + ", ".join(
                    f"{r['type']} ({r['count']})" for r in top if isinstance(r, dict)
                ))
        except Exception:
            lines.append("## Usage Analytics")
            lines.append("*Usage data not available.*")

    return "\n".join(lines)
