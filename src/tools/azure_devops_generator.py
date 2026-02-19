"""
Azure DevOps pipeline generator tool.
Produces CI/CD pipeline YAML for Azure DevOps.
"""

from pydantic import BaseModel, Field
from copilot import define_tool

from src.templates.pipeline_patterns import get_azure_devops_reference


class GenerateAzureDevOpsParams(BaseModel):
    description: str = Field(
        description=(
            "A description of the application and deployment requirements. "
            "Example: '.NET API deploying to AKS with dev, staging, and prod stages'"
        )
    )
    app_type: str = Field(
        default="dotnet",
        description="Application type: python, node, dotnet, java, or container",
    )
    deploy_target: str = Field(
        default="azure-app-service",
        description=(
            "Deployment target: azure-app-service, azure-functions, azure-aks, "
            "azure-container-apps, or custom"
        ),
    )
    environments: list[str] = Field(
        default=["dev", "staging", "prod"],
        description="List of deployment environments/stages in order",
    )
    use_templates: bool = Field(
        default=True,
        description="Use YAML template patterns for reusability",
    )


@define_tool(description=(
    "Generate a production-ready Azure DevOps pipeline YAML from a description. "
    "Use this tool when the user wants to create an Azure DevOps (ADO) pipeline. "
    "The tool provides reference patterns for multi-stage deployments, "
    "template usage, variable groups, and approval gates."
))
async def generate_azure_devops_pipeline(params: GenerateAzureDevOpsParams) -> str:
    """Generate Azure DevOps pipeline guidance and reference patterns."""
    reference = get_azure_devops_reference(params.app_type)

    envs_str = " → ".join(params.environments)
    template_note = ""
    if params.use_templates:
        template_note = """
### Template Structure
```
pipelines/
  azure-pipelines.yml        — Main pipeline definition
  templates/
    build.yml                — Reusable build template
    deploy.yml               — Reusable deploy template
    security-scan.yml        — Security scanning template
```
"""

    result = f"""## Azure DevOps Pipeline Generation Context

**Request:** {params.description}
**App Type:** {params.app_type}
**Deploy Target:** {params.deploy_target}
**Stages:** {envs_str}

### Pipeline Structure
```yaml
# azure-pipelines.yml
# Trigger: main branch
# Stages: Build → Test → Deploy-Dev → Deploy-Staging → Deploy-Prod
```

{template_note}

### Reference Patterns
{reference}

### Best Practices to Apply
- Use `stages` with environment-specific `deploymentJobs`
- Use Variable Groups linked to Azure Key Vault
- Add approval gates and checks on environments
- Use service connections with workload identity federation
- Use YAML templates for DRY pipeline definitions
- Include `dependsOn` and `condition` for stage orchestration
- Cache package managers (pip, npm, NuGet) for speed
- Use `resources.repositories` for shared template repos
- Include test result publishing with `PublishTestResults@2`
- Add deployment annotations and release gates

Please generate a complete Azure DevOps pipeline YAML following these patterns.
"""
    return result
