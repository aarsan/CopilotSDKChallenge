"""
GitHub Actions pipeline generator tool.
Produces CI/CD workflow YAML for GitHub Actions.
"""

from pydantic import BaseModel, Field
from copilot import define_tool

from src.templates.pipeline_patterns import get_github_actions_reference


class GenerateGitHubActionsParams(BaseModel):
    description: str = Field(
        description=(
            "A description of the application and deployment requirements. "
            "Example: 'Python FastAPI app deploying to Azure App Service with staging and prod'"
        )
    )
    app_type: str = Field(
        default="python",
        description="Application type: python, node, dotnet, java, or container",
    )
    deploy_target: str = Field(
        default="azure-app-service",
        description=(
            "Deployment target: azure-app-service, azure-functions, azure-aks, "
            "azure-container-apps, azure-static-web-apps, or custom"
        ),
    )
    environments: list[str] = Field(
        default=["dev", "staging", "prod"],
        description="List of deployment environments in order",
    )
    include_security_scanning: bool = Field(
        default=True,
        description="Include security scanning steps (CodeQL, dependency review)",
    )


@define_tool(description=(
    "Generate a production-ready GitHub Actions CI/CD workflow from a description. "
    "Use this tool when the user wants to create a GitHub Actions pipeline. "
    "The tool provides reference patterns for multi-environment deployments, "
    "security scanning, approval gates, and reusable workflows."
))
async def generate_github_actions_pipeline(params: GenerateGitHubActionsParams) -> str:
    """Generate GitHub Actions workflow guidance and reference patterns."""
    reference = get_github_actions_reference(params.app_type)

    envs_str = " → ".join(params.environments)
    security_note = ""
    if params.include_security_scanning:
        security_note = """
### Security Scanning Steps to Include
- **CodeQL Analysis** — Static analysis for security vulnerabilities
- **Dependency Review** — Check for vulnerable dependencies on PRs
- **Secret Scanning** — Ensure no secrets in code
- **OIDC Authentication** — Use federated credentials, no stored secrets
"""

    result = f"""## GitHub Actions Pipeline Generation Context

**Request:** {params.description}
**App Type:** {params.app_type}
**Deploy Target:** {params.deploy_target}
**Environments:** {envs_str}

### Pipeline Structure
```yaml
# .github/workflows/ci-cd.yml
# Triggers: push to main, pull_request
# Jobs: build → test → scan → deploy-dev → deploy-staging → deploy-prod
```

### Environment Promotion
{"→ ".join(f"[{env}]" for env in params.environments)}
- Production deployments require manual approval
- Each environment has its own set of secrets/variables

{security_note}

### Reference Patterns
{reference}

### Best Practices to Apply
- Use `workflow_dispatch` for manual triggers with environment input
- Use environment protection rules and required reviewers for prod
- Cache dependencies for faster builds
- Use OIDC (`azure/login` with federated credentials) — no stored secrets
- Pin action versions with full SHA, not just tags
- Use concurrency groups to prevent duplicate deployments
- Add status badges to README
- Include artifact upload/download between jobs
- Use matrix strategy for multi-version testing where applicable

Please generate a complete GitHub Actions workflow YAML following these patterns.
"""
    return result
