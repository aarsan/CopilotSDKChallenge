"""
InfraForge — ARM Deployment Engine

Deploys infrastructure directly to Azure using ARM JSON templates via the
Azure Python SDK.  No CLI dependencies (no `az`, `terraform`, or `bicep`
required on the deploy path).

Why ARM JSON over Bicep/Terraform?
    - Machine-native: no human-readable syntax to generate and parse
    - Zero extra tooling: azure-mgmt-resource is already in the stack
    - Azure manages state server-side: no state files to store/lock/corrupt
    - Atomic deployments: ARM handles dependency ordering within a template
    - What-If validation: validate before deploying (like `terraform plan`)
    - Idempotent: incremental mode is the default

Tools:
    validate_deployment   — Run ARM What-If to preview changes (like `terraform plan`)
    deploy_infrastructure — Deploy an ARM template to Azure with live progress
    get_deployment_status — Check the status of a running or completed deployment
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable

from pydantic import BaseModel, Field
from copilot import define_tool

logger = logging.getLogger("infraforge.deploy_engine")


# ══════════════════════════════════════════════════════════════
# DEPLOYMENT MANAGER — process-wide singleton
# ══════════════════════════════════════════════════════════════

ProgressCallback = Optional[Callable[[dict], Awaitable[None]]]


class DeploymentRecord:
    """In-memory record of a deployment (also persisted to DB)."""

    def __init__(
        self,
        deployment_id: str,
        resource_group: str,
        deployment_name: str,
        region: str,
        template_hash: str,
        initiated_by: str = "agent",
    ):
        self.deployment_id = deployment_id
        self.resource_group = resource_group
        self.deployment_name = deployment_name
        self.region = region
        self.template_hash = template_hash
        self.initiated_by = initiated_by
        self.template_id = ""           # catalog template ID if from template deploy
        self.template_name = ""         # human-readable template name
        self.subscription_id = ""        # filled at deploy time
        self.status = "pending"          # pending → validating → deploying → succeeded / failed
        self.phase = "init"
        self.progress = 0.0
        self.detail = ""
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.completed_at: Optional[str] = None
        self.what_if_results: Optional[dict] = None
        self.provisioned_resources: list[dict] = []
        self.error: Optional[str] = None
        self.events: list[dict] = []     # timeline of progress events

    def to_dict(self) -> dict:
        return {
            "deployment_id": self.deployment_id,
            "resource_group": self.resource_group,
            "deployment_name": self.deployment_name,
            "region": self.region,
            "subscription_id": self.subscription_id,
            "status": self.status,
            "phase": self.phase,
            "progress": self.progress,
            "detail": self.detail,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "what_if_results": self.what_if_results,
            "provisioned_resources": self.provisioned_resources,
            "error": self.error,
            "initiated_by": self.initiated_by,
            "template_id": self.template_id,
            "template_name": self.template_name,
        }


class DeploymentManager:
    """Tracks active and completed deployments with SSE broadcasting."""

    def __init__(self):
        self.deployments: dict[str, DeploymentRecord] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def broadcast(self, deployment_id: str, event: dict):
        """Send a progress event to all subscribers of a deployment."""
        record = self.deployments.get(deployment_id)
        if record:
            record.events.append(event)
            record.phase = event.get("phase", record.phase)
            record.progress = event.get("progress", record.progress)
            record.detail = event.get("detail", record.detail)

        for q in self._subscribers.get(deployment_id, []):
            try:
                q.put_nowait(event)
            except Exception:
                pass

    def subscribe(self, deployment_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        record = self.deployments.get(deployment_id)
        if record:
            for event in record.events:
                q.put_nowait(event)
        self._subscribers.setdefault(deployment_id, []).append(q)
        return q

    def unsubscribe(self, deployment_id: str, q: asyncio.Queue):
        subs = self._subscribers.get(deployment_id, [])
        if q in subs:
            subs.remove(q)

    def finish(self, deployment_id: str):
        """Send sentinel to all subscribers."""
        for q in self._subscribers.get(deployment_id, []):
            try:
                q.put_nowait(None)
            except Exception:
                pass
        self._subscribers.pop(deployment_id, None)

    def list_deployments(self, limit: int = 20) -> list[dict]:
        records = sorted(
            self.deployments.values(),
            key=lambda r: r.started_at,
            reverse=True,
        )[:limit]
        return [r.to_dict() for r in records]


# Module-level singleton
deploy_manager = DeploymentManager()


# ══════════════════════════════════════════════════════════════
# AZURE SDK HELPERS
# ══════════════════════════════════════════════════════════════

def _get_credential():
    """Get DefaultAzureCredential (same as the rest of InfraForge)."""
    from azure.identity import DefaultAzureCredential
    return DefaultAzureCredential(
        exclude_workload_identity_credential=True,
        exclude_managed_identity_credential=True,
    )


def _get_subscription_id() -> str:
    """Resolve the Azure subscription ID from env or CLI."""
    sub_id = os.getenv("AZURE_SUBSCRIPTION_ID", "")
    if sub_id:
        return sub_id

    try:
        import subprocess
        result = subprocess.run(
            ["az", "account", "show", "--query", "id", "-o", "tsv"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    raise ValueError(
        "No Azure subscription ID available. Set AZURE_SUBSCRIPTION_ID "
        "or log in with `az login`."
    )


def _get_resource_client():
    """Create a ResourceManagementClient."""
    from azure.mgmt.resource import ResourceManagementClient
    return ResourceManagementClient(_get_credential(), _get_subscription_id())


# ══════════════════════════════════════════════════════════════
# RESOURCE GROUP HELPERS (handle deprovisioning races)
# ══════════════════════════════════════════════════════════════

import time as _time

def _ensure_resource_group_sync(
    client, resource_group: str, region: str,
    tags: dict | None = None,
    max_wait: int = 120,
    poll_interval: int = 10,
):
    """Create-or-update a resource group, waiting if it's being deleted.

    Azure returns ResourceGroupBeingDeleted / 409 when you try to
    create_or_update an RG that's in deprovisioning state.  This helper
    retries with back-off until the deletion finishes or max_wait expires.
    """
    from azure.core.exceptions import ResourceExistsError, HttpResponseError

    rg_params = {"location": region}
    if tags:
        rg_params["tags"] = tags

    deadline = _time.monotonic() + max_wait

    while True:
        try:
            return client.resource_groups.create_or_update(resource_group, rg_params)
        except (ResourceExistsError, HttpResponseError) as exc:
            msg = str(exc).lower()
            if "beingdeleted" in msg or "deprovisioning" in msg:
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        f"Resource group '{resource_group}' is still being deleted "
                        f"after waiting {max_wait}s. Try again later."
                    ) from exc
                logger.info(
                    f"RG '{resource_group}' is deprovisioning — waiting "
                    f"{poll_interval}s (up to {int(remaining)}s left)…"
                )
                _time.sleep(poll_interval)
                continue
            raise  # not a deprovisioning error — let it bubble up


async def _ensure_resource_group(
    client, loop, resource_group: str, region: str,
    tags: dict | None = None,
    max_wait: int = 120,
    poll_interval: int = 10,
):
    """Async wrapper for _ensure_resource_group_sync."""
    return await loop.run_in_executor(
        None,
        lambda: _ensure_resource_group_sync(
            client, resource_group, region,
            tags=tags, max_wait=max_wait, poll_interval=poll_interval,
        ),
    )


# ══════════════════════════════════════════════════════════════
# WHAT-IF VALIDATION (like `terraform plan`)
# ══════════════════════════════════════════════════════════════

async def run_what_if(
    resource_group: str,
    template: dict,
    parameters: dict,
    region: str = "eastus2",
) -> dict:
    """Run ARM What-If to preview what changes a deployment would make.

    Returns a summary dict with:
      - change_type counts (Create, Modify, Delete, NoChange, etc.)
      - per-resource change details
      - any errors or warnings
    """
    from azure.mgmt.resource.resources.models import (
        DeploymentWhatIf,
        DeploymentWhatIfProperties,
        DeploymentMode,
    )

    client = _get_resource_client()
    loop = asyncio.get_event_loop()

    # Ensure resource group exists (with retry for ResourceGroupBeingDeleted)
    await _ensure_resource_group(client, loop, resource_group, region,
                                 tags={"managedBy": "InfraForge"})

    # Build What-If request
    what_if_params = DeploymentWhatIf(
        properties=DeploymentWhatIfProperties(
            mode=DeploymentMode.INCREMENTAL,
            template=template,
            parameters=_wrap_parameters(parameters),
        ),
    )

    # What-If is a long-running operation
    poller = await loop.run_in_executor(
        None,
        lambda: client.deployments.begin_what_if(
            resource_group,
            f"whatif-{uuid.uuid4().hex[:8]}",
            what_if_params,
        ),
    )

    result = await loop.run_in_executor(None, poller.result)

    # Parse results
    changes = []
    change_counts = {}

    for change in (result.changes or []):
        change_type = str(change.change_type).split(".")[-1] if change.change_type else "Unknown"
        change_counts[change_type] = change_counts.get(change_type, 0) + 1

        resource_id = change.resource_id or ""
        # Extract resource type and name from the ID
        parts = resource_id.split("/")
        resource_type = ""
        resource_name = ""
        if len(parts) >= 2:
            resource_name = parts[-1]
            resource_type = "/".join(parts[-3:-1]) if len(parts) >= 3 else parts[-2]

        change_detail = {
            "change_type": change_type,
            "resource_id": resource_id,
            "resource_type": resource_type,
            "resource_name": resource_name,
        }

        # Include property changes for Modify
        if change_type == "Modify" and change.delta:
            property_changes = []
            for prop in (change.delta or []):
                property_changes.append({
                    "path": prop.path or "",
                    "before": str(prop.before) if prop.before else None,
                    "after": str(prop.after) if prop.after else None,
                })
            change_detail["property_changes"] = property_changes[:10]  # limit

        changes.append(change_detail)

    return {
        "status": "success",
        "change_counts": change_counts,
        "total_changes": len(changes),
        "changes": changes,
        "has_destructive_changes": change_counts.get("Delete", 0) > 0,
        "errors": [],
    }


# ══════════════════════════════════════════════════════════════
# DEPLOYMENT EXECUTION
# ══════════════════════════════════════════════════════════════

async def execute_deployment(
    resource_group: str,
    template: dict,
    parameters: dict,
    region: str = "eastus2",
    deployment_name: Optional[str] = None,
    initiated_by: str = "agent",
    on_progress: ProgressCallback = None,
    template_id: str = "",
    template_name: str = "",
) -> dict:
    """Deploy an ARM template to Azure.

    This is the core engine.  It:
      1. Creates/ensures the resource group exists
      2. Validates the template
      3. Starts the deployment
      4. Polls for completion with progress updates
      5. Returns a summary with provisioned resource details

    Args:
        resource_group: Target resource group name
        template: ARM JSON template (dict)
        parameters: Template parameters (dict of name → value)
        region: Azure region for the resource group
        deployment_name: Optional custom name (auto-generated if omitted)
        initiated_by: Email or identity of the requestor
        on_progress: Async callback for progress events

    Returns:
        Summary dict with deployment results
    """
    from azure.mgmt.resource.resources.models import (
        DeploymentProperties,
        DeploymentMode,
        Deployment,
    )
    from azure.core.exceptions import HttpResponseError

    if not deployment_name:
        deployment_name = f"infraforge-{uuid.uuid4().hex[:8]}"

    deployment_id = f"deploy-{uuid.uuid4().hex[:12]}"
    template_hash = str(hash(json.dumps(template, sort_keys=True)))[:12]

    # Create tracking record
    record = DeploymentRecord(
        deployment_id=deployment_id,
        resource_group=resource_group,
        deployment_name=deployment_name,
        region=region,
        template_hash=template_hash,
        initiated_by=initiated_by,
    )
    record.template_id = template_id
    record.template_name = template_name
    deploy_manager.deployments[deployment_id] = record

    async def _emit(data: dict):
        data["deployment_id"] = deployment_id
        await deploy_manager.broadcast(deployment_id, data)
        if on_progress:
            await on_progress(data)

    client = _get_resource_client()
    loop = asyncio.get_event_loop()
    record.subscription_id = _get_subscription_id()

    try:
        # ── Phase 1: Resource Group ───────────────────────
        record.status = "deploying"
        await _emit({
            "phase": "resource_group",
            "detail": f"Ensuring resource group '{resource_group}' exists in {region}…",
            "progress": 0.05,
        })

        await loop.run_in_executor(None, lambda: _ensure_resource_group_sync(
            client, resource_group, region,
            tags={
                "managedBy": "InfraForge",
                "deployedBy": initiated_by,
                "lastDeployment": deployment_name,
            },
        ))

        # ── Phase 2: Template Validation ──────────────────
        await _emit({
            "phase": "validating",
            "detail": "Validating ARM template against Azure…",
            "progress": 0.10,
        })
        record.status = "validating"

        validation_result = await loop.run_in_executor(
            None,
            lambda: client.deployments.begin_validate(
                resource_group,
                deployment_name,
                Deployment(
                    properties=DeploymentProperties(
                        mode=DeploymentMode.INCREMENTAL,
                        template=template,
                        parameters=_wrap_parameters(parameters),
                    ),
                ),
            ).result(),
        )

        if validation_result.error:
            error_msg = _format_arm_error(validation_result.error)
            record.status = "failed"
            record.error = error_msg
            record.completed_at = datetime.now(timezone.utc).isoformat()
            await _emit({
                "phase": "error",
                "detail": f"Validation failed: {error_msg}",
                "progress": 0,
            })
            deploy_manager.finish(deployment_id)
            await _persist_deployment(record)
            return record.to_dict()

        await _emit({
            "phase": "validated",
            "detail": "Template validation passed ✓",
            "progress": 0.15,
        })

        # ── Phase 3: Start Deployment ─────────────────────
        record.status = "deploying"
        await _emit({
            "phase": "deploying",
            "detail": f"Starting deployment '{deployment_name}'…",
            "progress": 0.20,
        })

        poller = await loop.run_in_executor(
            None,
            lambda: client.deployments.begin_create_or_update(
                resource_group,
                deployment_name,
                Deployment(
                    properties=DeploymentProperties(
                        mode=DeploymentMode.INCREMENTAL,
                        template=template,
                        parameters=_wrap_parameters(parameters),
                    ),
                ),
            ),
        )

        # ── Phase 4: Poll for completion ──────────────────
        poll_interval = 5  # seconds
        max_polls = 360    # 30 minutes max
        polls = 0

        while not poller.done() and polls < max_polls:
            polls += 1
            await asyncio.sleep(poll_interval)

            # Get deployment operations for granular progress
            try:
                operations = await loop.run_in_executor(
                    None,
                    lambda: list(client.deployment_operations.list(
                        resource_group, deployment_name
                    )),
                )

                total_ops = len(operations)
                succeeded_ops = sum(
                    1 for op in operations
                    if op.properties and op.properties.provisioning_state == "Succeeded"
                )
                running_ops = sum(
                    1 for op in operations
                    if op.properties and op.properties.provisioning_state == "Running"
                )

                # Calculate progress (20% start → 90% complete)
                if total_ops > 0:
                    pct = 0.20 + 0.70 * (succeeded_ops / total_ops)
                else:
                    pct = 0.20 + 0.05 * min(polls, 14)

                # Build a human-readable summary of what's happening
                current_resources = []
                for op in operations:
                    if op.properties and op.properties.target_resource:
                        res = op.properties.target_resource
                        state = op.properties.provisioning_state or "Pending"
                        current_resources.append({
                            "type": res.resource_type or "",
                            "name": res.resource_name or "",
                            "state": state,
                        })

                detail = f"Provisioning: {succeeded_ops}/{total_ops} resources complete"
                if running_ops > 0:
                    running_names = [
                        r["name"] for r in current_resources if r["state"] == "Running"
                    ][:3]
                    if running_names:
                        detail += f" (creating: {', '.join(running_names)})"

                await _emit({
                    "phase": "provisioning",
                    "detail": detail,
                    "progress": round(min(pct, 0.90), 2),
                    "resources": current_resources,
                    "succeeded": succeeded_ops,
                    "total": total_ops,
                    "running": running_ops,
                })

            except Exception as e:
                logger.debug(f"Failed to list deployment operations: {e}")
                await _emit({
                    "phase": "provisioning",
                    "detail": "Deployment in progress…",
                    "progress": min(0.20 + 0.03 * polls, 0.85),
                })

        # ── Phase 5: Get final result ─────────────────────
        try:
            result = await loop.run_in_executor(None, poller.result)
        except HttpResponseError as e:
            error_msg = _format_http_error(e)
            # Fetch per-resource operation errors for detailed diagnostics
            op_errors = await _get_deployment_operation_errors(
                client, loop, resource_group, deployment_name
            )
            if op_errors:
                error_msg = f"{error_msg} | Operation errors: {op_errors}"
            record.status = "failed"
            record.error = error_msg
            record.completed_at = datetime.now(timezone.utc).isoformat()
            await _emit({
                "phase": "error",
                "detail": f"Deployment failed: {error_msg}",
                "progress": 0,
            })
            deploy_manager.finish(deployment_id)
            await _persist_deployment(record)
            return record.to_dict()

        # Check if deployment actually succeeded even if poller didn't throw
        prov_state = ""
        if result.properties:
            prov_state = result.properties.provisioning_state or ""
        if prov_state.lower() not in ("succeeded", ""):
            op_errors = await _get_deployment_operation_errors(
                client, loop, resource_group, deployment_name
            )
            error_msg = f"Deployment finished with state '{prov_state}'"
            if op_errors:
                error_msg = f"{error_msg} | Operation errors: {op_errors}"
            record.status = "failed"
            record.error = error_msg
            record.completed_at = datetime.now(timezone.utc).isoformat()
            await _emit({
                "phase": "error",
                "detail": f"Deployment failed: {error_msg}",
                "progress": 0,
            })
            deploy_manager.finish(deployment_id)
            await _persist_deployment(record)
            return record.to_dict()

        # Collect provisioned resources from the deployment
        provisioned = []
        try:
            operations = await loop.run_in_executor(
                None,
                lambda: list(client.deployment_operations.list(
                    resource_group, deployment_name
                )),
            )
            for op in operations:
                if (
                    op.properties
                    and op.properties.target_resource
                    and op.properties.provisioning_state == "Succeeded"
                ):
                    res = op.properties.target_resource
                    provisioned.append({
                        "type": res.resource_type or "",
                        "name": res.resource_name or "",
                        "id": res.id or "",
                    })
        except Exception as e:
            logger.warning(f"Failed to enumerate provisioned resources: {e}")

        # Collect outputs
        outputs = {}
        if result.properties and result.properties.outputs:
            for key, val in result.properties.outputs.items():
                outputs[key] = val.get("value") if isinstance(val, dict) else val

        record.status = "succeeded"
        record.provisioned_resources = provisioned
        record.completed_at = datetime.now(timezone.utc).isoformat()

        await _emit({
            "phase": "done",
            "detail": f"Deployment complete! {len(provisioned)} resources provisioned.",
            "progress": 1.0,
            "provisioned_resources": provisioned,
            "outputs": outputs,
        })

        deploy_manager.finish(deployment_id)
        await _persist_deployment(record)

        summary = record.to_dict()
        summary["outputs"] = outputs
        return summary

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Deployment failed: {error_msg}")
        record.status = "failed"
        record.error = error_msg
        record.completed_at = datetime.now(timezone.utc).isoformat()
        await _emit({
            "phase": "error",
            "detail": f"Deployment failed: {error_msg}",
            "progress": 0,
        })
        deploy_manager.finish(deployment_id)
        await _persist_deployment(record)
        return record.to_dict()


# ══════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════

def _wrap_parameters(params: dict) -> dict:
    """Wrap flat key=value parameters into ARM's {key: {value: v}} format."""
    wrapped = {}
    for key, val in params.items():
        if isinstance(val, dict) and "value" in val:
            wrapped[key] = val  # already wrapped
        else:
            wrapped[key] = {"value": val}
    return wrapped


