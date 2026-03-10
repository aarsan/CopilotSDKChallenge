"""
Microsoft Work IQ tools for the Copilot SDK agent.

These tools allow InfraForge to query M365 organizational data (emails,
meetings, documents, Teams messages, people) via Microsoft Work IQ,
enriching infrastructure design documents and agent decisions with
organizational context.
"""

from pydantic import BaseModel, Field
from copilot import define_tool

from src.workiq_client import get_workiq_client


class SearchOrgKnowledgeParams(BaseModel):
    query: str = Field(
        description=(
            "Natural language query to search across M365 data. "
            "Examples: 'architecture decisions about microservices', "
            "'meeting notes about Azure migration', "
            "'documents about API gateway patterns'"
        )
    )


@define_tool(
    description=(
        "Search organizational knowledge across Microsoft 365 using Work IQ. "
        "Queries emails, meetings, documents, Teams messages, and people data "
        "via natural language. Use this to find prior architecture discussions, "
        "meeting notes, specifications, governance decisions, or related context "
        "before generating infrastructure. Returns matching M365 content."
    )
)
async def search_org_knowledge(params: SearchOrgKnowledgeParams) -> str:
    """Search M365 data via Work IQ."""
    client = get_workiq_client()
    result = await client.ask(params.query)
    if result is None:
        return (
            "Work IQ is currently unavailable. Microsoft Work IQ requires Node.js 18+ "
            "and prior authentication via `npx -y @microsoft/workiq accept-eula`. "
            "Proceeding without organizational context."
        )
    return f"## Work IQ Results\n\n{result}"


class FindRelatedDocsParams(BaseModel):
    topic: str = Field(
        description=(
            "Infrastructure topic to search for related documents. "
            "Examples: 'Kubernetes cluster setup', 'CDN configuration', "
            "'database migration strategy'"
        )
    )


@define_tool(
    description=(
        "Find SharePoint and OneDrive documents related to an infrastructure topic "
        "using Microsoft Work IQ. Use this before generating design documents to "
        "discover existing architecture specs, runbooks, or reference documentation "
        "in the organization's M365 environment."
    )
)
async def find_related_documents(params: FindRelatedDocsParams) -> str:
    """Find related M365 documents via Work IQ."""
    client = get_workiq_client()
    result = await client.search_documents(params.topic)
    if result is None:
        return "Work IQ is currently unavailable. Cannot search for related documents."
    return f"## Related Documents\n\n{result}"


class FindExpertsParams(BaseModel):
    domain: str = Field(
        description=(
            "Technical domain or infrastructure pattern to find experts for. "
            "Examples: 'Kubernetes', 'Azure networking', 'CI/CD pipelines', "
            "'cost optimization'"
        )
    )


@define_tool(
    description=(
        "Find subject matter experts in the organization who have experience "
        "with a specific infrastructure domain using Microsoft Work IQ. "
        "Searches across M365 data (emails, meetings, documents, Teams) to "
        "identify people who have worked on or discussed similar topics. "
        "Use this when users need to find reviewers, collaborators, or "
        "experts for their infrastructure requests."
    )
)
async def find_subject_matter_experts(params: FindExpertsParams) -> str:
    """Find SMEs via Work IQ."""
    client = get_workiq_client()
    result = await client.find_experts(params.domain)
    if result is None:
        return "Work IQ is currently unavailable. Cannot search for subject matter experts."
    return f"## Subject Matter Experts\n\n{result}"
