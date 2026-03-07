"""
Template catalog browser tool.

Provides direct listing and ID-based lookup for the template catalog,
including composition tree and version history. Complements the fuzzy-search
tool (search_template_catalog) by supporting browsing without a query.
"""

from pydantic import BaseModel, Field
from copilot import define_tool

from src.database import (
    get_all_templates,
    get_template_by_id,
    get_template_versions,
    get_services_basic,
)


class BrowseTemplateCatalogParams(BaseModel):
    template_id: str = Field(
        default="",
        description=(
            "Specific template ID to look up for full detail. "
            "Leave empty to list all templates. "
            "Example: 'bicep-appservice-linux', 'blueprint-webapp-sql'."
        ),
    )
    category: str = Field(
        default="",
        description=(
            "Filter by category: compute, database, security, monitoring, "
            "storage, networking, cicd, blueprint, foundation. Empty for all."
        ),
    )
    format: str = Field(
        default="",
        description="Filter by format: bicep, terraform, github-actions, azure-devops. Empty for all.",
    )
    include_versions: bool = Field(
        default=False,
        description="When looking up a single template, include its version history.",
    )
    include_composition: bool = Field(
        default=False,
        description=(
            "When looking up a blueprint template, show its composition tree "
            "(the child services/templates it is composed from)."
        ),
    )


MAX_BROWSE_RESULTS = 50


@define_tool(
    description=(
        "Browse or look up templates in the organization's approved template catalog. "
        "Use this tool in two ways: (1) provide a template_id to get full details "
        "about a specific template including its composition tree and version history, "
        "or (2) leave template_id empty to list all templates with optional category "
        "and format filters. Unlike search_template_catalog (which does fuzzy search "
        "by query), this tool does direct listing and ID-based lookup. Use this when "
        "the user asks 'what templates do we have?', 'show me template X', "
        "'what is template Y composed of?', or 'list all bicep templates'."
    ),
)
async def browse_template_catalog(params: BrowseTemplateCatalogParams) -> str:
    """List all templates or get full detail for one by ID."""

    # ── Single-template detail mode ──────────────────────────
    if params.template_id:
        return await _single_template(params)

    # ── Browse / list mode ───────────────────────────────────
    return await _browse_templates(params)