def _format_arm_error(error) -> str:
    """Extract a readable message from an ARM error object."""
    if hasattr(error, "message") and error.message:
        msg = error.message
        if hasattr(error, "details") and error.details:
            detail_msgs = [
                d.message for d in error.details
                if hasattr(d, "message") and d.message
            ][:3]
            if detail_msgs:
                msg += " | " + " | ".join(detail_msgs)
        return msg
    return str(error)


def _format_http_error(error) -> str:
    """Extract a readable message from an HttpResponseError."""
    msg = str(error.message) if hasattr(error, "message") else str(error)
    if hasattr(error, "error") and error.error:
        if hasattr(error.error, "message"):
            msg = error.error.message
    return msg


async def _get_deployment_operation_errors(
    client, loop, resource_group: str, deployment_name: str
) -> str:
    """Fetch deployment operations and extract per-resource error details.

    This is the key to getting *actual* error messages instead of the
    generic 'At least one resource deployment operation failed' message
    that ARM returns.
    """
    try:
        operations = await loop.run_in_executor(
            None,
            lambda: list(client.deployment_operations.list(
                resource_group, deployment_name
            )),
        )

        error_details = []
        for op in operations:
            props = op.properties
            if not props:
                continue
            # Only interested in failed operations
            if props.provisioning_state not in ("Failed",):
                continue

            res_type = ""
            res_name = ""
            if props.target_resource:
                res_type = props.target_resource.resource_type or ""
                res_name = props.target_resource.resource_name or ""

            # Extract the actual error from status_message
            error_msg = ""
            status_msg = props.status_message
            if status_msg:
                if isinstance(status_msg, dict):
                    err = status_msg.get("error", status_msg)
                    error_msg = err.get("message", "")
                    code = err.get("code", "")
                    if code:
                        error_msg = f"[{code}] {error_msg}"
                    # Check for nested details
                    details = err.get("details", [])
                    if details and isinstance(details, list):
                        for d in details[:3]:
                            if isinstance(d, dict):
                                d_code = d.get("code", "")
                                d_msg = d.get("message", "")
                                if d_msg:
                                    error_msg += f" -> [{d_code}] {d_msg}"
                elif hasattr(status_msg, "error"):
                    e = status_msg.error
                    error_msg = getattr(e, "message", str(status_msg))
                    code = getattr(e, "code", "")
                    if code:
                        error_msg = f"[{code}] {error_msg}"
                else:
                    error_msg = str(status_msg)

            if res_type or error_msg:
                error_details.append(
                    f"{res_type}/{res_name}: {error_msg}" if error_msg
                    else f"{res_type}/{res_name}: Failed (no details)"
                )

        return "; ".join(error_details) if error_details else ""

    except Exception as e:
        logger.debug(f"Failed to fetch deployment operation errors: {e}")
        return ""


