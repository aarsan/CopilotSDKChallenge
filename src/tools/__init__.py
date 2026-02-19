"""
InfraForge custom tools for the Copilot SDK agent.
"""

from src.tools.catalog_search import search_template_catalog
from src.tools.catalog_compose import compose_from_catalog
from src.tools.catalog_register import register_template
from src.tools.bicep_generator import generate_bicep
from src.tools.terraform_generator import generate_terraform
from src.tools.github_actions_generator import generate_github_actions_pipeline
from src.tools.azure_devops_generator import generate_azure_devops_pipeline
from src.tools.diagram_generator import generate_architecture_diagram
from src.tools.design_document import generate_design_document
from src.tools.cost_estimator import estimate_azure_cost
from src.tools.policy_checker import check_policy_compliance
from src.tools.save_output import save_output_to_file
from src.tools.github_publisher import publish_to_github
from src.tools.service_catalog import (
    check_service_approval,
    request_service_approval,
    list_approved_services,
    get_approval_request_status,
    review_approval_request,
)
from src.tools.governance_tools import (
    list_security_standards,
    list_compliance_frameworks,
    list_governance_policies,
)


def get_all_tools() -> list:
    """Return all custom tools for the InfraForge agent.

    Tools are ordered to mirror the enterprise infrastructure lifecycle:
    1. Service governance — check which Azure services are approved
    2. Standards & compliance — security standards, compliance frameworks, org policies
    3. Catalog tools (search → compose → register) — always try reuse first
    4. Generation tools — fallback when catalog has no match
    5. Architecture visualization — diagram + design document
    6. Validation tools — cost estimation and policy checks
    7. Output tools — save results
    """
    return [
        # Service governance (check before everything)
        check_service_approval,
        request_service_approval,
        list_approved_services,
        get_approval_request_status,
        review_approval_request,
        # Standards & compliance
        list_security_standards,
        list_compliance_frameworks,
        list_governance_policies,
        # Catalog-first workflow
        search_template_catalog,
        compose_from_catalog,
        register_template,
        # Generation (fallback)
        generate_bicep,
        generate_terraform,
        generate_github_actions_pipeline,
        generate_azure_devops_pipeline,
        # Architecture visualization
        generate_architecture_diagram,
        generate_design_document,
        # Validation
        estimate_azure_cost,
        check_policy_compliance,
        # Output
        save_output_to_file,
        # Publishing
        publish_to_github,
    ]
