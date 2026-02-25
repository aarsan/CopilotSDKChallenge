"""
InfraForge — Azure Policy Deployer

Deploys generated Azure Policy definitions and assignments to Azure so that
governance rules are enforced at the Azure Resource Manager level — not just
locally inside InfraForge.

Flow:
    1. create_policy_definition()  — Creates a custom policy definition at
       subscription scope.
    2. assign_policy_to_rg()       — Assigns the definition to a specific
       resource group with deny/audit effect.
    3. cleanup_policy()            — Deletes both the assignment and definition
       (called during validation RG teardown).
"""

import asyncio
import json
import logging
import os
from typing import Optional

logger = logging.getLogger("infraforge.policy_deployer")

# ── Naming convention ─────────────────────────────────────────
# Policy definition:  infraforge-<service-slug>-<run_id>
# Policy assignment:  infraforge-assign-<service-slug>-<run_id>
# Both are scoped to the subscription (definition) and RG (assignment).

_POLICY_NAME_PREFIX = "infraforge"


def _sanitize_slug(service_id: str) -> str:
    """Convert e.g. 'Microsoft.Compute/virtualMachines' → 'compute-virtualmachines'."""
    slug = service_id.lower()
    for ch in ("microsoft.", "/", ".", " "):
        slug = slug.replace(ch, "-")
    # collapse runs of dashes
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:60]


def _get_credential():
    from azure.identity import DefaultAzureCredential
    return DefaultAzureCredential(
        exclude_workload_identity_credential=True,
        exclude_managed_identity_credential=True,
    )


def _get_subscription_id() -> str:
    sub_id = os.getenv("AZURE_SUBSCRIPTION_ID", "")
    if sub_id:
        return sub_id
    raise ValueError("AZURE_SUBSCRIPTION_ID not set")


def _get_policy_client():
    from azure.mgmt.resource.policy import PolicyClient
    return PolicyClient(_get_credential(), _get_subscription_id())


def _build_names(service_id: str, run_id: str) -> tuple[str, str]:
    """Return (definition_name, assignment_name)."""
    slug = _sanitize_slug(service_id)
    def_name = f"{_POLICY_NAME_PREFIX}-{slug}-{run_id}"[:64]
    assign_name = f"{_POLICY_NAME_PREFIX}-assign-{slug}-{run_id}"[:64]
    return def_name, assign_name


# ══════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════

async def create_policy_definition(
    service_id: str,
    run_id: str,
    policy_json: dict,
    display_name: Optional[str] = None,
) -> dict:
    """Create a custom Azure Policy definition at subscription scope.

    Args:
        service_id:   e.g. 'Microsoft.Compute/virtualMachines'
        run_id:       unique pipeline run identifier
        policy_json:  the generated policy dict (with 'properties.policyRule')
        display_name: optional human-readable name

    Returns:
        dict with 'definition_name', 'definition_id', 'subscription_id'
    """
    def_name, _ = _build_names(service_id, run_id)
    sub_id = _get_subscription_id()
    client = _get_policy_client()

    # Extract the policyRule — handle both wrapped and unwrapped formats
    props = policy_json.get("properties", policy_json)
    policy_rule = props.get("policyRule", {})
    if not policy_rule:
        raise ValueError("Policy JSON has no policyRule — cannot deploy")

    _display = display_name or props.get("displayName", f"InfraForge governance — {service_id}")
    _description = (
        f"Auto-deployed by InfraForge (run {run_id}). "
        f"Enforces organization standards for {service_id}."
    )
    _mode = props.get("mode", "All")

    definition_params = {
        "display_name": _display,
        "description": _description,
        "policy_type": "Custom",
        "mode": _mode,
        "policy_rule": policy_rule,
        "metadata": {
            "createdBy": "InfraForge",
            "runId": run_id,
            "serviceType": service_id,
        },
    }

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: client.policy_definitions.create_or_update(
            def_name, definition_params
        ),
    )

    logger.info(
        f"Created policy definition '{def_name}' "
        f"(id: {result.id}) for {service_id}"
    )
    return {
        "definition_name": def_name,
        "definition_id": result.id,
        "subscription_id": sub_id,
    }


async def assign_policy_to_rg(
    service_id: str,
    run_id: str,
    definition_id: str,
    resource_group: str,
    effect: str = "deny",
) -> dict:
    """Assign a policy definition to a resource group.

    Args:
        service_id:     e.g. 'Microsoft.Compute/virtualMachines'
        run_id:         unique pipeline run identifier
        definition_id:  full ARM resource ID of the policy definition
        resource_group: name of the target resource group
        effect:         'deny' or 'audit' (default: deny)

    Returns:
        dict with 'assignment_name', 'assignment_id', 'scope'
    """
    _, assign_name = _build_names(service_id, run_id)
    sub_id = _get_subscription_id()
    scope = f"/subscriptions/{sub_id}/resourceGroups/{resource_group}"
    client = _get_policy_client()

    assignment_params = {
        "display_name": f"InfraForge — {service_id} ({run_id})",
        "description": (
            f"Enforces organization standards for {service_id} "
            f"in resource group {resource_group}."
        ),
        "policy_definition_id": definition_id,
        "enforcement_mode": "Default",  # actively enforced
        "metadata": {
            "createdBy": "InfraForge",
            "runId": run_id,
            "serviceType": service_id,
        },
    }

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: client.policy_assignments.create(
            scope, assign_name, assignment_params
        ),
    )

    logger.info(
        f"Assigned policy '{assign_name}' to scope {scope} "
        f"(id: {result.id})"
    )
    return {
        "assignment_name": assign_name,
        "assignment_id": result.id,
        "scope": scope,
    }


async def deploy_policy(
    service_id: str,
    run_id: str,
    policy_json: dict,
    resource_group: str,
    display_name: Optional[str] = None,
) -> dict:
    """End-to-end: create definition + assign to resource group.

    Returns combined dict with definition and assignment details.
    """
    defn = await create_policy_definition(
        service_id, run_id, policy_json, display_name
    )
    assignment = await assign_policy_to_rg(
        service_id, run_id, defn["definition_id"], resource_group
    )
    return {**defn, **assignment}


async def cleanup_policy(service_id: str, run_id: str, resource_group: str) -> dict:
    """Delete the policy assignment and definition created by a pipeline run.

    Safe to call even if the policy was never deployed — errors are logged
    and swallowed.
    """
    def_name, assign_name = _build_names(service_id, run_id)
    sub_id = _get_subscription_id()
    scope = f"/subscriptions/{sub_id}/resourceGroups/{resource_group}"
    client = _get_policy_client()
    loop = asyncio.get_event_loop()
    deleted = {"assignment": False, "definition": False}

    # 1. Delete assignment first (depends on definition)
    try:
        await loop.run_in_executor(
            None,
            lambda: client.policy_assignments.delete(scope, assign_name),
        )
        deleted["assignment"] = True
        logger.info(f"Deleted policy assignment '{assign_name}' from {scope}")
    except Exception as e:
        logger.debug(f"Policy assignment cleanup (non-fatal): {e}")

    # 2. Delete definition
    try:
        await loop.run_in_executor(
            None,
            lambda: client.policy_definitions.delete(def_name),
        )
        deleted["definition"] = True
        logger.info(f"Deleted policy definition '{def_name}'")
    except Exception as e:
        logger.debug(f"Policy definition cleanup (non-fatal): {e}")

    return deleted