async def _persist_deployment(record: DeploymentRecord):
    """Save a deployment record to the database."""
    try:
        from src.database import save_deployment
        await save_deployment(record.to_dict())
    except Exception as e:
        logger.warning(f"Failed to persist deployment record: {e}")


# ══════════════════════════════════════════════════════════════
# COPILOT SDK TOOLS
# ══════════════════════════════════════════════════════════════


class ValidateDeploymentParams(BaseModel):
    arm_template: str = Field(
        description=(
            "The ARM JSON template as a string. Must be valid ARM template JSON "
            "with $schema, contentVersion, resources array, etc."
        )
    )
    parameters: str = Field(
        default="{}",
        description=(
            "Template parameters as a JSON string. "
            'Example: \'{"appName": "myapp", "environment": "dev"}\''
        ),
    )
    resource_group: str = Field(
        description=(
            "The Azure resource group name. Will be created if it doesn't exist. "
            "Example: 'rg-myapp-dev-eastus2'"
        )
    )
    region: str = Field(
        default="eastus2",
        description="Azure region for the resource group.",
    )


@define_tool(description=(
    "Validate an ARM JSON template against Azure using What-If analysis (like "
    "'terraform plan'). This shows exactly what resources would be created, "
    "modified, or deleted — WITHOUT actually deploying anything. Use this "
    "before deploy_infrastructure to let the user review the plan. "
    "Returns a change summary with per-resource details."
))
async def validate_deployment(params: ValidateDeploymentParams) -> str:
    """Run ARM What-If to preview deployment changes."""
    try:
        template = json.loads(params.arm_template)
    except json.JSONDecodeError as e:
        return f"❌ Invalid ARM template JSON: {e}"

    try:
        parameters = json.loads(params.parameters)
    except json.JSONDecodeError as e:
        return f"❌ Invalid parameters JSON: {e}"

    try:
        result = await run_what_if(
            resource_group=params.resource_group,
            template=template,
            parameters=parameters,
            region=params.region,
        )
    except Exception as e:
        return f"❌ What-If validation failed: {e}"

    # Format results for the agent
    lines = ["## 📋 Deployment Preview (What-If)", ""]

    counts = result["change_counts"]
    if counts:
        lines.append("### Change Summary")
        for change_type, count in sorted(counts.items()):
            emoji = {
                "Create": "🆕",
                "Modify": "✏️",
                "Delete": "🗑️",
                "NoChange": "✓",
                "Ignore": "⏭️",
            }.get(change_type, "•")
            lines.append(f"- {emoji} **{change_type}**: {count}")
        lines.append("")

    if result["has_destructive_changes"]:
        lines.append("⚠️ **WARNING: This deployment will DELETE resources!**")
        lines.append("")

    # Per-resource details
    if result["changes"]:
        lines.append("### Resources")
        for change in result["changes"]:
            emoji = {
                "Create": "🆕",
                "Modify": "✏️",
                "Delete": "🗑️",
                "NoChange": "✓",
            }.get(change["change_type"], "•")
            lines.append(
                f"- {emoji} `{change['resource_type']}` / "
                f"**{change['resource_name']}** → {change['change_type']}"
            )
        lines.append("")

    lines.append(f"**Resource group:** `{params.resource_group}` ({params.region})")
    lines.append(f"**Total changes:** {result['total_changes']}")
    lines.append("")
    lines.append("Use `deploy_infrastructure` to execute this deployment.")

    return "\n".join(lines)


