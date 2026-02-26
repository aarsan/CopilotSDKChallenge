"""
CISO (Chief Information Security Officer) tools.

Provides policy modification, exception management, and enforcement
tools for the CISO Advisor and Concierge agents. These tools have
elevated authority — they can actually change policies, not just
request changes.
"""

import json as _json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from pydantic import BaseModel, Field
from copilot import define_tool

from src.database import (
    get_governance_policies,
    upsert_governance_policy,
)

logger = logging.getLogger("infraforge.tools.ciso")

# ── In-memory policy exception store ─────────────────────────
# Maps exception_id → exception dict.  In production this would be a
# database table; for now we keep it in memory per server lifecycle.
_policy_exceptions: dict[str, dict] = {}


# ══════════════════════════════════════════════════════════════
#  TOOL: Modify Governance Policy
# ══════════════════════════════════════════════════════════════

class ModifyGovernancePolicyParams(BaseModel):
    policy_id: str = Field(
        description="The ID of the governance policy to modify (e.g. 'GOV-006').",
    )
    changes: str = Field(
        description=(
            "A JSON string describing what to change. Supported keys: "
            "'description' (new description text), "
            "'severity' (critical|high|medium|low), "
            "'enforcement' (block|warn|audit), "
            "'rule_value' (new rule value — type depends on policy). "
            "Example: {\"enforcement\": \"warn\", \"description\": \"Updated to allow Firewalls\"}"
        ),
    )
    reason: str = Field(
        description="Brief explanation of WHY the policy is being modified (for audit trail).",
    )


@define_tool(description=(
    "Modify an existing governance policy's enforcement level, severity, description, "
    "or rule value. Use this when a policy needs to be permanently adjusted — for example, "
    "changing enforcement from 'block' to 'warn' to allow teams to proceed while maintaining "
    "visibility. ALWAYS look up the policy first with list_governance_policies to understand "
    "its current configuration before modifying."
))
async def modify_governance_policy(params: ModifyGovernancePolicyParams) -> str:
    """Modify a governance policy."""

    # Fetch current policy
    all_policies = await get_governance_policies(enabled_only=False)
    current = next((p for p in all_policies if p["id"] == params.policy_id), None)

    if not current:
        return _json.dumps({
            "error": f"Policy '{params.policy_id}' not found.",
            "available_policies": [p["id"] for p in all_policies],
        })

    # Parse changes
    try:
        changes = _json.loads(params.changes)
    except _json.JSONDecodeError:
        return _json.dumps({"error": "Invalid JSON in 'changes' parameter."})

    # Apply changes
    before = {
        "description": current.get("description", ""),
        "severity": current.get("severity", ""),
        "enforcement": current.get("enforcement", ""),
        "rule_value": current.get("rule_value"),
    }

    if "description" in changes:
        current["description"] = changes["description"]
    if "severity" in changes:
        current["severity"] = changes["severity"]
    if "enforcement" in changes:
        current["enforcement"] = changes["enforcement"]
    if "rule_value" in changes:
        current["rule_value"] = changes["rule_value"]

    # Persist
    await upsert_governance_policy(current)

    after = {
        "description": current.get("description", ""),
        "severity": current.get("severity", ""),
        "enforcement": current.get("enforcement", ""),
        "rule_value": current.get("rule_value"),
    }

    logger.info(
        f"[CISO] Policy {params.policy_id} modified: {params.reason} | "
        f"Changes: {_json.dumps(changes)}"
    )

    return _json.dumps({
        "status": "modified",
        "policy_id": params.policy_id,
        "policy_name": current.get("name", ""),
        "before": before,
        "after": after,
        "reason": params.reason,
        "modified_at": datetime.now(timezone.utc).isoformat(),
        "message": (
            f"Policy {params.policy_id} ({current.get('name', '')}) has been "
            f"modified. Changes: {', '.join(f'{k}: {before[k]} → {after[k]}' for k in changes if k in before and before[k] != after[k])}. "
            f"Reason: {params.reason}"
        ),
    })


# ══════════════════════════════════════════════════════════════
#  TOOL: Toggle Policy (Enable / Disable)
# ══════════════════════════════════════════════════════════════

class TogglePolicyParams(BaseModel):
    policy_id: str = Field(
        description="The ID of the governance policy to enable or disable.",
    )
    enabled: bool = Field(
        description="True to enable the policy, False to disable it.",
    )
    reason: str = Field(
        description="Brief explanation of WHY the policy is being toggled.",
    )


