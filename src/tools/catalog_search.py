"""
Template catalog search tool.
Searches the organization's approved template registry (stored in Azure SQL)
before generating from scratch.
"""

from pydantic import BaseModel, Field
from copilot import define_tool

from ..database import get_all_templates


def _score_match(template: dict, query: str, filters: dict) -> int:
    """Score how well a template matches the search query and filters."""
    score = 0
    query_lower = query.lower()
    query_terms = query_lower.split()

    # Match against name and description
    name_lower = template.get("name", "").lower()
    desc_lower = template.get("description", "").lower()

    for term in query_terms:
        if term in name_lower:
            score += 10
        if term in desc_lower:
            score += 5

    # Match against tags
    tags = [t.lower() for t in template.get("tags", [])]
    for term in query_terms:
        if term in tags:
            score += 15

    # Match against resource types
    resources = [r.lower() for r in template.get("resources", [])]
    for term in query_terms:
        for resource in resources:
            if term in resource:
                score += 12

    # Apply filters
    if filters.get("format"):
        if template.get("format", "").lower() == filters["format"].lower():
            score += 5
        else:
            score -= 50  # Strong penalty for format mismatch

    if filters.get("category"):
        if template.get("category", "").lower() == filters["category"].lower():
            score += 5
        else:
            score -= 20

    return score


class SearchCatalogParams(BaseModel):
    query: str = Field(
        description=(
            "Natural-language search query describing what infrastructure is needed. "
            "Examples: 'web app with database', 'key vault', 'CI/CD pipeline for Python'"
        )
    )
    format: str = Field(
        default="",
        description="Filter by format: bicep, terraform, github-actions, azure-devops, or empty for all",
    )
    category: str = Field(
        default="",
        description="Filter by category: compute, database, security, monitoring, storage, networking, cicd, blueprint, foundation, or empty for all",
    )


@define_tool(description=(
    "Search the organization's approved template catalog for reusable infrastructure templates. "
    "ALWAYS call this tool FIRST before generating infrastructure from scratch. "
    "The catalog contains pre-approved, tested Bicep modules, Terraform modules, pipeline templates, "
    "and multi-resource blueprints. If a matching template exists, use it instead of generating new code. "
    "Returns matching templates with their descriptions, parameters, and source file paths."
))
async def search_template_catalog(params: SearchCatalogParams) -> str:
    """Search the approved template catalog (from Azure SQL Database)."""
    catalog = await get_all_templates(
        category=params.category or None,
        fmt=params.format or None,
    )

    if not catalog:
        return "⚠️ Template catalog is empty. No approved templates found. Proceed with generation."

    filters = {}
    if params.format:
        filters["format"] = params.format
    if params.category:
        filters["category"] = params.category

    # Score and rank matches
    scored = []
    for template in catalog:
        score = _score_match(template, params.query, filters)
        if score > 0:
            scored.append((score, template))

    scored.sort(key=lambda x: x[0], reverse=True)

    if not scored:
        return (
            "No matching templates found in the catalog for: "
            f"'{params.query}'\n\n"
            "You should generate this from scratch using the appropriate generation tool."
        )

    # Format results
    lines = []
    lines.append(f"## 📚 Catalog Search Results for: \"{params.query}\"\n")
    lines.append(f"Found **{len(scored)}** matching template(s):\n")

    for rank, (score, tmpl) in enumerate(scored[:10], 1):
        is_blueprint = tmpl.get("is_blueprint") or tmpl.get("category") == "blueprint"
        icon = "🏗️" if is_blueprint else "📦"

        lines.append(f"### {icon} {rank}. {tmpl['name']}")
        lines.append(f"- **ID:** `{tmpl['id']}`")
        lines.append(f"- **Format:** {tmpl['format']} | **Category:** {tmpl['category']}")
        lines.append(f"- **Description:** {tmpl['description']}")

        if tmpl.get("source"):
            lines.append(f"- **Source:** `catalog/{tmpl['source']}`")

        if tmpl.get("service_ids"):
            lines.append(f"- **Composed of:** {', '.join(tmpl['service_ids'])}")

        # Parameters
        params_list = tmpl.get("parameters", [])
        if params_list:
            lines.append("- **Parameters:**")
            for p in params_list:
                req = "required" if p.get("required") else f"optional, default: `{p.get('default', 'N/A')}`"
                lines.append(f"  - `{p['name']}` ({p['type']}) — {req}")

        # Outputs
        outputs = tmpl.get("outputs", [])
        if outputs:
            lines.append(f"- **Outputs:** {', '.join(f'`{o}`' for o in outputs)}")

        lines.append(f"- **Match score:** {score}")
        lines.append("")

    lines.append("---")
    lines.append("**To use a template:** Use the template content directly and adapt parameters for the user's requirements.")
    lines.append("**To compose multiple templates:** Use the `compose_from_catalog` tool with the template IDs.")

    return "\n".join(lines)