async def _single_template(params: BrowseTemplateCatalogParams) -> str:
    tmpl = await get_template_by_id(params.template_id)
    if not tmpl:
        return (
            f"**Template not found:** `{params.template_id}`\n\n"
            "Use `browse_template_catalog` with no template_id to list all templates, "
            "or `search_template_catalog` to search by keyword."
        )

    lines = [
        f"# {tmpl.get('name', params.template_id)}",
        "",
        f"- **ID:** `{tmpl.get('id', params.template_id)}`",
        f"- **Format:** {tmpl.get('format', 'unknown')} | **Category:** {tmpl.get('category', 'unknown')}",
    ]

    if tmpl.get("template_type"):
        lines.append(f"- **Type:** {tmpl['template_type']}")
    if tmpl.get("description"):
        lines.append(f"- **Description:** {tmpl['description']}")

    tags = tmpl.get("tags") or []
    if tags:
        lines.append(f"- **Tags:** {', '.join(tags) if isinstance(tags, list) else tags}")

    resource_types = tmpl.get("resource_types") or []
    if resource_types:
        lines.append(f"- **Resources:** {', '.join(resource_types) if isinstance(resource_types, list) else resource_types}")

    if tmpl.get("source"):
        lines.append(f"- **Source:** {tmpl['source']}")

    # Parameters
    parameters = tmpl.get("parameters") or []
    if parameters:
        lines.append("")
        lines.append(f"## Parameters ({len(parameters)})")
        lines.append("")
        lines.append("| Name | Type | Required | Default |")
        lines.append("|------|------|----------|---------|")
        for p in parameters:
            if isinstance(p, dict):
                pname = p.get("name", "?")
                ptype = p.get("type", "?")
                preq = "yes" if p.get("required") else "no"
                pdef = str(p.get("default", "\u2014"))
                lines.append(f"| {pname} | {ptype} | {preq} | {pdef} |")

    # Outputs
    outputs = tmpl.get("outputs") or []
    if outputs:
        lines.append("")
        lines.append(f"## Outputs ({len(outputs)})")
        for o in outputs:
            lines.append(f"- `{o}`")

    # ── Composition tree ─────────────────────────────────────
    if params.include_composition:
        service_ids = tmpl.get("service_ids") or []
        lines.append("")
        if service_ids:
            services_map = await get_services_basic(service_ids)
            lines.append(f"## Composition ({len(service_ids)} services)")
            lines.append("")
            for i, sid in enumerate(service_ids, 1):
                svc_info = services_map.get(sid)
                if svc_info:
                    sname = svc_info.get("name", sid)
                    scat = svc_info.get("category", "")
                    sstatus = svc_info.get("status", "")
                    lines.append(f"{i}. **{sname}** (`{sid}`) \u2014 {scat}, {sstatus}")
                else:
                    lines.append(f"{i}. `{sid}` \u2014 *not found in catalog*")
        else:
            lines.append("## Composition")
            lines.append("*This template is not composed from other services.*")

    # ── Version history ──────────────────────────────────────
    if params.include_versions:
        versions = await get_template_versions(params.template_id)
        lines.append("")
        if versions:
            lines.append(f"## Version History ({len(versions)})")
            lines.append("")
            lines.append("| Version | Semver | Status | Changelog | Created |")
            lines.append("|---------|--------|--------|-----------|---------|")
            for v in versions:
                ver = v.get("version", "?")
                sem = v.get("semver", "")
                vst = v.get("status", "")
                clog = (v.get("changelog") or "")[:60]
                created = str(v.get("created_at", ""))[:10]
                lines.append(f"| v{ver} | {sem or '\u2014'} | {vst} | {clog or '\u2014'} | {created} |")
        else:
            lines.append("## Version History")
            lines.append("*No versions found.*")

    return "\n".join(lines)


async def _browse_templates(params: BrowseTemplateCatalogParams) -> str:
    templates = await get_all_templates(
        category=params.category or None,
        fmt=params.format or None,
    )

    if not templates:
        filters = []
        if params.category:
            filters.append(f"category={params.category}")
        if params.format:
            filters.append(f"format={params.format}")
        filter_str = f" (filters: {', '.join(filters)})" if filters else ""
        return f"**No templates found{filter_str}.** The catalog may be empty or the filters too restrictive."

    total = len(templates)
    truncated = total > MAX_BROWSE_RESULTS
    display = templates[:MAX_BROWSE_RESULTS]

    filters = []
    if params.category:
        filters.append(f"category={params.category}")
    if params.format:
        filters.append(f"format={params.format}")
    filter_str = f" | **Filters:** {', '.join(filters)}" if filters else ""

    lines = [
        "# Template Catalog",
        "",
        f"**Total:** {total} templates{filter_str}",
        "",
        "| ID | Name | Format | Category | Type | Tags |",
        "|----|------|--------|----------|------|------|",
    ]

    for t in display:
        tid = t.get("id", "?")
        tname = t.get("name", "?")
        tfmt = t.get("format", "?")
        tcat = t.get("category", "?")
        ttype = t.get("template_type", "")
        tags = t.get("tags") or []
        tag_str = ", ".join(tags[:3]) if isinstance(tags, list) else str(tags)[:30]
        lines.append(f"| {tid} | {tname} | {tfmt} | {tcat} | {ttype} | {tag_str} |")

    if truncated:
        lines.append("")
        lines.append(
            f"*Showing {MAX_BROWSE_RESULTS} of {total}. "
            "Use category or format filters to narrow results.*"
        )

    return "\n".join(lines)
