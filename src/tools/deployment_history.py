"""
Deployment history tool.

Provides persistent, database-backed deployment listing with filters.
Complements the in-memory get_deployment_status tool (which only shows
in-flight deployments from the current server session).
"""

from pydantic import BaseModel, Field
from copilot import define_tool

from src.database import (
    get_deployments,
    get_deployment,
)


class ListDeploymentsParams(BaseModel):
    deployment_id: str = Field(
        default="",
        description=(
            "Specific deployment ID to look up for full detail. "
            "Leave empty to list recent deployments."
        ),
    )
    status: str = Field(
        default="",
        description="Filter by status: running, succeeded, failed, torn_down. Empty for all.",
    )
    resource_group: str = Field(
        default="",
        description="Filter by Azure resource group name. Empty for all.",
    )


@define_tool(
    description=(
        "List infrastructure deployments from the persistent deployment history, "
        "or get full details for a specific deployment by ID. Unlike "
        "get_deployment_status (which shows in-flight deployments from the current "
        "session), this tool queries the full deployment database including completed, "
        "failed, and torn-down deployments. Filter by status or resource group. "
        "Use this when the user asks 'what have we deployed?', 'show me failed "
        "deployments', 'what happened to deployment X?', or 'list deployments in "
        "resource group Y'."
    ),
)
async def list_deployments(params: ListDeploymentsParams) -> str:
    """List or look up deployments from the persistent database."""

    # ── Single deployment detail ─────────────────────────────
    if params.deployment_id:
        return await _single_deployment(params.deployment_id)

    # ── List mode ────────────────────────────────────────────
    return await _list_deployments(params)


async def _single_deployment(deployment_id: str) -> str:
    dep = await get_deployment(deployment_id)
    if not dep:
        return (
            f"**Deployment not found:** `{deployment_id}`\n\n"
            "Use `list_deployments` with no deployment_id to browse recent deployments."
        )

    status = dep.get("status", "unknown")
    status_icon = {
        "running": "\u23f3",
        "succeeded": "\u2705",
        "failed": "\u274c",
        "torn_down": "\U0001f5d1\ufe0f",
    }.get(status, "\u2753")

    lines = [
        f"# Deployment: {dep.get('deployment_id', deployment_id)}",
        "",
        f"- **Status:** {status_icon} {status}",
    ]

    if dep.get("template_name"):
        tline = f"- **Template:** {dep['template_name']}"
        if dep.get("template_version"):
            tline += f" (v{dep['template_version']}"
            if dep.get("template_semver"):
                tline += f", semver {dep['template_semver']}"
            tline += ")"
        lines.append(tline)

    if dep.get("template_id"):
        lines.append(f"- **Template ID:** `{dep['template_id']}`")
    if dep.get("resource_group"):
        lines.append(f"- **Resource Group:** {dep['resource_group']}")
    if dep.get("region"):
        lines.append(f"- **Region:** {dep['region']}")
    if dep.get("subscription_id"):
        lines.append(f"- **Subscription:** {dep['subscription_id']}")
    if dep.get("initiated_by"):
        lines.append(f"- **Initiated By:** {dep['initiated_by']}")

    started = dep.get("started_at")
    completed = dep.get("completed_at")
    if started:
        lines.append(f"- **Started:** {str(started)[:19]}")
    if completed:
        lines.append(f"- **Completed:** {str(completed)[:19]}")
    if started and completed:
        dur = _format_duration(started, completed)
        if dur:
            lines.append(f"- **Duration:** {dur}")

    if dep.get("error"):
        lines.append("")
        lines.append(f"## Error")
        lines.append(f"```\n{dep['error']}\n```")

    # Provisioned resources
    resources = dep.get("provisioned_resources") or []
    if resources:
        lines.append("")
        lines.append(f"## Provisioned Resources ({len(resources)})")
        for r in resources:
            if isinstance(r, dict):
                rtype = r.get("type", "unknown")
                rname = r.get("name", "?")
                lines.append(f"- `{rtype}` / {rname}")
            else:
                lines.append(f"- {r}")

    # Outputs
    outputs = dep.get("outputs") or {}
    if outputs:
        lines.append("")
        lines.append("## Outputs")
        for k, v in outputs.items():
            val = v.get("value", v) if isinstance(v, dict) else v
            lines.append(f"- **{k}:** {val}")

    if dep.get("torn_down_at"):
        lines.append("")
        lines.append(f"*Torn down at {str(dep['torn_down_at'])[:19]}*")

    return "\n".join(lines)


async def _list_deployments(params: ListDeploymentsParams) -> str:
    deps = await get_deployments(
        status=params.status or None,
        resource_group=params.resource_group or None,
        limit=25,
    )

    if not deps:
        filters = []
        if params.status:
            filters.append(f"status={params.status}")
        if params.resource_group:
            filters.append(f"resource_group={params.resource_group}")
        filter_str = f" (filters: {', '.join(filters)})" if filters else ""
        return f"**No deployments found{filter_str}.**"

    total = len(deps)
    filters = []
    if params.status:
        filters.append(f"status={params.status}")
    if params.resource_group:
        filters.append(f"resource_group={params.resource_group}")
    filter_str = f" | **Filters:** {', '.join(filters)}" if filters else ""

    # Status summary
    by_status: dict[str, int] = {}
    for d in deps:
        s = d.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
    summary_parts = [f"{s}: {c}" for s, c in sorted(by_status.items())]

    lines = [
        "# Deployment History",
        "",
        f"**Showing:** {total} deployments{filter_str}",
        f"**Breakdown:** {' | '.join(summary_parts)}",
        "",
        "| ID | Template | Resource Group | Status | Started | Duration |",
        "|----|----------|----------------|--------|---------|----------|",
    ]

    for d in deps:
        did = d.get("deployment_id", "?")
        tname = d.get("template_name", "") or d.get("template_id", "\u2014")
        rg = d.get("resource_group", "\u2014")
        dst = d.get("status", "?")
        started = str(d.get("started_at", ""))[:16]
        dur = _format_duration(d.get("started_at"), d.get("completed_at")) or "\u2014"
        # Strip what_if_results from listing (can be huge)
        lines.append(f"| {did} | {tname} | {rg} | {dst} | {started} | {dur} |")

    return "\n".join(lines)


def _format_duration(started, completed) -> str:
    """Format duration between two timestamps."""
    if not started or not completed:
        return ""
    try:
        from datetime import datetime
        s = datetime.fromisoformat(str(started))
        e = datetime.fromisoformat(str(completed))
        secs = int((e - s).total_seconds())
        if secs < 0:
            return ""
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m {secs % 60}s"
    except Exception:
        return ""
