"""
Azure Service Catalog tools.

Provides tools to check whether requested Azure services are approved for use
within the organization, and to submit approval requests for services that
are not yet approved. This is a core governance feature that ensures teams
only use vetted, policy-compliant Azure services.

All data is read from the InfraForge database.
No YAML files are read at runtime.
"""

import json
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field
from copilot import define_tool

from src.database import (
    get_all_services,
    get_service,
    save_approval_request,
    get_approval_requests,
    update_approval_request,
)


# ── Service catalog loader (database-backed) ─────────────────

async def _load_services(
    category: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """Load the approved services registry from the database."""
    return await get_all_services(category=category, status=status)


def _find_service(services: list[dict], query: str) -> list[dict]:
    """Find services matching a query string (resource type, name, or keyword)."""
    query_lower = query.lower().strip()
    matches = []

    for svc in services:
        score = 0
        svc_id = svc.get("id", "").lower()
        svc_name = svc.get("name", "").lower()
        svc_category = svc.get("category", "").lower()

        # Exact resource type match
        if query_lower == svc_id:
            score += 100

        # Partial resource type match
        elif query_lower in svc_id:
            score += 50

        # Name match
        if query_lower in svc_name:
            score += 40

        # Individual word matches
        for word in query_lower.split():
            if word in svc_id:
                score += 15
            if word in svc_name:
                score += 10
            if word in svc_category:
                score += 5

        if score > 0:
            matches.append((score, svc))

    matches.sort(key=lambda x: x[0], reverse=True)
    return [m[1] for m in matches]


def _format_service_status(svc: dict) -> str:
    """Format a service entry into a human-readable status report."""
    status = svc.get("status", "unknown")
    status_icons = {
        "approved": "✅",
        "conditional": "⚠️",
        "under_review": "🔍",
        "not_approved": "❌",
    }
    icon = status_icons.get(status, "❓")

    lines = [
        f"### {icon} {svc.get('name', 'Unknown')}",
        f"- **Resource Type:** `{svc.get('id', 'N/A')}`",
        f"- **Status:** {status.replace('_', ' ').title()}",
        f"- **Category:** {svc.get('category', 'N/A')}",
        f"- **Risk Tier:** {svc.get('risk_tier', 'N/A')}",
    ]

    if status == "approved":
        if svc.get("approved_skus"):
            lines.append(f"- **Approved SKUs:** {', '.join(svc['approved_skus'])}")
        if svc.get("approved_regions"):
            lines.append(f"- **Approved Regions:** {', '.join(svc['approved_regions'])}")
        if svc.get("policies"):
            lines.append("- **Policies:**")
            for p in svc["policies"]:
                lines.append(f"  - {p}")
        if svc.get("approved_date"):
            lines.append(f"- **Approved:** {svc['approved_date']} by {svc.get('reviewed_by', 'N/A')}")

    elif status == "conditional":
        if svc.get("conditions"):
            lines.append("- **Conditions (must be met):**")
            for c in svc["conditions"]:
                lines.append(f"  - ⚠️ {c}")
        if svc.get("approved_skus"):
            lines.append(f"- **Approved SKUs:** {', '.join(svc['approved_skus'])}")
        if svc.get("approved_regions"):
            lines.append(f"- **Approved Regions:** {', '.join(svc['approved_regions'])}")
        if svc.get("policies"):
            lines.append("- **Additional Policies:**")
            for p in svc["policies"]:
                lines.append(f"  - {p}")

    elif status == "under_review":
        if svc.get("review_notes"):
            lines.append(f"- **Review Notes:** {svc['review_notes']}")
        if svc.get("contact"):
            lines.append(f"- **Contact:** {svc['contact']}")
        lines.append("")
        lines.append("🔄 This service is currently being evaluated. You can submit a request to expedite the review.")

    elif status == "not_approved":
        if svc.get("rejection_reason"):
            lines.append(f"- **Reason:** {svc['rejection_reason']}")
        if svc.get("contact"):
            lines.append(f"- **Contact:** {svc['contact']}")
        lines.append("")
        lines.append("🚫 This service is not approved. You can submit a Service Approval Request to begin the review process.")

    if svc.get("documentation"):
        lines.append(f"- **Documentation:** {svc['documentation']}")

    return "\n".join(lines)


# ── Approval request helpers (database-backed) ───────────────

async def _save_approval_request(request: dict) -> str:
    """Save an approval request to the database. Returns the request ID."""
    request_id = f"SAR-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    request["id"] = request_id
    request["status"] = "submitted"
    request["submitted_at"] = datetime.now(timezone.utc).isoformat()
    await save_approval_request(request)
    return request_id


async def _list_approval_requests() -> list[dict]:
    """List all stored approval requests from the database."""
    return await get_approval_requests()


# ── Tool 1: Check Service Approval ───────────────────────────

class CheckServiceApprovalParams(BaseModel):
    services: str = Field(
        description=(
            "Comma-separated list of Azure service names or resource types to check. "
            "Examples: 'App Service, SQL Database, Key Vault' or "
            "'Microsoft.Web/sites, Microsoft.Sql/servers/databases'"
        )
    )


@define_tool(description=(
    "Check whether Azure services are approved for use in the organization. "
    "Call this tool whenever a user requests infrastructure to verify all required Azure services "
    "are approved, conditionally approved, under review, or not yet approved. "
    "For non-approved services, provides guidance on the approval process. "
    "For conditionally approved services, lists the restrictions that must be met. "
    "Accepts service names (e.g., 'App Service') or resource types (e.g., 'Microsoft.Web/sites')."
))
async def check_service_approval(params: CheckServiceApprovalParams) -> str:
    """Check approval status for one or more Azure services."""
    services = await _load_services()
    if not services:
        return "⚠️ Service catalog is empty or not found. Cannot validate service approval status."

    queries = [q.strip() for q in params.services.split(",") if q.strip()]
    if not queries:
        return "No services specified. Please provide service names or resource types to check."

    results = []
    all_approved = True
    blockers = []
    conditionals = []

    for query in queries:
        matches = _find_service(services, query)
        if not matches:
            all_approved = False
            blockers.append(query)
            results.append(
                f"### ❓ {query}\n"
                f"- **Status:** Not found in service catalog\n"
                f"- This service is not in the approved services registry.\n"
                f"- It may need to go through the Service Approval Process before use.\n"
                f"- Contact: platform-team@contoso.com"
            )
        else:
            best_match = matches[0]
            status = best_match.get("status", "unknown")
            results.append(_format_service_status(best_match))

            if status == "not_approved":
                all_approved = False
                blockers.append(best_match.get("name", query))
            elif status == "under_review":
                all_approved = False
                blockers.append(best_match.get("name", query))
            elif status == "conditional":
                conditionals.append(best_match.get("name", query))

    # Summary header
    header_lines = ["# 🏢 Service Approval Check\n"]

    if all_approved and not conditionals:
        header_lines.append("✅ **All requested services are fully approved.** Proceed with infrastructure provisioning.\n")
    elif all_approved and conditionals:
        header_lines.append(
            f"⚠️ **All services are available, but {len(conditionals)} have conditions** that must be met: "
            f"{', '.join(conditionals)}. Review the conditions below before proceeding.\n"
        )
    else:
        header_lines.append(
            f"🚧 **{len(blockers)} service(s) are NOT approved for use:** {', '.join(blockers)}.\n"
            f"These must go through the Service Approval Process before they can be used in production.\n"
            f"You can submit a Service Approval Request using the `request_service_approval` tool.\n"
        )

    header_lines.append("---\n")
    return "\n".join(header_lines) + "\n\n".join(results)


# ── Tool 2: Request Service Approval ─────────────────────────

class RequestServiceApprovalParams(BaseModel):
    service_name: str = Field(
        description="The Azure service name or resource type to request approval for."
    )
    business_justification: str = Field(
        description=(
            "Business justification explaining why this service is needed and "
            "why existing approved alternatives won't work."
        )
    )
    project_name: str = Field(
        description="The project or application that needs this service."
    )
    environment: str = Field(
        default="production",
        description="Target environment: development, staging, or production."
    )
    requestor_name: str = Field(
        default="",
        description="Name of the person requesting the service (auto-filled from user context if available)."
    )
    requestor_email: str = Field(
        default="",
        description="Email of the requestor (auto-filled from user context if available)."
    )


@define_tool(description=(
    "Submit a Service Approval Request to get a non-approved Azure service added to the "
    "organization's approved services catalog. Use this when `check_service_approval` indicates "
    "a service is not approved or not found. The request is stored and routed to the platform team "
    "for review. Requires a business justification explaining why the service is needed and "
    "why approved alternatives won't work."
))
async def request_service_approval(params: RequestServiceApprovalParams) -> str:
    """Submit a service approval request."""
    services = await _load_services()

    # Check if the service is already approved
    matches = _find_service(services, params.service_name)
    if matches:
        best = matches[0]
        if best.get("status") == "approved":
            return (
                f"ℹ️ **{best['name']}** is already fully approved!\n\n"
                f"No approval request needed. You can use this service immediately.\n\n"
                + _format_service_status(best)
            )

    # Determine risk tier from known service info
    risk_tier = "medium"  # default
    review_time = "1-2 weeks"
    if matches:
        risk_tier = matches[0].get("risk_tier", "medium")
    if risk_tier in ("high", "critical"):
        review_time = "2-4 weeks"

    # Build the approval request
    request = {
        "service_name": params.service_name,
        "service_resource_type": matches[0]["id"] if matches else "unknown",
        "current_status": matches[0]["status"] if matches else "not_in_catalog",
        "risk_tier": risk_tier,
        "business_justification": params.business_justification,
        "project_name": params.project_name,
        "environment": params.environment,
        "requestor": {
            "name": params.requestor_name or "Not specified",
            "email": params.requestor_email or "Not specified",
        },
    }

    request_id = await _save_approval_request(request)

    # Format response
    lines = [
        "# 📋 Service Approval Request Submitted\n",
        f"**Request ID:** `{request_id}`\n",
        f"| Field | Value |",
        f"|-------|-------|",
        f"| **Service** | {params.service_name} |",
        f"| **Resource Type** | {request['service_resource_type']} |",
        f"| **Current Status** | {request['current_status'].replace('_', ' ').title()} |",
        f"| **Risk Tier** | {risk_tier.title()} |",
        f"| **Project** | {params.project_name} |",
        f"| **Environment** | {params.environment} |",
        f"| **Requestor** | {request['requestor']['name']} ({request['requestor']['email']}) |",
        f"| **Estimated Review** | {review_time} |",
        "",
        "### Business Justification",
        params.business_justification,
        "",
        "---",
        "",
        "### Next Steps",
        f"1. Your request `{request_id}` has been logged and will be routed to the **Platform Engineering** team",
        f"2. Expected review timeline: **{review_time}** (based on {risk_tier} risk tier)",
        "3. The review covers: security, compliance, cost, and operational readiness",
        "4. You'll receive one of these outcomes:",
        "   - ✅ **Approved** — Service added to catalog with usage policies",
        "   - ⚠️ **Conditional** — Approved with restrictions (e.g., specific SKUs/regions)",
        "   - ⏳ **Deferred** — Needs more evaluation or depends on another initiative",
        "   - ❌ **Rejected** — With rationale and recommended alternatives",
        "",
        "**Contact:** platform-team@contoso.com for questions about this request.",
    ]

    return "\n".join(lines)


# ── Tool 3: List Service Categories ──────────────────────────

class ListApprovedServicesParams(BaseModel):
    category: str = Field(
        default="",
        description=(
            "Filter by category: compute, database, security, storage, monitoring, "
            "networking, ai, other. Leave empty to list all services."
        )
    )
    status: str = Field(
        default="",
        description=(
            "Filter by approval status: approved, conditional, under_review, not_approved. "
            "Leave empty to list all statuses."
        )
    )


@define_tool(description=(
    "List all Azure services in the organization's approved services catalog. "
    "Can filter by category (compute, database, security, etc.) and by approval status "
    "(approved, conditional, under_review, not_approved). "
    "Useful for discovering what services are available and understanding the organization's "
    "cloud service governance posture."
))
async def list_approved_services(params: ListApprovedServicesParams) -> str:
    """List approved services with optional filtering."""
    filtered = await _load_services(
        category=params.category or None,
        status=params.status or None,
    )
    if not filtered:
        filter_desc = []
        if params.category:
            filter_desc.append(f"category={params.category}")
        if params.status:
            filter_desc.append(f"status={params.status}")
        if filter_desc:
            return f"No services found matching filters: {', '.join(filter_desc)}"
        return "⚠️ Service catalog is empty. Database may not be seeded yet."

    # Group by status
    status_groups: dict[str, list[dict]] = {}
    for svc in filtered:
        status = svc.get("status", "unknown")
        status_groups.setdefault(status, []).append(svc)

    status_icons = {
        "approved": "✅",
        "conditional": "⚠️",
        "under_review": "🔍",
        "not_approved": "❌",
    }

    lines = ["# 🏢 Azure Service Catalog\n"]

    if params.category:
        lines.append(f"**Category:** {params.category.title()}\n")
    lines.append(f"**Total services:** {len(filtered)}\n")

    # Summary counts
    for status in ["approved", "conditional", "under_review", "not_approved"]:
        count = len(status_groups.get(status, []))
        if count > 0:
            icon = status_icons.get(status, "❓")
            lines.append(f"- {icon} {status.replace('_', ' ').title()}: **{count}**")

    lines.append("\n---\n")

    # Detail per status group
    for status in ["approved", "conditional", "under_review", "not_approved"]:
        group = status_groups.get(status, [])
        if not group:
            continue

        icon = status_icons.get(status, "❓")
        lines.append(f"## {icon} {status.replace('_', ' ').title()}\n")

        for svc in group:
            risk = svc.get("risk_tier", "N/A")
            risk_badge = {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}.get(risk, "⚪")
            lines.append(f"- **{svc['name']}** ({svc['id']}) — {risk_badge} {risk} risk")

        lines.append("")

    return "\n".join(lines)


# ── Tool 4: Get Approval Request Status ──────────────────────

class GetApprovalRequestStatusParams(BaseModel):
    request_id: str = Field(
        default="",
        description=(
            "Specific approval request ID to check (e.g., 'SAR-20250218123456'). "
            "Leave empty to list all pending requests."
        ),
    )
    requestor_email: str = Field(
        default="",
        description=(
            "Filter requests by requestor email. Auto-filled from user context if available. "
            "Leave empty to see all requests (admin view)."
        ),
    )


@define_tool(description=(
    "Check the status of Service Approval Requests. Use this to track whether a previously "
    "submitted request has been reviewed, approved, conditionally approved, or denied by IT. "
    "Call with a specific request ID, or leave empty to list all pending requests. "
    "This helps users understand what services are in the pipeline and when they can deploy "
    "their ideal architecture."
))
async def get_approval_request_status(params: GetApprovalRequestStatusParams) -> str:
    """Check status of approval requests."""
    requests = await _list_approval_requests()

    if not requests:
        return (
            "# 📋 Approval Requests\n\n"
            "No approval requests found. You can submit one using `request_service_approval` "
            "when you need a service that isn't yet approved."
        )

    # Filter by specific ID
    if params.request_id:
        matching = [r for r in requests if r.get("id") == params.request_id]
        if not matching:
            return f"❓ No approval request found with ID `{params.request_id}`."
        return _format_approval_detail(matching[0])

    # Filter by requestor
    if params.requestor_email:
        requests = [
            r for r in requests
            if (r.get("requestor_email", "") == params.requestor_email
                or r.get("requestor", {}).get("email", "") == params.requestor_email)
        ]

    if not requests:
        return "No approval requests found for your account."

    # Group by status
    status_groups: dict[str, list[dict]] = {}
    for req in requests:
        s = req.get("status", "submitted")
        status_groups.setdefault(s, []).append(req)

    status_icons = {
        "submitted": "📨", "in_review": "🔍", "approved": "✅",
        "conditional": "⚠️", "denied": "❌", "deferred": "⏳",
    }

    lines = [
        "# 📋 Service Approval Requests\n",
        f"**Total requests:** {len(requests)}\n",
        "| Request ID | Service | Status | Submitted |",
        "|------------|---------|--------|-----------|",
    ]

    for req in requests:
        status = req.get("status", "submitted")
        icon = status_icons.get(status, "❓")
        submitted = req.get("submitted_at", "N/A")[:10]
        svc = req.get("service_name", "Unknown")
        rid = req.get("id", "N/A")
        lines.append(f"| `{rid}` | {svc} | {icon} {status.replace('_', ' ').title()} | {submitted} |")

    lines.append("")

    # Show details for reviewed items
    for status in ["approved", "conditional", "denied", "deferred"]:
        group = status_groups.get(status, [])
        for req in group:
            lines.append("\n---\n")
            lines.append(_format_approval_detail(req))

    # Guidance
    pending = len(status_groups.get("submitted", [])) + len(status_groups.get("in_review", []))
    if pending > 0:
        lines.extend([
            "\n---\n",
            f"⏳ **{pending} request(s) pending review.** The platform team typically reviews:",
            "- Low/Medium risk: **1-2 weeks**",
            "- High/Critical risk: **2-4 weeks**",
            "",
            "While waiting, I can help you design a **Phase 1** deployment using only approved "
            "services, ready to deploy today.",
        ])

    return "\n".join(lines)


def _format_approval_detail(req: dict) -> str:
    """Format a single approval request into a detailed view."""
    status = req.get("status", "submitted")
    status_icons = {
        "submitted": "📨", "in_review": "🔍", "approved": "✅",
        "conditional": "⚠️", "denied": "❌", "deferred": "⏳",
    }
    icon = status_icons.get(status, "❓")

    lines = [
        f"### {icon} {req.get('service_name', 'Unknown')} — `{req.get('id', 'N/A')}`",
        f"- **Status:** {status.replace('_', ' ').title()}",
        f"- **Project:** {req.get('project_name', 'N/A')}",
        f"- **Environment:** {req.get('environment', 'N/A')}",
        f"- **Risk Tier:** {req.get('risk_tier', 'N/A')}",
        f"- **Submitted:** {req.get('submitted_at', 'N/A')[:19]}",
    ]

    requestor = req.get("requestor", {})
    if isinstance(requestor, dict):
        lines.append(f"- **Requestor:** {requestor.get('name', req.get('requestor_name', 'N/A'))} "
                      f"({requestor.get('email', req.get('requestor_email', 'N/A'))})")

    if req.get("business_justification"):
        lines.extend(["", "**Business Justification:**", req["business_justification"]])

    if req.get("reviewed_at"):
        lines.append(f"\n- **Reviewed:** {req['reviewed_at'][:19]}")
    if req.get("reviewer"):
        lines.append(f"- **Reviewed by:** {req['reviewer']}")
    if req.get("review_notes"):
        lines.extend(["", "**Review Notes:**", req["review_notes"]])

    # Action guidance based on status
    if status == "approved":
        lines.extend([
            "", "✅ **This service is now approved!** You can use it in your infrastructure designs.",
            "The service catalog has been updated. Ask me to generate your architecture."
        ])
    elif status == "conditional":
        lines.extend([
            "", "⚠️ **Approved with conditions.** Review the restrictions above before proceeding."
        ])
    elif status == "denied":
        lines.extend([
            "", "❌ **Request denied.** I can suggest approved alternatives that meet your requirements."
        ])
    elif status in ("submitted", "in_review"):
        lines.extend([
            "", "⏳ In the meantime, I can design a **Phase 1** deployment using only approved "
            "services, ready to deploy today."
        ])

    return "\n".join(lines)


# ── Tool 5: Review Approval Request (IT Admin) ───────────────

class ReviewApprovalRequestParams(BaseModel):
    request_id: str = Field(
        description="The approval request ID to review (e.g., 'SAR-20250218123456')."
    )
    decision: str = Field(
        description=(
            "Review decision: 'approved' (add to catalog), 'conditional' (approved with "
            "restrictions), 'denied' (rejected), or 'deferred' (needs more evaluation)."
        )
    )
    reviewer_name: str = Field(
        default="",
        description="Name of the reviewer (auto-filled from user context if available)."
    )
    review_notes: str = Field(
        default="",
        description=(
            "Review notes explaining the decision. For 'conditional', describe the restrictions. "
            "For 'denied', explain the rationale and suggest alternatives."
        )
    )


@define_tool(description=(
    "Review and act on a Service Approval Request (Platform Team / IT Admin only). "
    "Approve, conditionally approve, deny, or defer a request. Only available to users "
    "with platform team or admin access. When approving, the service will be added to the "
    "catalog so users can immediately start using it in their designs."
))
async def review_approval_request(params: ReviewApprovalRequestParams) -> str:
    """IT admin reviews an approval request."""
    valid_decisions = {"approved", "conditional", "denied", "deferred"}
    if params.decision not in valid_decisions:
        return f"❌ Invalid decision `{params.decision}`. Must be one of: {', '.join(sorted(valid_decisions))}"

    # Find the request first to confirm it exists
    all_requests = await _list_approval_requests()
    matching = [r for r in all_requests if r.get("id") == params.request_id]
    if not matching:
        return f"❓ No approval request found with ID `{params.request_id}`."

    req = matching[0]
    current_status = req.get("status", "submitted")
    if current_status in ("approved", "denied"):
        return (
            f"⚠️ Request `{params.request_id}` has already been **{current_status}**. "
            "No further action is needed."
        )

    # Perform the update
    try:
        await update_approval_request(
            request_id=params.request_id,
            status=params.decision,
            reviewer=params.reviewer_name or "Platform Team",
            review_notes=params.review_notes,
        )
    except Exception as e:
        return f"❌ Failed to update approval request: {e}"

    decision_icons = {
        "approved": "✅", "conditional": "⚠️", "denied": "❌", "deferred": "⏳",
    }
    icon = decision_icons.get(params.decision, "❓")

    lines = [
        f"# {icon} Approval Request Reviewed\n",
        f"**Request ID:** `{params.request_id}`",
        f"**Service:** {req.get('service_name', 'Unknown')}",
        f"**Decision:** {params.decision.replace('_', ' ').title()}",
        f"**Reviewer:** {params.reviewer_name or 'Platform Team'}",
    ]

    if params.review_notes:
        lines.extend(["", "**Notes:**", params.review_notes])

    if params.decision == "approved":
        lines.extend([
            "", "---", "",
            "The service has been **approved** and the catalog will be updated.",
            "The requestor can now use this service in their infrastructure designs.",
            "",
            "💡 **Next steps:** The platform team should add approved SKUs, regions, and policies to the "
            "service entry in the governance database for full governance."
        ])
    elif params.decision == "conditional":
        lines.extend([
            "", "---", "",
            "The service has been **conditionally approved**. The requestor must comply with "
            "the restrictions noted above. Update the catalog entry with the conditions."
        ])
    elif params.decision == "denied":
        lines.extend([
            "", "---", "",
            "The request has been **denied**. The requestor will see the rationale and can "
            "ask for approved alternatives."
        ])
    elif params.decision == "deferred":
        lines.extend([
            "", "---", "",
            "The request has been **deferred** for further evaluation. The requestor will "
            "be notified when a final decision is made."
        ])

    return "\n".join(lines)
