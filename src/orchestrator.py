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


# ══════════════════════════════════════════════════════════════
# TEMPLATE FEEDBACK — CHAT WITH YOUR TEMPLATE
# ══════════════════════════════════════════════════════════════

async def analyze_template_feedback(
    template: dict,
    user_message: str,
    *,
    copilot_client=None,
    progress_callback=None,
) -> dict:
    """Analyze user feedback about a template and determine corrective actions.

    The user says something like "I wanted a VM but only a VNet got deployed".
    This function:
    1. Uses the LLM to understand what the template CURRENTLY provides
       vs what the user EXPECTS
    2. Identifies missing Azure resource types
    3. For each missing resource: checks catalog → auto-onboards if needed
    4. Returns a structured action plan

    Args:
        template: The full template catalog entry (with content, service_ids, etc.)
        user_message: Natural language feedback from the user
        copilot_client: Copilot SDK client for LLM analysis
        progress_callback: Async callback for streaming progress events

    Returns:
        {
            "analysis": str,               # LLM's analysis of the gap
            "missing_services": [...],      # Resource types identified as missing
            "actions_taken": [...],         # What the orchestrator did
            "should_recompose": bool,       # Whether to trigger recompose
            "new_service_ids": [...],       # Updated service list
        }
    """
    import asyncio
    from src.template_engine import RESOURCE_DEPENDENCIES

    async def _emit(msg: str, phase: str = "feedback"):
        logger.info(f"[feedback] {msg}")
        if progress_callback:
            await progress_callback({"phase": phase, "detail": msg})

    await _emit("Analyzing your feedback…")

    # ── Step 1: Build context for the LLM ─────────────────────
    current_services = template.get("service_ids") or []
    current_resources = template.get("resources") or []
    provides = template.get("provides") or []

    # Build the known resource types list for the LLM
    from src.template_engine import RESOURCE_DEPENDENCIES
    known_types = sorted(RESOURCE_DEPENDENCIES.keys())

    # ── Step 2: Ask the LLM to identify what's missing ────────
    analysis_result = None

    if copilot_client:
        await _emit("Consulting AI to identify missing resources…")

        prompt = (
            "You are an Azure infrastructure expert analyzing a user's template feedback.\n\n"
            f"--- TEMPLATE INFO ---\n"
            f"Name: {template.get('name', 'Unknown')}\n"
            f"Description: {template.get('description', 'None')}\n"
            f"Current services: {json.dumps(current_services)}\n"
            f"Provides (resource types): {json.dumps(provides)}\n"
            f"Current resource types: {json.dumps(current_resources)}\n"
            f"--- END TEMPLATE INFO ---\n\n"
            f"--- USER FEEDBACK ---\n"
            f"{user_message}\n"
            f"--- END FEEDBACK ---\n\n"
            f"--- KNOWN AZURE RESOURCE TYPES ---\n"
            f"{json.dumps(known_types)}\n"
            f"--- END KNOWN TYPES ---\n\n"
            "The user's feedback can be one of TWO categories:\n"
            "A) ADD SERVICES — they want NEW resource types added to the template\n"
            "B) MODIFY EXISTING — they want to change, remove, reduce, reconfigure, or fix \n"
            "   resources that ALREADY exist in the template (e.g. reduce 2 VNets to 1, \n"
            "   change a SKU, remove a subnet, rename a resource, fix a config error)\n\n"
            "Based on the user's feedback, determine:\n"
            "1. Is this category A (add new services) or B (modify existing code)?\n"
            "2. If A: which Azure resource types should be added?\n"
            "3. If B: describe the specific code change needed\n\n"
            "Return ONLY a JSON object with this exact structure:\n"
            "{\n"
            '  "analysis": "One paragraph explaining what the user wants",\n'
            '  "category": "add_services" or "modify_existing",\n'
            '  "missing_resource_types": ["Microsoft.Compute/virtualMachines", ...],\n'
            '  "explanation_per_type": {"Microsoft.Compute/virtualMachines": "..."},\n'
            '  "needs_code_edit": true/false,\n'
            '  "edit_instruction": "Specific instruction for what to change in the ARM template JSON"\n'
            "}\n\n"
            "RULES:\n"
            "- For category A: populate missing_resource_types, set needs_code_edit=false\n"
            "- For category B: set missing_resource_types=[], needs_code_edit=true, and write a clear edit_instruction\n"
            "- Only include resource types from the KNOWN AZURE RESOURCE TYPES list\n"
            "- Do NOT include resource types already in the template's current services\n"
            "- Return ONLY the raw JSON — no markdown fences, no extra text\n"
        )

        try:
            from src.model_router import Task, get_model_for_task
            model = get_model_for_task(Task.PLANNING)

            session = await copilot_client.create_session({
                "model": model,
                "streaming": True,
                "tools": [],
                "system_message": {
                    "content": (
                        "You are an Azure infrastructure analysis agent. "
                        "You identify gaps between what a template provides and "
                        "what a user expects. Return ONLY raw JSON."
                    )
                },
            })

            chunks: list[str] = []
            done_ev = asyncio.Event()

            def on_event(ev):
                try:
                    if ev.type.value == "assistant.message_delta":
                        chunks.append(ev.data.delta_content or "")
                    elif ev.type.value in ("assistant.message", "session.idle"):
                        done_ev.set()
                except Exception:
                    done_ev.set()

            unsub = session.on(on_event)
            try:
                await session.send({"prompt": prompt})
                await asyncio.wait_for(done_ev.wait(), timeout=60)
            finally:
                unsub()

            raw = "".join(chunks).strip()
            # Strip markdown fences if present
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()

            analysis_result = json.loads(raw)
            await _emit(f"AI identified {len(analysis_result.get('missing_resource_types', []))} missing resource types")

        except asyncio.TimeoutError:
            await _emit("LLM analysis timed out — falling back to heuristic", "warning")
            logger.warning("analyze_template_feedback LLM timed out after 60s")
        except json.JSONDecodeError as e:
            await _emit(f"LLM returned invalid JSON — falling back to heuristic: {e}", "warning")
            logger.warning(f"LLM feedback response was: {raw[:500]}")
        except Exception as e:
            await _emit(f"LLM analysis failed: {e}", "warning")
            logger.warning(f"analyze_template_feedback LLM exception: {type(e).__name__}: {e}")

    # ── Step 3: Fallback heuristic if LLM unavailable ─────────
    if not analysis_result:
        await _emit("Using keyword-based heuristic analysis…")
        msg_lower = user_message.lower()
        missing = []

        # ── First: detect modification-style requests ─────────
        # Words that signal the user wants to CHANGE existing resources,
        # not add new ones.
        modify_signals = [
            "reduce", "remove", "delete", "change", "modify", "update",
            "rename", "fix", "replace", "should be", "instead of",
            "too many", "only need", "don't need", "do not need",
            "shouldn't", "should not", "wrong", "incorrect",
            "provisioning 2", "provisioning two", "has 2", "has two",
            "2 vnet", "two vnet", "1 vnet", "one vnet",
            "duplicate", "extra", "unwanted", "unnecessary",
        ]
        is_modification = any(sig in msg_lower for sig in modify_signals)

        if is_modification:
            analysis_result = {
                "analysis": f"Your request appears to modify existing resources in the template.",
                "missing_resource_types": [],
                "explanation_per_type": {},
                "needs_code_edit": True,
                "edit_instruction": user_message,
            }
        else:
            # Simple keyword → resource type mapping
            keyword_map = {
                "vm": "Microsoft.Compute/virtualMachines",
                "virtual machine": "Microsoft.Compute/virtualMachines",
                "sql": "Microsoft.Sql/servers",
                "database": "Microsoft.Sql/servers",
                "key vault": "Microsoft.KeyVault/vaults",
                "keyvault": "Microsoft.KeyVault/vaults",
                "storage": "Microsoft.Storage/storageAccounts",
                "app service": "Microsoft.Web/sites",
                "web app": "Microsoft.Web/sites",
                "aks": "Microsoft.ContainerService/managedClusters",
                "kubernetes": "Microsoft.ContainerService/managedClusters",
                "container app": "Microsoft.App/containerApps",
                "redis": "Microsoft.Cache/redis",
                "cosmos": "Microsoft.DocumentDB/databaseAccounts",
                "cosmosdb": "Microsoft.DocumentDB/databaseAccounts",
                "dns": "Microsoft.Network/dnsZones",
                "front door": "Microsoft.Cdn/profiles",
                "cdn": "Microsoft.Cdn/profiles",
                "vnet": "Microsoft.Network/virtualNetworks",
                "virtual network": "Microsoft.Network/virtualNetworks",
                "nsg": "Microsoft.Network/networkSecurityGroups",
                "load balancer": "Microsoft.Network/loadBalancers",
                "application gateway": "Microsoft.Network/applicationGateways",
                "container registry": "Microsoft.ContainerRegistry/registries",
                "acr": "Microsoft.ContainerRegistry/registries",
                "monitor": "Microsoft.Insights/components",
                "application insights": "Microsoft.Insights/components",
                "log analytics": "Microsoft.OperationalInsights/workspaces",
                "postgresql": "Microsoft.DBforPostgreSQL/flexibleServers",
                "postgres": "Microsoft.DBforPostgreSQL/flexibleServers",
            }

            for keyword, rtype in keyword_map.items():
                if keyword in msg_lower and rtype not in current_services and rtype not in missing:
                    missing.append(rtype)

            analysis_result = {
                "analysis": f"Based on keyword analysis of your feedback, identified {len(missing)} resource types that may be missing from the template.",
                "missing_resource_types": missing,
                "explanation_per_type": {rt: f"Detected '{rt.split('/')[-1]}' keyword in feedback" for rt in missing},
            }

    # ── Step 4: Act on each missing resource type ─────────────
    missing_types = analysis_result.get("missing_resource_types", [])
    explanations = analysis_result.get("explanation_per_type", {})
    actions_taken = []
    new_service_ids = list(current_services)

    if not missing_types:
        needs_edit = analysis_result.get("needs_code_edit", False)
        edit_instruction = analysis_result.get("edit_instruction", "")
        if needs_edit:
            await _emit("This is a modification to existing resources — will edit template code directly.")
        else:
            await _emit("No missing resource types identified. The template may already cover your needs.")
        return {
            "analysis": analysis_result.get("analysis", ""),
            "missing_services": [],
            "actions_taken": [],
            "should_recompose": False,
            "needs_code_edit": needs_edit,
            "edit_instruction": edit_instruction,
            "new_service_ids": current_services,
        }

    for rtype in missing_types:
        if rtype in new_service_ids:
            continue  # Already in template

        await _emit(f"Processing missing resource: {rtype.split('/')[-1]}")

        # Check if service exists in catalog
        from src.database import get_service, get_active_service_version

        svc = await get_service(rtype)
        if svc and svc.get("status") == "approved":
            active = await get_active_service_version(rtype)
            if active and active.get("arm_template"):
                new_service_ids.append(rtype)
                actions_taken.append({
                    "action": "added_from_catalog",
                    "service_id": rtype,
                    "detail": f"Found approved service in catalog",
                    "explanation": explanations.get(rtype, ""),
                })
                await _emit(f"✅ {rtype} found in catalog — will add to composition")
                continue

        # Not in catalog — auto-onboard
        await _emit(f"🔧 {rtype} not in catalog — auto-onboarding…")
        result = await auto_onboard_service(
            rtype,
            copilot_client=copilot_client,
            progress_callback=progress_callback,
        )

        if result["status"] in ("onboarded", "already_approved"):
            new_service_ids.append(rtype)
            actions_taken.append({
                "action": "auto_onboarded",
                "service_id": rtype,
                "detail": f"Auto-onboarded: {result.get('name', rtype)}",
                "explanation": explanations.get(rtype, ""),
            })
            await _emit(f"✅ {rtype} onboarded — will add to composition")
        else:
            actions_taken.append({
                "action": "onboard_failed",
                "service_id": rtype,
                "detail": f"Failed to onboard: {result.get('reason', 'unknown')}",
                "explanation": explanations.get(rtype, ""),
            })
            await _emit(f"❌ {rtype} onboarding failed: {result.get('reason')}")

    should_recompose = len(new_service_ids) > len(current_services)

    await _emit(
        f"Analysis complete: {len(actions_taken)} actions, "
        f"{'will recompose' if should_recompose else 'no recompose needed'}"
    )

    return {
        "analysis": analysis_result.get("analysis", ""),
        "missing_services": missing_types,
        "actions_taken": actions_taken,
        "should_recompose": should_recompose,
        "new_service_ids": new_service_ids,
    }