class DeployInfrastructureParams(BaseModel):
    arm_template: str = Field(
        description=(
            "The ARM JSON template as a string. Must be valid ARM template JSON "
            "with $schema, contentVersion, resources array, etc."
        )
    )
    parameters: str = Field(
        default="{}",
        description=(
            "Template parameters as a JSON string. "
            'Example: \'{"appName": "myapp", "environment": "dev"}\''
        ),
    )
    resource_group: str = Field(
        description=(
            "The Azure resource group name. Will be created if it doesn't exist. "
            "Follow naming convention: 'rg-{project}-{environment}-{region}'. "
            "Example: 'rg-myapp-dev-eastus2'"
        )
    )
    region: str = Field(
        default="eastus2",
        description="Azure region for the resource group and resources.",
    )
    deployment_name: str = Field(
        default="",
        description=(
            "Optional custom deployment name. Auto-generated if not provided. "
            "Shows up in the Azure Portal under resource group → Deployments."
        ),
    )


@define_tool(description=(
    "Deploy an ARM JSON template directly to Azure. This creates real Azure "
    "resources. The deployment engine: (1) creates the resource group if needed, "
    "(2) validates the template, (3) starts the deployment, (4) monitors progress "
    "with per-resource status, and (5) returns the provisioned resource details "
    "and any template outputs. Use validate_deployment first to preview changes. "
    "Deployments run in incremental mode (only adds/updates, never deletes "
    "existing resources unless explicitly removed from the template)."
))
async def deploy_infrastructure(params: DeployInfrastructureParams) -> str:
    """Deploy ARM template to Azure."""
    try:
        template = json.loads(params.arm_template)
    except json.JSONDecodeError as e:
        return f"❌ Invalid ARM template JSON: {e}"

    try:
        parameters = json.loads(params.parameters)
    except json.JSONDecodeError as e:
        return f"❌ Invalid parameters JSON: {e}"

    # Log progress to stdout for the agent to follow
    async def _agent_progress(event: dict):
        phase = event.get("phase", "")
        detail = event.get("detail", "")
        logger.info(f"[Deploy] {phase}: {detail}")

    try:
        result = await execute_deployment(
            resource_group=params.resource_group,
            template=template,
            parameters=parameters,
            region=params.region,
            deployment_name=params.deployment_name or None,
            on_progress=_agent_progress,
        )
    except Exception as e:
        return f"❌ Deployment failed: {e}"

    # Format results for the agent
    lines = []

    if result["status"] == "succeeded":
        lines.append("## ✅ Deployment Succeeded")
        lines.append("")
        lines.append(f"- **Deployment:** `{result['deployment_name']}`")
        lines.append(f"- **Resource Group:** `{result['resource_group']}` ({result['region']})")
        lines.append(f"- **Started:** {result['started_at']}")
        lines.append(f"- **Completed:** {result['completed_at']}")
        lines.append("")

        if result.get("provisioned_resources"):
            lines.append("### Provisioned Resources")
            for res in result["provisioned_resources"]:
                lines.append(f"- ✅ `{res['type']}` / **{res['name']}**")
            lines.append("")

        if result.get("outputs"):
            lines.append("### Template Outputs")
            for key, val in result["outputs"].items():
                lines.append(f"- **{key}:** `{val}`")
            lines.append("")

        lines.append(
            f"🔗 [View in Azure Portal]"
            f"(https://portal.azure.com/#@/resource/subscriptions/"
            f"{os.getenv('AZURE_SUBSCRIPTION_ID', '')}/resourceGroups/"
            f"{result['resource_group']})"
        )

    elif result["status"] == "failed":
        lines.append("## ❌ Deployment Failed")
        lines.append("")
        lines.append(f"- **Deployment:** `{result['deployment_name']}`")
        lines.append(f"- **Error:** {result.get('error', 'Unknown error')}")
        lines.append("")
        lines.append("Check the error above and fix the template, then retry.")

    else:
        lines.append(f"## ⚠️ Deployment Status: {result['status']}")
        lines.append(f"Detail: {result.get('detail', '')}")

    return "\n".join(lines)


