"""
Template cloning tool.
Creates a copy of an existing catalog template under a new unique ID.
"""

from pydantic import BaseModel, Field
from copilot import define_tool

from ..database import get_template_by_id, upsert_template


class CloneTemplateParams(BaseModel):
    source_id: str = Field(
        description="ID of the existing template to clone. Example: 'bicep-container-app'"
    )
    new_id: str = Field(
        description=(
            "Unique ID for the cloned template. Must differ from source_id and "
            "not already exist. Format: {format}-{resource-type}. "
            "Example: 'bicep-container-app-v2'"
        )
    )
    new_name: str = Field(
        default="",
        description="Optional new display name. If empty, uses '<original name> (Copy)'.",
    )


@define_tool(description=(
    "Clone an existing template in the catalog under a new unique ID. "
    "Creates a full copy of all template metadata, content, tags, parameters, "
    "and compliance profile. The clone starts in 'draft' status so it can be "
    "customized and validated independently. Use this to fork a template for "
    "a different team, environment, or variation without modifying the original."
))
async def clone_template(params: CloneTemplateParams) -> str:
    """Clone a catalog template to a new ID."""

    source = await get_template_by_id(params.source_id)
    if not source:
        return f"❌ Source template `{params.source_id}` not found in the catalog."

    # Prevent clobber — reject if target ID already exists
    if params.new_id == params.source_id:
        return "❌ The new ID must be different from the source template ID."

    existing = await get_template_by_id(params.new_id)
    if existing:
        return (
            f"❌ A template with ID `{params.new_id}` already exists.\n"
            f"- **Name:** {existing['name']}\n"
            f"- **Status:** {existing.get('status', 'unknown')}\n\n"
            f"Choose a different ID to avoid overwriting."
        )

    new_name = params.new_name.strip() if params.new_name else f"{source['name']} (Copy)"

    clone = {
        "id": params.new_id,
        "name": new_name,
        "description": source.get("description", ""),
        "format": source.get("format", "bicep"),
        "category": source.get("category", "compute"),
        "source_path": "",
        "content": source.get("content", ""),
        "tags": list(source.get("tags", [])),
        "resources": list(source.get("resources", [])),
        "parameters": list(source.get("parameters", [])),
        "outputs": list(source.get("outputs", [])),
        "is_blueprint": source.get("is_blueprint", False),
        "service_ids": list(source.get("service_ids", [])),
        "template_type": source.get("template_type", "workload"),
        "provides": list(source.get("provides", [])),
        "requires": list(source.get("requires", [])),
        "optional_refs": list(source.get("optional_refs", [])),
        "compliance_profile": source.get("compliance_profile"),
        "pinned_versions": dict(source.get("pinned_versions", {})),
        "registered_by": "copilot-agent",
        "status": "draft",
    }

    await upsert_template(clone)

    return (
        f"✅ Template cloned successfully!\n\n"
        f"- **Source:** `{params.source_id}` → **Clone:** `{params.new_id}`\n"
        f"- **Name:** {new_name}\n"
        f"- **Status:** draft (ready for customization)\n\n"
        f"The clone is independent — edit, validate, and promote it without "
        f"affecting the original template."
    )