# ══════════════════════════════════════════════════════════════
# DIRECT TEMPLATE CODE EDITING — MODIFY EXISTING RESOURCES
# ══════════════════════════════════════════════════════════════

async def apply_template_code_edit(
    template: dict,
    edit_instruction: str,
    user_message: str,
    *,
    copilot_client=None,
) -> dict:
    """Apply a direct code edit to an existing ARM template via the LLM.

    Used when the user wants to modify existing resources (reduce, remove,
    reconfigure, rename) rather than add new services.

    Args:
        template: The full template catalog entry (with content)
        edit_instruction: Specific instruction from analyze_template_feedback
        user_message: The original user request (for additional context)
        copilot_client: Copilot SDK client for LLM editing

    Returns:
        {
            "success": bool,
            "content": str,             # Updated ARM JSON string
            "changes_summary": str,     # Human-readable summary of changes
            "error": str | None,
        }
    """
    import asyncio

    current_content = template.get("content", "")
    if not current_content:
        return {
            "success": False,
            "content": "",
            "changes_summary": "",
            "error": "Template has no content to edit",
        }

    # Ensure content is a string
    if not isinstance(current_content, str):
        current_content = json.dumps(current_content, indent=2)

    prompt = (
        "You are an ARM template editor. You will receive an existing ARM JSON template "
        "and an instruction describing what to change. Apply the change precisely.\n\n"
        f"--- USER REQUEST ---\n{user_message}\n--- END USER REQUEST ---\n\n"
        f"--- EDIT INSTRUCTION ---\n{edit_instruction}\n--- END EDIT INSTRUCTION ---\n\n"
        f"--- CURRENT ARM TEMPLATE ---\n{current_content}\n--- END TEMPLATE ---\n\n"
        "Apply the requested change to the ARM template. Return a JSON object with:\n"
        "{\n"
        '  "arm_template": { ... the complete modified ARM JSON ... },\n'
        '  "changes_summary": "Brief description of what was changed"\n'
        "}\n\n"
        "RULES:\n"
        "- Return the COMPLETE ARM template, not just the changed parts\n"
        "- Maintain valid ARM template structure ($schema, contentVersion, parameters, variables, resources, outputs)\n"
        "- Keep all existing parameters, variables, and outputs that are still relevant\n"
        "- Remove parameters/outputs that are no longer needed after the change\n"
        "- Preserve resource tags, dependencies, and naming conventions\n"
        "- Return ONLY the raw JSON — no markdown fences, no extra text\n"
    )

    if not copilot_client:
        return {
            "success": False,
            "content": current_content,
            "changes_summary": "",
            "error": "No AI client available for code editing",
        }

    try:
        from src.model_router import Task, get_model_for_task
        model = get_model_for_task(Task.CODE_GENERATION)

        session = await copilot_client.create_session({
            "model": model,
            "streaming": True,
            "tools": [],
            "system_message": {
                "content": (
                    "You are an ARM template editor. You modify existing Azure "
                    "Resource Manager templates based on user instructions. "
                    "Return ONLY raw JSON — no markdown, no commentary."
                )
            },
        })

        chunks: list[str] = []
        done_ev = asyncio.Event()

        def on_event(ev):
            try:
                if ev.type.value == "assistant.message_delta":
                    chunks.append(ev.data.delta_content or "")
                elif ev.type.value in ("assistant.message", "session.idle"):
                    done_ev.set()
            except Exception:
                done_ev.set()

        unsub = session.on(on_event)
        try:
            await session.send({"prompt": prompt})
            await asyncio.wait_for(done_ev.wait(), timeout=90)
        finally:
            unsub()

        raw = "".join(chunks).strip()
        # Strip markdown fences
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3].strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()

        result = json.loads(raw)
        arm_template = result.get("arm_template", result)
        changes_summary = result.get("changes_summary", "Template modified per user request")

        # If the LLM returned the arm_template directly (without wrapper)
        if "$schema" in arm_template:
            edited_content = json.dumps(arm_template, indent=2)
        else:
            edited_content = json.dumps(arm_template, indent=2)

        return {
            "success": True,
            "content": edited_content,
            "changes_summary": changes_summary,
            "error": None,
        }

    except asyncio.TimeoutError:
        logger.warning("LLM code edit timed out")
        return {
            "success": False,
            "content": current_content,
            "changes_summary": "",
            "error": "AI editing timed out — try simplifying the request",
        }
    except json.JSONDecodeError as e:
        logger.warning(f"LLM returned invalid JSON for code edit: {e}")
        # Try to extract just the ARM template from raw response
        try:
            # Maybe the LLM returned the ARM template directly
            if '"$schema"' in raw:
                start = raw.index("{")
                depth = 0
                end = start
                for i in range(start, len(raw)):
                    if raw[i] == "{":
                        depth += 1
                    elif raw[i] == "}":
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                arm_str = raw[start:end]
                parsed = json.loads(arm_str)
                return {
                    "success": True,
                    "content": json.dumps(parsed, indent=2),
                    "changes_summary": "Template modified per user request",
                    "error": None,
                }
        except Exception:
            pass
        return {
            "success": False,
            "content": current_content,
            "changes_summary": "",
            "error": f"AI returned invalid template JSON: {e}",
        }
    except Exception as e:
        logger.error(f"Code edit failed: {e}")
        return {
            "success": False,
            "content": current_content,
            "changes_summary": "",
            "error": str(e),
        }


