"""
Template registration tool.
Adds newly created or approved templates into the catalog database for future reuse.
"""

from pydantic import BaseModel, Field
from copilot import define_tool

from ..database import upsert_template, get_template_by_id


class RegisterTemplateParams(BaseModel):
    id: str = Field(
        description=(
            "Unique template ID using format: {format}-{resource-type}. "
            "Examples: 'bicep-container-app', 'tf-aks-cluster', 'pipeline-gha-node'"
        )
    )
    name: str = Field(
        description="Human-readable template name. Example: 'Container App with Dapr'"
    )
    description: str = Field(
        description="Description of what the template deploys and its key features"
    )
    format: str = Field(
        description="Template format: bicep, terraform, github-actions, or azure-devops"
    )
    category: str = Field(
        description="Category: compute, database, security, monitoring, storage, networking, cicd, blueprint, or foundation"
    )
    content: str = Field(
        description="The full template source code to save"
    )
    tags: list[str] = Field(
        description="Search tags for discoverability. Example: ['container', 'serverless', 'dapr']"
    )
    resources: list[str] = Field(
        default=[],
        description="Azure resource types created. Example: ['Microsoft.App/containerApps']"
    )
    parameters: list[dict] = Field(
        default=[],
        description=(
            "Parameter definitions. Each dict: {'name': str, 'type': str, 'required': bool, "
            "'description': str, 'default': str (optional)}"
        ),
    )
    outputs: list[str] = Field(
        default=[],
        description="Output names. Example: ['containerAppUrl', 'fqdn']"
    )
    is_blueprint: bool = Field(
        default=False,
        description="Whether this is a multi-resource blueprint composed of other templates",
    )
    service_ids: list[str] = Field(
        default=[],
        description="If blueprint, list of template IDs this is composed of",
    )


@define_tool(description=(
    "Register a new template in the organization's approved template catalog. "
    "Use this tool after generating a template that should be reusable across the organization. "
    "The template will be stored in the database and become immediately searchable and available "
    "for composition. This enables the 'generate once, reuse forever' workflow."
))
async def register_template(params: RegisterTemplateParams) -> str:
    """Register a new template in the catalog database."""

    # Check for existing template with same ID
    existing = await get_template_by_id(params.id)
    if existing:
        return (
            f"⚠️ Template with ID `{params.id}` already exists in the catalog.\n"
            f"- **Name:** {existing['name']}\n"
            f"- **Category:** {existing['category']}\n\n"
            f"Use a different ID or update the existing template."
        )

    # Build the template record
    tmpl = {
        "id": params.id,
        "name": params.name,
        "description": params.description,
        "format": params.format,
        "category": params.category,
        "source_path": "",
        "content": params.content,
        "tags": params.tags,
        "resources": params.resources,
        "parameters": [p if isinstance(p, dict) else {"name": str(p)} for p in params.parameters],
        "outputs": params.outputs,
        "is_blueprint": params.is_blueprint,
        "service_ids": params.service_ids,
        "registered_by": "copilot-agent",
        "status": "approved",
    }

    await upsert_template(tmpl)

    return (
        f"✅ Template registered successfully!\n\n"
        f"- **ID:** `{params.id}`\n"
        f"- **Name:** {params.name}\n"
        f"- **Format:** {params.format}\n"
        f"- **Category:** {params.category}\n"
        f"- **Tags:** {', '.join(params.tags)}\n\n"
        f"This template is now searchable and available for composition in future requests."
    )
