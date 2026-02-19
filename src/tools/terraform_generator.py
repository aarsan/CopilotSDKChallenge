"""
Terraform configuration generator tool.
Produces Terraform HCL configurations from natural-language descriptions.
"""

from pydantic import BaseModel, Field
from copilot import define_tool

from src.templates.terraform_patterns import get_terraform_reference


class GenerateTerraformParams(BaseModel):
    description: str = Field(
        description=(
            "A natural-language description of the infrastructure to generate. "
            "Example: 'A Kubernetes cluster with a managed database and storage account'"
        )
    )
    provider: str = Field(
        default="azurerm",
        description="Terraform provider: azurerm, aws, google, or multi-cloud",
    )
    environment: str = Field(
        default="dev",
        description="Target environment: dev, staging, or prod",
    )
    backend: str = Field(
        default="azurerm",
        description="Terraform backend type for state storage: azurerm, s3, gcs, or local",
    )


@define_tool(description=(
    "Generate a production-ready Terraform configuration from a natural-language description. "
    "Use this tool when the user wants to create infrastructure using Terraform/HCL. "
    "The tool provides reference patterns for proper module structure, state management, "
    "variable definitions, and provider configuration."
))
async def generate_terraform(params: GenerateTerraformParams) -> str:
    """Generate Terraform configuration guidance and reference patterns."""
    reference = get_terraform_reference(params.provider)

    result = f"""## Terraform Generation Context

**Request:** {params.description}
**Provider:** {params.provider}
**Environment:** {params.environment}
**State Backend:** {params.backend}

### File Structure to Generate
```
main.tf          — Primary resource definitions
variables.tf     — Input variable declarations
outputs.tf       — Output value declarations
providers.tf     — Provider and backend configuration
terraform.tfvars — Default variable values for {params.environment}
```

### Reference Patterns
{reference}

### Best Practices to Apply
- Use `terraform {{ }}` block with required_providers and version constraints
- Configure remote state backend ({params.backend})
- Use locals for computed values and naming conventions
- Tag all resources with environment, project, and managed-by
- Use data sources instead of hardcoded IDs
- Define sensitive variables with `sensitive = true`
- Use `for_each` over `count` for named resources
- Include lifecycle blocks where appropriate (prevent_destroy for databases)
- Add validation blocks to variables where applicable

Please generate a complete, modular Terraform configuration following these patterns.
"""
    return result