class GetDeploymentStatusParams(BaseModel):
    deployment_id: str = Field(
        default="",
        description="The deployment ID to check. Leave empty to list recent deployments.",
    )


@define_tool(description=(
    "Check the status of a deployment or list recent deployments. "
    "Use after deploy_infrastructure to check progress, or to list all "
    "deployments tracked by InfraForge."
))
async def get_deployment_status(params: GetDeploymentStatusParams) -> str:
    """Get deployment status or list recent deployments."""
    if params.deployment_id:
        record = deploy_manager.deployments.get(params.deployment_id)
        if not record:
            return f"❌ Deployment `{params.deployment_id}` not found."
        result = record.to_dict()
        lines = [
            f"## Deployment: `{result['deployment_name']}`",
            "",
            f"- **Status:** {result['status']}",
            f"- **Resource Group:** `{result['resource_group']}` ({result['region']})",
            f"- **Phase:** {result['phase']}",
            f"- **Progress:** {round(result['progress'] * 100)}%",
            f"- **Detail:** {result['detail']}",
            f"- **Started:** {result['started_at']}",
        ]
        if result["completed_at"]:
            lines.append(f"- **Completed:** {result['completed_at']}")
        if result["error"]:
            lines.append(f"- **Error:** {result['error']}")
        if result["provisioned_resources"]:
            lines.append("")
            lines.append("### Resources")
            for res in result["provisioned_resources"]:
                lines.append(f"- ✅ `{res['type']}` / **{res['name']}**")
        return "\n".join(lines)

    else:
        deployments = deploy_manager.list_deployments()
        if not deployments:
            return "No deployments tracked yet. Use `deploy_infrastructure` to deploy."

        lines = ["## Recent Deployments", ""]
        for d in deployments:
            status_emoji = {
                "succeeded": "✅",
                "failed": "❌",
                "deploying": "🔄",
                "validating": "🔍",
                "pending": "⏳",
            }.get(d["status"], "•")
            lines.append(
                f"- {status_emoji} `{d['deployment_name']}` → {d['resource_group']} "
                f"({d['status']}) — {d['started_at']}"
            )
        return "\n".join(lines)
