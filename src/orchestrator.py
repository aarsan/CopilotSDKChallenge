"""
InfraForge — Template Orchestrator Engine

Runtime engine that executes orchestration processes defined in the DB.
Powers:
  - Auto-onboarding of missing services
  - Dependency resolution during composition
  - Full lifecycle promotion after deep heal
  - Self-healing deploy loops

The orchestrator reads process definitions from the DB so the LLM
knows the steps. The EXECUTION of each step is handled here in Python —
the DB definitions are the "playbook" that both humans and LLMs can read.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

logger = logging.getLogger("infraforge.orchestrator")


# ══════════════════════════════════════════════════════════════
# SERVICE AUTO-ONBOARDING
# ══════════════════════════════════════════════════════════════

async def auto_onboard_service(
    resource_type: str,
    *,
    copilot_client=None,
    progress_callback=None,
) -> dict:
    """Auto-onboard a missing Azure service into the approved catalog.

    Follows the service_onboarding process:
    1. Create service entry (under_review)
    2. Generate ARM template (builtin skeleton or LLM)
    3. Create service version (draft)
    4. Approve + set active version (skip ARM validation for auto-onboard,
       the parent composition will validate the composed result)

    Returns: {"status": "onboarded"|"failed", "service_id": ..., "version": ...}
    """
    from src.database import (
        get_service, upsert_service, create_service_version,
        set_active_service_version, get_process,
    )
    from src.tools.arm_generator import (
        generate_arm_template, has_builtin_skeleton,
        generate_arm_template_with_copilot,
    )
    from src.template_engine import RESOURCE_DEPENDENCIES

    async def _emit(msg: str, phase: str = "onboarding"):
        logger.info(f"[auto-onboard {resource_type}] {msg}")
        if progress_callback:
            await progress_callback({
                "phase": phase,
                "service_id": resource_type,
                "detail": msg,
            })

    # Load the process definition for context
    process = await get_process("service_onboarding")
    if process:
        step_names = [s["name"] for s in process.get("steps", [])]
        await _emit(f"Following process: {' → '.join(step_names)}")

    # ── Step 1: Check if already exists ───────────────────────
    existing = await get_service(resource_type)
    if existing and existing.get("status") == "approved":
        await _emit(f"Already approved in catalog")
        return {"status": "already_approved", "service_id": resource_type}

    # ── Step 2: Create service entry ──────────────────────────
    short_name = resource_type.split("/")[-1]
    # Derive human-readable name
    name_parts = []
    for part in short_name:
        if part.isupper() and name_parts and not name_parts[-1].isupper():
            name_parts.append(" ")
        name_parts.append(part)
    display_name = "".join(name_parts)

    # Determine category from resource type
    category = _infer_category(resource_type)

    # Determine risk tier
    deps = RESOURCE_DEPENDENCIES.get(resource_type, [])
    risk_tier = "low" if not deps else ("medium" if len(deps) <= 3 else "high")

    svc_data = {
        "id": resource_type,
        "name": display_name,
        "category": category,
        "status": "approved",  # Auto-approve for auto-onboard
        "risk_tier": risk_tier,
        "review_notes": "Auto-onboarded by orchestrator during template composition",
        "reviewed_by": "orchestrator",
        "approved_date": datetime.now(timezone.utc).isoformat(),
    }

    if existing:
        # Merge — keep existing fields, update status
        for k, v in svc_data.items():
            if k != "id":
                existing[k] = v
        await upsert_service(existing)
        await _emit(f"Updated existing service → approved")
    else:
        await upsert_service(svc_data)
        await _emit(f"Created service entry: {display_name} ({category})")

    # ── Step 3: Generate ARM template ─────────────────────────
    arm_dict = None

    if has_builtin_skeleton(resource_type):
        arm_dict = generate_arm_template(resource_type)
        await _emit(f"Using builtin ARM skeleton")
    elif copilot_client:
        await _emit(f"Generating ARM template via LLM…")
        try:
            from src.copilot_helpers import get_model_for_task, Task
            model = get_model_for_task(Task.CODE_GENERATION)
            arm_json = await generate_arm_template_with_copilot(
                resource_type,
                display_name,
                copilot_client,
                model=model,
            )
            if arm_json:
                arm_dict = json.loads(arm_json)
                await _emit(f"LLM generated ARM template")
        except Exception as e:
            await _emit(f"LLM generation failed: {e}")

    if not arm_dict:
        await _emit(f"No ARM template available — onboarding incomplete", "error")
        return {"status": "failed", "service_id": resource_type, "reason": "no_arm_template"}

    # ── Step 4: Create service version ────────────────────────
    arm_json_str = json.dumps(arm_dict, indent=2)
    version = await create_service_version(
        resource_type,
        arm_json_str,
        changelog="Auto-onboarded by orchestrator",
        created_by="orchestrator",
    )
    ver_num = version.get("version", 1)
    await _emit(f"Created service version v{ver_num}")

    # ── Step 5: Set as active version ─────────────────────────
    await set_active_service_version(resource_type, ver_num)
    await _emit(f"Set v{ver_num} as active version — onboarding complete ✅")

    return {
        "status": "onboarded",
        "service_id": resource_type,
        "version": ver_num,
        "name": display_name,
        "category": category,
    }


def _infer_category(resource_type: str) -> str:
    """Infer a service category from its Azure resource type."""
    rt = resource_type.lower()
    if "network" in rt or "dns" in rt or "gateway" in rt or "frontdoor" in rt:
        return "networking"
    if "compute" in rt or "virtualmachines" in rt:
        return "compute"
    if "web/" in rt or "app/" in rt or "container" in rt:
        return "compute"
    if "sql" in rt or "database" in rt or "documentdb" in rt or "cache" in rt or "postgresql" in rt:
        return "database"
    if "storage" in rt:
        return "storage"
    if "keyvault" in rt or "identity" in rt or "security" in rt:
        return "security"
    if "insights" in rt or "operationalinsights" in rt or "monitor" in rt:
        return "monitoring"
    if "cognitive" in rt or "machinelearning" in rt or "openai" in rt:
        return "ai"
    return "other"


# ══════════════════════════════════════════════════════════════
# DEPENDENCY RESOLVER
# ══════════════════════════════════════════════════════════════

async def resolve_composition_dependencies(
    selected_service_ids: list[str],
    *,
    copilot_client=None,
    progress_callback=None,
) -> dict:
    """Resolve dependencies for a set of services being composed.

    For each REQUIRED dependency that isn't provided by the selected services:
    1. Check if the service exists in the catalog (approved)
    2. If not → auto-onboard it
    3. Add it to the composition

    Returns:
    {
        "resolved": [{"service_id": ..., "reason": ..., "action": "added"|"onboarded"}],
        "skipped": [{"service_id": ..., "reason": ...}],
        "failed": [{"service_id": ..., "reason": ..., "error": ...}],
        "final_service_ids": [...],  # original + auto-added
    }
    """
    from src.database import get_service, get_active_service_version, get_process
    from src.template_engine import RESOURCE_DEPENDENCIES
    from src.tools.arm_generator import has_builtin_skeleton

    async def _emit(msg: str, phase: str = "dependency_resolution"):
        logger.info(f"[dep-resolver] {msg}")
        if progress_callback:
            await progress_callback({"phase": phase, "detail": msg})

    # Load process definition for context
    process = await get_process("dependency_resolution")
    if process:
        await _emit(f"Following process: dependency_resolution ({len(process.get('steps', []))} steps)")

    provides = set(selected_service_ids)
    resolved = []
    skipped = []
    failed = []
    auto_added = []

    # Find all required deps across all selected services
    for svc_id in selected_service_ids:
        deps = RESOURCE_DEPENDENCIES.get(svc_id, [])
        for dep in deps:
            dep_type = dep["type"]
            if dep_type in provides:
                continue  # Already provided

            if dep.get("created_by_template"):
                provides.add(dep_type)  # Auto-created, no action needed
                continue

            if not dep.get("required"):
                continue  # Optional — skip

            if dep_type in {r["service_id"] for r in resolved}:
                continue  # Already resolved
            if dep_type in {f["service_id"] for f in failed}:
                continue  # Already tried and failed

            await _emit(f"Required dependency: {dep_type} (needed by {svc_id.split('/')[-1]})")

            # Check if service exists and is approved
            svc = await get_service(dep_type)
            if svc and svc.get("status") == "approved":
                # Check it has an ARM template
                active = await get_active_service_version(dep_type)
                if active and active.get("arm_template"):
                    resolved.append({
                        "service_id": dep_type,
                        "reason": dep["reason"],
                        "action": "added",
                        "detail": "Found in catalog (approved)",
                    })
                    provides.add(dep_type)
                    auto_added.append(dep_type)
                    await _emit(f"✅ {dep_type} found in catalog — adding to composition")
                    continue
                elif has_builtin_skeleton(dep_type):
                    resolved.append({
                        "service_id": dep_type,
                        "reason": dep["reason"],
                        "action": "added",
                        "detail": "Approved with builtin skeleton",
                    })
                    provides.add(dep_type)
                    auto_added.append(dep_type)
                    await _emit(f"✅ {dep_type} approved with builtin skeleton — adding")
                    continue

            # Not in catalog or not approved — auto-onboard
            await _emit(f"🔧 {dep_type} not approved — auto-onboarding…")
            result = await auto_onboard_service(
                dep_type,
                copilot_client=copilot_client,
                progress_callback=progress_callback,
            )

            if result["status"] in ("onboarded", "already_approved"):
                resolved.append({
                    "service_id": dep_type,
                    "reason": dep["reason"],
                    "action": "onboarded" if result["status"] == "onboarded" else "added",
                    "detail": f"Auto-onboarded: {result.get('name', dep_type)}",
                })
                provides.add(dep_type)
                auto_added.append(dep_type)
                await _emit(f"✅ {dep_type} onboarded and added to composition")
            else:
                failed.append({
                    "service_id": dep_type,
                    "reason": dep["reason"],
                    "error": result.get("reason", "onboarding failed"),
                })
                await _emit(f"❌ {dep_type} onboarding failed: {result.get('reason')}")

    final_ids = list(selected_service_ids) + auto_added
    await _emit(
        f"Resolution complete: {len(resolved)} resolved, {len(failed)} failed. "
        f"Final services: {len(final_ids)}"
    )

    return {
        "resolved": resolved,
        "skipped": skipped,
        "failed": failed,
        "final_service_ids": final_ids,
    }


# ══════════════════════════════════════════════════════════════
# FULL LIFECYCLE PROMOTION (for deep heal)
# ══════════════════════════════════════════════════════════════

async def promote_healed_service(
    service_id: str,
    version_num: int,
    *,
    progress_callback=None,
) -> dict:
    """Promote a healed service version through the full lifecycle.

    After deep heal fixes and validates a service template:
    1. Update version status → 'validated'
    2. Set as active version
    3. Promote service status → 'approved'

    Returns: {"status": "promoted"|"failed", ...}
    """
    from src.database import (
        get_backend, set_active_service_version,
        promote_service_after_validation,
    )

    async def _emit(msg: str):
        logger.info(f"[promote {service_id} v{version_num}] {msg}")
        if progress_callback:
            await progress_callback({
                "phase": "promote",
                "service_id": service_id,
                "version": version_num,
                "detail": msg,
            })

    try:
        backend = await get_backend()
        now = datetime.now(timezone.utc).isoformat()

        # Step 1: Update version status
        await backend.execute_write(
            """UPDATE service_versions
               SET status = 'validated', validated_at = ?
               WHERE service_id = ? AND version = ?""",
            (now, service_id, version_num),
        )
        await _emit(f"Version v{version_num} status → validated")

        # Step 2: Set as active version
        await set_active_service_version(service_id, version_num)
        await _emit(f"Set v{version_num} as active version")

        # Step 3: Promote service
        await promote_service_after_validation(service_id, {
            "validated_at": now,
            "promoted_by": "deep-healer",
        })
        await _emit(f"Service promoted to approved ✅")

        return {
            "status": "promoted",
            "service_id": service_id,
            "version": version_num,
        }

    except Exception as e:
        await _emit(f"Promotion failed: {e}")
        return {
            "status": "failed",
            "service_id": service_id,
            "version": version_num,
            "error": str(e),
        }


# ══════════════════════════════════════════════════════════════
# PROCESS QUERY API (for LLMs and UI)
# ══════════════════════════════════════════════════════════════

async def get_process_playbook(process_id: str) -> str:
    """Get a human/LLM-readable playbook for a process.

    Returns a formatted text description of the process and its steps
    that can be injected into an LLM prompt for context.
    """
    from src.database import get_process

    proc = await get_process(process_id)
    if not proc:
        return f"Process '{process_id}' not found."

    lines = [
        f"## Process: {proc['name']}",
        f"**Trigger:** {proc['trigger_event']}",
        f"**Description:** {proc['description']}",
        "",
        "### Steps:",
    ]

    for step in proc.get("steps", []):
        lines.append(
            f"  {step['step_order']}. **{step['name']}** (`{step['action']}`)\n"
            f"     {step['description']}\n"
            f"     → Success: {step['on_success']} | Failure: {step['on_failure']}"
        )

    return "\n".join(lines)
