"""
Bicep template generator tool.
Produces Azure Bicep templates based on natural-language infrastructure descriptions.
"""

from pydantic import BaseModel, Field
from copilot import define_tool

from src.templates.bicep_patterns import get_bicep_reference


class GenerateBicepParams(BaseModel):
    description: str = Field(
        description=(
            "A natural-language description of the Azure infrastructure to generate. "
            "Example: 'A 3-tier web app with App Service, SQL Database, and Key Vault'"
        )
    )
    environment: str = Field(
        default="dev",
        description="Target environment: dev, staging, or prod",
    )
    region: str = Field(
        default="eastus2",
        description="Azure region for the deployment",
    )
    resource_prefix: str = Field(
        default="myapp",
        description="Naming prefix for all resources",
    )


@define_tool(description=(
    "Generate a production-ready Azure Bicep template from a natural-language description. "
    "Use this tool when the user wants to create Azure infrastructure using Bicep. "
    "The tool provides reference patterns and best practices that the agent should use "
    "to produce complete, deployable Bicep files with proper parameterization, "
    "naming conventions, tagging, and security configuration."
))
async def generate_bicep(params: GenerateBicepParams) -> str:
    """Generate Bicep template guidance and reference patterns."""
    reference = get_bicep_reference()

    result = f"""## Bicep Generation Context

**Request:** {params.description}
**Environment:** {params.environment}
**Region:** {params.region}
**Resource Prefix:** {params.resource_prefix}

### Naming Convention
Use the pattern: `{params.resource_prefix}-<resourceType>-{params.environment}-<instance>`

### Required Tags
All resources must include these tags:
- `environment`: {params.environment}
- `managedBy`: InfraForge
- `project`: {params.resource_prefix}

### Reference Patterns
{reference}

### Best Practices to Apply
- Use `@secure()` decorator for sensitive parameters
- Use managed identities instead of connection strings where possible
- Enable diagnostic settings for all resources
- Use `dependsOn` only when implicit dependencies aren't sufficient
- Parameterize SKUs so environments can use different tiers
- Add `@description()` decorators to all parameters
- Include outputs for resource IDs and endpoints needed downstream

Please generate a complete, deployable Bicep template following these patterns and practices.
"""
    return result
