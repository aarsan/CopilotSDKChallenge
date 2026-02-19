"""
Template composition tool.
Assembles deployments from multiple catalog entries (stored in Azure SQL),
wiring parameters together.
"""

from pydantic import BaseModel, Field
from copilot import define_tool

from ..database import get_all_templates, get_template_by_id


class ComposeFromCatalogParams(BaseModel):
    template_ids: list[str] = Field(
        description=(
            "List of template IDs from the catalog to compose together. "
            "Example: ['bicep-appservice-linux', 'bicep-sql-database', 'bicep-keyvault']"
        )
    )
    project_name: str = Field(
        description="Project name to use for resource naming across all templates",
    )
    environment: str = Field(
        default="dev",
        description="Target environment: dev, staging, or prod",
    )
    region: str = Field(
        default="eastus2",
        description="Azure region for all resources",
    )


@define_tool(description=(
    "Compose a deployment from multiple approved catalog templates. "
    "Use this tool when the user needs multiple resources that exist as individual templates "
    "in the catalog. Provide the template IDs and this tool will return all the template sources "
    "along with guidance on wiring them together (shared parameters, outputs→inputs, "
    "managed identity assignments, and diagnostic logging). "
    "If a blueprint already exists for the combination, prefer using that instead."
))
async def compose_from_catalog(params: ComposeFromCatalogParams) -> str:
    """Compose multiple catalog templates into a deployment."""
    # Look up each requested template from the DB
    found = []
    missing = []

    for tid in params.template_ids:
        tmpl = await get_template_by_id(tid)
        if tmpl:
            found.append(tmpl)
        else:
            missing.append(tid)

    if not found:
        return (
            f"❌ None of the requested templates were found: {params.template_ids}\n"
            "Use `search_template_catalog` to find available templates."
        )

    # Check if a blueprint already covers this combination
    all_templates = await get_all_templates()
    for tmpl in all_templates:
        composed = set(tmpl.get("service_ids", []))
        requested = set(params.template_ids)
        if composed and requested.issubset(composed) and tmpl.get("is_blueprint"):
            content = tmpl.get("content", "// No content stored for this blueprint")
            fmt = tmpl.get("format", "bicep")
            return (
                f"## 🏗️ Existing Blueprint Found: {tmpl['name']}\n\n"
                f"A pre-composed blueprint already covers these templates.\n"
                f"**Blueprint ID:** `{tmpl['id']}`\n\n"
                f"### Template Content\n"
                f"```{fmt}\n{content}\n```\n\n"
                f"Adapt the parameters:\n"
                f"- `projectName`: `{params.project_name}`\n"
                f"- `environment`: `{params.environment}`\n"
                f"- `location`: `{params.region}`\n"
            )

    # No blueprint — provide individual templates with wiring guidance
    lines = []
    lines.append(f"## 🔧 Composing {len(found)} Templates\n")
    lines.append(f"**Project:** {params.project_name} | **Env:** {params.environment} | **Region:** {params.region}\n")

    if missing:
        lines.append(f"⚠️ Templates not found (will need generation): {', '.join(missing)}\n")

    # Collect all outputs for wiring
    all_outputs = {}

    for tmpl in found:
        content = tmpl.get("content", "// No content stored for this template")
        fmt = tmpl.get("format", "bicep")

        lines.append(f"### 📦 {tmpl['name']} (`{tmpl['id']}`)")
        if tmpl.get("source"):
            lines.append(f"**Source:** `catalog/{tmpl['source']}`\n")
        lines.append(f"```{fmt}\n{content}\n```\n")

        for out in tmpl.get("outputs", []):
            all_outputs[out] = tmpl["id"]

    # Wiring guidance
    lines.append("---")
    lines.append("## 🔗 Wiring Guidance\n")
    lines.append("### Shared Parameters")
    lines.append(f"- `projectName` / `project_name`: `{params.project_name}`")
    lines.append(f"- `environment`: `{params.environment}`")
    lines.append(f"- `location`: `{params.region}`\n")

    lines.append("### Output → Input Connections")
    # Suggest common wiring patterns
    if "workspaceId" in all_outputs:
        lines.append(f"- `logAnalyticsWorkspaceId` ← `{all_outputs['workspaceId']}.outputs.workspaceId`")
    if "principalId" in all_outputs and "keyVaultUri" in all_outputs:
        lines.append(f"- Grant `{all_outputs['principalId']}` Key Vault Secrets User role on Key Vault")
    if "sqlServerFqdn" in all_outputs and "principalId" in all_outputs:
        lines.append(f"- Wire SQL connection string into App Service app settings via Key Vault reference")
    lines.append("")

    lines.append("### Deployment Order")
    # Suggest logical ordering
    order = []
    for cat in ["monitoring", "foundation", "networking", "security", "storage", "database", "compute", "cicd"]:
        for tmpl in found:
            if tmpl.get("category") == cat:
                order.append(tmpl["name"])
    if order:
        for i, name in enumerate(order, 1):
            lines.append(f"{i}. {name}")
    lines.append("")

    lines.append("Use this source code and wiring guidance to assemble a complete deployment. "
                 "Consider creating a new blueprint and registering it with `register_template` for future reuse.")

    return "\n".join(lines)