# ══════════════════════════════════════════════════════════════
# POLICY PRE-CHECK — INSTANT FEEDBACK ON REVISION REQUESTS
# ══════════════════════════════════════════════════════════════

async def check_revision_policy(
    user_prompt: str,
    template: dict | None = None,
    *,
    copilot_client=None,
) -> dict:
    """Check a user's revision/compose request against org policies BEFORE processing.

    Returns instant feedback: pass/warning/block with reasons.
    This runs BEFORE any composition to catch policy violations early.

    Args:
        user_prompt: The user's natural language request
        template: Existing template (for revisions) or None (for new compose)
        copilot_client: Copilot SDK client for LLM analysis

    Returns:
        {
            "verdict": "pass" | "warning" | "block",
            "issues": [{"severity": "block"|"warning", "rule": str, "message": str}],
            "summary": str,
        }
    """
    import asyncio
    from src.database import get_governance_policies_as_dict

    policies = await get_governance_policies_as_dict()
    if not policies:
        return {"verdict": "pass", "issues": [], "summary": "No governance policies configured."}

    # ── LLM-based policy check ────────────────────────────────
    if copilot_client:
        try:
            from src.model_router import Task, get_model_for_task
            model = get_model_for_task(Task.PLANNING)

            template_context = ""
            if template:
                template_context = (
                    f"\n--- EXISTING TEMPLATE ---\n"
                    f"Name: {template.get('name', 'Unknown')}\n"
                    f"Current services: {json.dumps(template.get('service_ids', []))}\n"
                    f"Provides: {json.dumps(template.get('provides', []))}\n"
                    f"--- END TEMPLATE ---\n"
                )

            prompt = (
                "You are an Azure infrastructure governance agent. Your job is to check whether a user's "
                "infrastructure request complies with organizational policies BEFORE any resources are created.\n\n"
                f"--- ORGANIZATION POLICIES ---\n"
                f"{json.dumps(policies, indent=2)}\n"
                f"--- END POLICIES ---\n"
                f"{template_context}\n"
                f"--- USER REQUEST ---\n"
                f"{user_prompt}\n"
                f"--- END REQUEST ---\n\n"
                "Evaluate the user's request against the organization policies. Check for:\n"
                "1. Requests for public endpoints when policy requires private networking\n"
                "2. Requests for regions not in allowed_regions\n"
                "3. Requests for services or SKUs that may be blocked\n"
                "4. Requests that would skip required tags, monitoring, or encryption\n"
                "5. Requests for hardcoded secrets or passwords\n"
                "6. Any request that conflicts with the organization's security posture\n\n"
                "Return ONLY a JSON object:\n"
                "{\n"
                '  "verdict": "pass" | "warning" | "block",\n'
                '  "issues": [\n'
                '    {"severity": "block" or "warning", "rule": "policy rule name", "message": "human-readable explanation"}\n'
                "  ],\n"
                '  "summary": "One sentence overall assessment"\n'
                "}\n\n"
                "RULES:\n"
                "- verdict is 'block' if ANY issue has severity 'block'\n"
                "- verdict is 'warning' if issues exist but none are blocking\n"
                "- verdict is 'pass' if no issues found\n"
                "- Return ONLY raw JSON — no markdown fences\n"
            )

            session = await copilot_client.create_session({
                "model": model,
                "streaming": True,
                "tools": [],
                "system_message": {
                    "content": (
                        "You are a governance policy checker for Azure infrastructure. "
                        "You evaluate user requests against organizational policies. "
                        "Return ONLY raw JSON."
                    )
                },
            })

            chunks: list[str] = []
            done_ev = asyncio.Event()

            def on_event(ev):
                try:
                    if ev.type.value == "assistant.message_delta":
                        chunks.append(ev.data.delta_content or "")
                    elif ev.type.value in ("assistant.message", "session.idle"):
                        done_ev.set()
                except Exception:
                    done_ev.set()

            unsub = session.on(on_event)
            try:
                await session.send({"prompt": prompt})
                await asyncio.wait_for(done_ev.wait(), timeout=30)
            finally:
                unsub()

            raw = "".join(chunks).strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()

            result = json.loads(raw)
            return {
                "verdict": result.get("verdict", "pass"),
                "issues": result.get("issues", []),
                "summary": result.get("summary", ""),
            }

        except asyncio.TimeoutError:
            logger.warning("Policy check LLM timed out")
        except json.JSONDecodeError:
            logger.warning(f"Policy check LLM returned invalid JSON")
        except Exception as e:
            logger.warning(f"Policy check LLM failed: {e}")

    # ── Heuristic fallback ────────────────────────────────────
    issues = []
    prompt_lower = user_prompt.lower()

    # Check for public endpoint requests
    if any(kw in prompt_lower for kw in ["public ip", "public endpoint", "publicly accessible", "open to internet"]):
        if policies.get("require_private_endpoints") or policies.get("deny_public_ips"):
            issues.append({
                "severity": "block",
                "rule": "require_private_endpoints",
                "message": "Organization policy blocks public endpoints. Use private endpoints or VNet integration instead.",
            })

    # Check for disallowed regions
    allowed_regions = policies.get("allowed_regions", [])
    if allowed_regions:
        for region_kw in ["west us 3", "south africa", "brazil", "asia pacific"]:
            if region_kw in prompt_lower:
                issues.append({
                    "severity": "warning",
                    "rule": "allowed_regions",
                    "message": f"Region may not be in the allowed list: {', '.join(allowed_regions)}",
                })
                break

    # Check for hardcoded secrets
    if any(kw in prompt_lower for kw in ["hardcode", "hard-code", "embed password", "inline secret"]):
        issues.append({
            "severity": "block",
            "rule": "no_hardcoded_secrets",
            "message": "Hardcoded secrets are prohibited. Use Azure Key Vault for credential management.",
        })

    # Check for skipping tags/monitoring
    if any(kw in prompt_lower for kw in ["no tags", "skip tags", "without tags", "no monitoring", "skip monitoring"]):
        issues.append({
            "severity": "warning",
            "rule": "require_tags",
            "message": "Organization policy requires standard tags (environment, owner, costCenter, project) on all resources.",
        })

    verdict = "block" if any(i["severity"] == "block" for i in issues) else \
              "warning" if issues else "pass"

    return {
        "verdict": verdict,
        "issues": issues,
        "summary": f"{len(issues)} policy issue(s) found." if issues else "Request appears to comply with organizational policies.",
    }