@define_tool(description=(
    "Enable or disable a governance policy. Disabling a policy means it will no longer be "
    "enforced during template validation or deployment. Use this when a policy is causing "
    "widespread blocking issues and needs to be temporarily turned off while a proper fix "
    "is developed. The policy remains in the system and can be re-enabled at any time."
))
async def toggle_policy(params: TogglePolicyParams) -> str:
    """Enable or disable a governance policy."""

    all_policies = await get_governance_policies(enabled_only=False)
    current = next((p for p in all_policies if p["id"] == params.policy_id), None)

    if not current:
        return _json.dumps({
            "error": f"Policy '{params.policy_id}' not found.",
            "available_policies": [p["id"] for p in all_policies],
        })

    was_enabled = bool(current.get("enabled", True))
    current["enabled"] = params.enabled

    await upsert_governance_policy(current)

    action = "enabled" if params.enabled else "disabled"
    logger.info(f"[CISO] Policy {params.policy_id} {action}: {params.reason}")

    return _json.dumps({
        "status": action,
        "policy_id": params.policy_id,
        "policy_name": current.get("name", ""),
        "was_enabled": was_enabled,
        "now_enabled": params.enabled,
        "reason": params.reason,
        "modified_at": datetime.now(timezone.utc).isoformat(),
        "message": (
            f"Policy {params.policy_id} ({current.get('name', '')}) has been "
            f"**{action}**. Reason: {params.reason}"
        ),
    })


# ══════════════════════════════════════════════════════════════
#  TOOL: Grant Policy Exception
# ══════════════════════════════════════════════════════════════

class GrantPolicyExceptionParams(BaseModel):
    policy_id: str = Field(
        description="The ID of the governance policy to grant an exception for.",
    )
    scope: str = Field(
        description=(
            "What the exception applies to — e.g., a service type like "
            "'Microsoft.Network/azureFirewalls', a specific template name, "
            "or 'all' for a blanket exception."
        ),
    )
    duration_days: int = Field(
        default=30,
        description="How many days the exception is valid for (default: 30, max: 365).",
    )
    conditions: str = Field(
        default="",
        description=(
            "Any conditions that must be met for the exception to apply. "
            "E.g., 'Only for production firewall deployments with WAF enabled'."
        ),
    )
    reason: str = Field(
        description="Business justification for granting the exception.",
    )


@define_tool(description=(
    "Grant a temporary exception to a governance policy. Unlike modifying a policy "
    "(which changes the rule for everyone), an exception allows a specific use case to "
    "bypass a policy for a defined period. Exceptions have an expiration date and optional "
    "conditions. Use this when a policy is correct in general but needs a carve-out for "
    "a legitimate business need."
))
async def grant_policy_exception(params: GrantPolicyExceptionParams) -> str:
    """Grant a temporary policy exception."""

    # Validate policy exists
    all_policies = await get_governance_policies(enabled_only=False)
    current = next((p for p in all_policies if p["id"] == params.policy_id), None)

    if not current:
        return _json.dumps({
            "error": f"Policy '{params.policy_id}' not found.",
        })

    # Cap duration
    duration = min(max(params.duration_days, 1), 365)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=duration)

    exc_id = f"EXC-{params.policy_id}-{now.strftime('%Y%m%d%H%M%S')}"

    exception = {
        "id": exc_id,
        "policy_id": params.policy_id,
        "policy_name": current.get("name", ""),
        "scope": params.scope,
        "conditions": params.conditions,
        "reason": params.reason,
        "granted_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "duration_days": duration,
        "status": "active",
    }

    _policy_exceptions[exc_id] = exception

    logger.info(
        f"[CISO] Exception granted: {exc_id} for {params.policy_id} "
        f"scope={params.scope} duration={duration}d reason={params.reason}"
    )

    return _json.dumps({
        "status": "granted",
        "exception": exception,
        "message": (
            f"Exception **{exc_id}** granted for policy {params.policy_id} "
            f"({current.get('name', '')}). Scope: {params.scope}. "
            f"Valid for {duration} days (expires {expires.strftime('%Y-%m-%d')}). "
            + (f"Conditions: {params.conditions}. " if params.conditions else "")
            + f"Reason: {params.reason}"
        ),
    })


# ══════════════════════════════════════════════════════════════
#  TOOL: List Policy Exceptions
# ══════════════════════════════════════════════════════════════

class ListPolicyExceptionsParams(BaseModel):
    policy_id: str = Field(
        default="",
        description="Filter by policy ID. Leave empty to list all exceptions.",
    )
    active_only: bool = Field(
        default=True,
        description="If true, only show exceptions that haven't expired.",
    )


@define_tool(description=(
    "List active policy exceptions. Shows all temporary bypasses that have been "
    "granted, including their scope, conditions, expiration dates, and reasons."
))
async def list_policy_exceptions(params: ListPolicyExceptionsParams) -> str:
    """List policy exceptions."""

    now = datetime.now(timezone.utc)
    results = []

    for exc in _policy_exceptions.values():
        # Filter by policy
        if params.policy_id and exc["policy_id"] != params.policy_id:
            continue

        # Check expiration
        expires = datetime.fromisoformat(exc["expires_at"])
        is_active = expires > now
        exc["status"] = "active" if is_active else "expired"

        if params.active_only and not is_active:
            continue

        results.append(exc)

    return _json.dumps({
        "exceptions": results,
        "total": len(results),
        "message": (
            f"Found {len(results)} {'active ' if params.active_only else ''}exception(s)"
            + (f" for policy {params.policy_id}" if params.policy_id else "")
            + "."
        ),
    })