# ══════════════════════════════════════════════════════════════
# LLM-DRIVEN SERVICE SELECTION — PROMPT → SERVICES
# ══════════════════════════════════════════════════════════════

async def determine_services_from_prompt(
    user_prompt: str,
    *,
    copilot_client=None,
    progress_callback=None,
) -> dict:
    """Use the LLM to determine which Azure services are needed for a user's request.

    Takes a natural language description like "I need a VM with a SQL database"
    and returns a list of Azure resource types to compose.

    Args:
        user_prompt: Natural language description of desired infrastructure
        copilot_client: Copilot SDK client
        progress_callback: Async callback for progress events

    Returns:
        {
            "services": [{"resource_type": str, "reason": str, "quantity": int}],
            "name_suggestion": str,
            "description_suggestion": str,
            "category_suggestion": str,
        }
    """
    import asyncio
    from src.template_engine import RESOURCE_DEPENDENCIES

    async def _emit(msg: str, phase: str = "service_selection"):
        logger.info(f"[prompt→services] {msg}")
        if progress_callback:
            await progress_callback({"phase": phase, "detail": msg})

    known_types = sorted(RESOURCE_DEPENDENCIES.keys())

    # ── LLM path ──────────────────────────────────────────────
    if copilot_client:
        await _emit("Analyzing your request to determine required services…")

        try:
            from src.model_router import Task, get_model_for_task
            model = get_model_for_task(Task.PLANNING)

            prompt = (
                "You are an Azure infrastructure architect. A user has described what infrastructure they need "
                "in natural language. Your job is to determine which Azure resource types are required.\n\n"
                f"--- AVAILABLE AZURE RESOURCE TYPES ---\n"
                f"{json.dumps(known_types)}\n"
                f"--- END AVAILABLE TYPES ---\n\n"
                f"--- USER REQUEST ---\n"
                f"{user_prompt}\n"
                f"--- END REQUEST ---\n\n"
                "Determine which Azure resource types from the available list are needed to fulfill this request.\n\n"
                "Return ONLY a JSON object:\n"
                "{\n"
                '  "services": [\n'
                '    {"resource_type": "Microsoft.Compute/virtualMachines", "reason": "User wants a VM", "quantity": 1}\n'
                "  ],\n"
                '  "name_suggestion": "short template name (3-5 words)",\n'
                '  "description_suggestion": "one sentence describing what the template deploys",\n'
                '  "category_suggestion": "compute|database|networking|storage|security|monitoring|blueprint"\n'
                "}\n\n"
                "RULES:\n"
                "- Only use resource types from the AVAILABLE list\n"
                "- Include networking foundations (VNet, NSG) if the workload needs them\n"
                "- Default quantity to 1 unless user specifies otherwise\n"
                "- Be conservative — only include what the user actually asked for\n"
                "- If the user mentions a concept like 'web app', map it to the appropriate service (Microsoft.Web/sites)\n"
                "- Return ONLY raw JSON — no markdown fences\n"
            )

            session = await copilot_client.create_session({
                "model": model,
                "streaming": True,
                "tools": [],
                "system_message": {
                    "content": (
                        "You are an Azure infrastructure architect that maps user requests "
                        "to specific Azure resource types. Return ONLY raw JSON."
                    )
                },
            })

            chunks: list[str] = []
            done_ev = asyncio.Event()

            def on_event(ev):
                try:
                    if ev.type.value == "assistant.message_delta":
                        chunks.append(ev.data.delta_content or "")
                    elif ev.type.value in ("assistant.message", "session.idle"):
                        done_ev.set()
                except Exception:
                    done_ev.set()

            unsub = session.on(on_event)
            try:
                await session.send({"prompt": prompt})
                await asyncio.wait_for(done_ev.wait(), timeout=60)
            finally:
                unsub()

            raw = "".join(chunks).strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()

            result = json.loads(raw)
            services = result.get("services", [])
            await _emit(f"AI identified {len(services)} service(s): {', '.join(s['resource_type'].split('/')[-1] for s in services)}")

            return {
                "services": services,
                "name_suggestion": result.get("name_suggestion", ""),
                "description_suggestion": result.get("description_suggestion", ""),
                "category_suggestion": result.get("category_suggestion", "blueprint"),
            }

        except asyncio.TimeoutError:
            await _emit("LLM timed out — falling back to keyword analysis", "warning")
        except json.JSONDecodeError:
            await _emit("LLM returned invalid JSON — falling back to keyword analysis", "warning")
        except Exception as e:
            await _emit(f"LLM failed: {e} — falling back to keyword analysis", "warning")

    # ── Heuristic fallback ────────────────────────────────────
    await _emit("Using keyword-based service detection…")
    prompt_lower = user_prompt.lower()
    services = []
    seen = set()

    keyword_map = {
        "vm": ("Microsoft.Compute/virtualMachines", "Virtual machine requested"),
        "virtual machine": ("Microsoft.Compute/virtualMachines", "Virtual machine requested"),
        "sql": ("Microsoft.Sql/servers", "SQL database requested"),
        "database": ("Microsoft.Sql/servers", "Database requested"),
        "key vault": ("Microsoft.KeyVault/vaults", "Key Vault for secrets management"),
        "keyvault": ("Microsoft.KeyVault/vaults", "Key Vault requested"),
        "storage": ("Microsoft.Storage/storageAccounts", "Storage account requested"),
        "app service": ("Microsoft.Web/sites", "App Service requested"),
        "web app": ("Microsoft.Web/sites", "Web application requested"),
        "aks": ("Microsoft.ContainerService/managedClusters", "AKS cluster requested"),
        "kubernetes": ("Microsoft.ContainerService/managedClusters", "Kubernetes cluster requested"),
        "container app": ("Microsoft.App/containerApps", "Container App requested"),
        "redis": ("Microsoft.Cache/redis", "Redis cache requested"),
        "cosmos": ("Microsoft.DocumentDB/databaseAccounts", "Cosmos DB requested"),
        "cosmosdb": ("Microsoft.DocumentDB/databaseAccounts", "Cosmos DB requested"),
        "dns": ("Microsoft.Network/dnsZones", "DNS zone requested"),
        "front door": ("Microsoft.Cdn/profiles", "Front Door / CDN requested"),
        "cdn": ("Microsoft.Cdn/profiles", "CDN requested"),
        "vnet": ("Microsoft.Network/virtualNetworks", "Virtual network requested"),
        "virtual network": ("Microsoft.Network/virtualNetworks", "Virtual network requested"),
        "nsg": ("Microsoft.Network/networkSecurityGroups", "Network security group requested"),
        "load balancer": ("Microsoft.Network/loadBalancers", "Load balancer requested"),
        "application gateway": ("Microsoft.Network/applicationGateways", "Application gateway requested"),
        "container registry": ("Microsoft.ContainerRegistry/registries", "Container registry requested"),
        "acr": ("Microsoft.ContainerRegistry/registries", "Container registry requested"),
        "monitor": ("Microsoft.Insights/components", "Monitoring requested"),
        "application insights": ("Microsoft.Insights/components", "Application Insights requested"),
        "log analytics": ("Microsoft.OperationalInsights/workspaces", "Log Analytics requested"),
        "postgresql": ("Microsoft.DBforPostgreSQL/flexibleServers", "PostgreSQL requested"),
        "postgres": ("Microsoft.DBforPostgreSQL/flexibleServers", "PostgreSQL requested"),
    }

    for keyword, (rtype, reason) in keyword_map.items():
        if keyword in prompt_lower and rtype not in seen:
            services.append({"resource_type": rtype, "reason": reason, "quantity": 1})
            seen.add(rtype)

    # Auto-suggest name
    words = user_prompt.split()[:5]
    name_suggestion = " ".join(w.capitalize() for w in words) if words else "My Template"

    return {
        "services": services,
        "name_suggestion": name_suggestion,
        "description_suggestion": user_prompt[:200],
        "category_suggestion": "blueprint" if len(services) > 1 else (
            _infer_category(services[0]["resource_type"]) if services else "blueprint"
        ),
    }
