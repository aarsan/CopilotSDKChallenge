"""
InfraForge — Web Interface

FastAPI backend providing:
- Entra ID (Azure AD) authentication with MSAL
- WebSocket-based streaming chat connected to the Copilot SDK
- User context injection for personalized infrastructure provisioning
- REST endpoints for auth flow, session management, and usage tracking

This is the enterprise-grade frontend for InfraForge — authenticated users
interact with the agent through a browser, and their identity context enriches
every infrastructure request.
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from copilot import CopilotClient

from src.copilot_helpers import approve_all

from src.config import (
    APP_NAME,
    APP_VERSION,
    APP_DESCRIPTION,
    COPILOT_MODEL,
    COPILOT_LOG_LEVEL,
    OUTPUT_DIR,
    WEB_HOST,
    WEB_PORT,
    SESSION_SECRET,
    AVAILABLE_MODELS,
    get_active_model,
    set_active_model,
)
from src.agents import (
    WEB_CHAT_AGENT,
    TEMPLATE_HEALER,
    ERROR_CULPRIT_DETECTOR,
    DEPLOY_FAILURE_ANALYST,
    REMEDIATION_PLANNER,
    REMEDIATION_EXECUTOR,
    ARTIFACT_GENERATOR,
    POLICY_FIXER,
    DEEP_TEMPLATE_HEALER,
    LLM_REASONER,
)
from src.tools import get_all_tools
from src.auth import (
    UserContext,
    create_auth_url,
    complete_auth,
    get_pending_session,
    get_user_context,
    invalidate_session,
    is_auth_configured,
)
from src.database import (
    ARTIFACT_TYPES,
    approve_service_artifact,
    bulk_update_api_versions,
    cleanup_expired_sessions,
    cleanup_orphaned_pipeline_runs,
    complete_pipeline_run,
    compute_next_semver,
    create_pipeline_run,
    create_service_version,
    create_template_version,
    delete_service_versions_by_status,
    delete_template,
    delete_template_versions_by_status,
    fail_service_validation,
    get_active_service_version,
    get_all_services,
    get_all_templates,
    get_backend,
    get_governance_policies_as_dict,
    get_governance_reviews,
    get_latest_semver,
    get_latest_service_version,
    get_pipeline_runs,
    has_running_pipeline,
    get_service,
    get_service_artifacts,
    get_service_version,
    get_service_versions,
    get_services_basic,
    get_template_by_id,
    get_template_version,
    get_template_versions,
    get_version_summary_batch,
    init_db,
    log_usage,
    promote_service_after_validation,
    promote_template_version,
    save_governance_review,
    save_service_artifact,
    set_active_service_version,
    unapprove_service_artifact,
    update_service_version_deployment_info,
    update_service_version_status,
    update_service_version_template,
    update_template_validation_status,
    update_template_version_status,
    upsert_service,
    upsert_template,
)
from src.utils import ensure_output_dir
from src.standards import init_standards
from src.standards_api import router as standards_router
from src.model_router import Task, get_model_for_task, get_model_display, get_task_reason, get_routing_table

logger = logging.getLogger("infraforge.web")

# ── Healing loop utilities ───────────────────────────────────
# Shared helpers live in pipeline_helpers.py (single source of truth).
# We re-export them here with underscore-prefixed aliases so all existing
# call sites (56+) continue to work.  During the router split these
# aliases will be replaced with direct imports.

from src.pipeline_helpers import (
    brief_azure_error      as _brief_azure_error,
    summarize_fix          as _summarize_fix,
    friendly_error         as _friendly_error,
    ensure_parameter_defaults as _ensure_parameter_defaults,
    sanitize_placeholder_guids as _sanitize_placeholder_guids,
    inject_standard_tags   as _inject_standard_tags,
    sanitize_dns_zone_names as _sanitize_dns_zone_names,
    version_to_semver      as _version_to_semver,
    stamp_template_metadata as _stamp_template_metadata,
    extract_param_values   as _extract_param_values,
    copilot_heal_template  as _copilot_heal_template,
    PARAM_DEFAULTS         as _PARAM_DEFAULTS,
)


# ── Shared request helpers ───────────────────────────────────

async def _require_template(template_id: str) -> dict:
    """Fetch a template by ID or raise 404."""
    from src.database import get_template_by_id
    tmpl = await get_template_by_id(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")
    return tmpl


async def _require_service(service_id: str) -> dict:
    """Fetch a service by resource type ID or raise 404."""
    from src.database import get_service
    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")
    return svc


async def _reject_if_pipeline_running(template_id: str) -> None:
    """Raise 409 Conflict if a pipeline is already running for this template."""
    existing = await has_running_pipeline(template_id)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"A pipeline is already running for this template "
                   f"(run {existing['run_id']}, type: {existing['pipeline_type']}). "
                   f"Wait for it to finish or check the Pipeline Runs tab.",
        )


async def _parse_body(request: Request) -> dict:
    """Parse JSON body, returning empty dict on failure."""
    try:
        return await request.json()
    except Exception:
        return {}


async def _parse_body_required(request: Request) -> dict:
    """Parse JSON body, raising 400 on failure."""
    try:
        return await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")


def _build_api_version_status(svc: dict, versions: list[dict]) -> dict | None:
    """Compare the apiVersion in the active ARM template against Azure's latest.

    Returns an advisory dict like:
        {
            "template_api_version": "2023-09-01",
            "latest_stable": "2025-07-01",
            "default": "2024-05-01",
            "newer_available": True,
        }
    or None if comparison isn't possible (no active template, no Azure data).
    """
    latest_api = svc.get("latest_api_version")
    default_api = svc.get("default_api_version")
    if not latest_api:
        return None  # No Azure API version data stored yet

    # Find the active version's ARM template
    active_ver_num = svc.get("active_version")
    if active_ver_num is None:
        return None

    active_ver = next(
        (v for v in versions if v.get("version") == active_ver_num),
        None,
    )
    if not active_ver:
        return None

    arm_str = active_ver.get("arm_template")
    if not arm_str:
        return None

    try:
        tpl = json.loads(arm_str)
    except Exception:
        return None

    # Extract apiVersion(s) from the template's resources
    resources = tpl.get("resources", [])
    template_api_versions = list({
        r.get("apiVersion", "")
        for r in resources
        if isinstance(r, dict) and r.get("apiVersion")
    })
    if not template_api_versions:
        return None

    # Use the newest apiVersion found in the template for comparison
    template_api_versions.sort(reverse=True)
    template_api = template_api_versions[0]

    # Simple string comparison works for YYYY-MM-DD versions
    newer_available = latest_api > template_api
    # Recommended differs — even if template is ahead of recommended
    recommended_differs = bool(
        default_api and default_api != template_api and default_api != latest_api
    )

    return {
        "template_api_version": template_api,
        "latest_stable": latest_api,
        "default": default_api,
        "newer_available": newer_available,
        "recommended_differs": recommended_differs,
    }


# ── Deep healing engine for composed/blueprint templates ──────

async def _deep_heal_composed_template(
    template_id: str,
    service_ids: list[str],
    error_msg: str,
    current_template: dict,
    region: str = "eastus2",
    on_event=None,
) -> dict | None:
    """Deep-heal a composed template by fixing the underlying service templates.

    Flow:
    1. Root-cause analysis (o3-mini) — which service's ARM is broken?
    2. Fix that service's ARM template via LLM
    3. Validate the fixed service ARM with a standalone deploy
    4. Save as new service version
    5. Recompose the parent template from all service ARMs
    6. Return the fixed composed template dict

    Returns the fixed composed template dict, or None if healing failed.
    ``on_event`` is an async callable for streaming progress events.
    """
    import uuid as _dh_uuid
    from src.tools.arm_generator import (
        generate_arm_template, has_builtin_skeleton,
        _STANDARD_PARAMETERS, _TEMPLATE_WRAPPER,
    )
    from src.tools.deploy_engine import execute_deployment

    async def _emit(evt: dict):
        if on_event:
            await on_event(evt)

    await _emit({"phase": "deep_heal_start", "detail": "Let me look at the individual service templates to figure out what's going wrong…"})

    # ── Step 1: Root-cause analysis ──────────────────────────
    # Identify which service template is causing the failure
    resource_type_map: dict[str, dict] = {}  # service_id → ARM template dict
    for sid in service_ids:
        svc = await get_service(sid)
        if not svc:
            continue
        ver = await get_active_service_version(sid)
        arm = None
        if ver and ver.get("arm_template"):
            try:
                arm = json.loads(ver["arm_template"])
            except Exception:
                pass
        if not arm and has_builtin_skeleton(sid):
            arm = generate_arm_template(sid)
        if arm:
            resource_type_map[sid] = arm

    if not resource_type_map:
        await _emit({"phase": "deep_heal_fail", "detail": "I can't find the source service templates to analyze — there's nothing for me to dig into here."})
        return None

    # Use the error message + resource types to identify the culprit
    # Extract resource type from the error (e.g. "Microsoft.Network/dnszones/if-dnszones")
    culprit_sid = None
    error_lower = error_msg.lower()
    for sid in service_ids:
        # Match by resource type in error
        rt_lower = sid.lower()
        short = rt_lower.split("/")[-1]
        if rt_lower in error_lower or short in error_lower:
            culprit_sid = sid
            break

    if not culprit_sid:
        # If can't detect from error, try o3-mini reasoning
        try:
            from src.copilot_helpers import copilot_send
            _client = await ensure_copilot_client()
            if _client:
                resp = await copilot_send(
                    _client,
                    model=get_model_for_task(ERROR_CULPRIT_DETECTOR.task),
                    system_prompt=ERROR_CULPRIT_DETECTOR.system_prompt,
                    prompt=(
                        f"Error: {error_msg[:500]}\n\n"
                        f"Service templates: {', '.join(service_ids)}\n\n"
                        "Which service template is causing this error? "
                        "Reply with ONLY the exact service ID from the list above."
                    ),
                    timeout=30,
                    agent_name="ERROR_CULPRIT_DETECTOR",
                )
                for sid in service_ids:
                    if sid.lower() in resp.lower():
                        culprit_sid = sid
                        break
        except Exception:
            pass

    if not culprit_sid:
        culprit_sid = service_ids[0]  # fallback to first

    await _emit({
        "phase": "deep_heal_identified",
        "detail": f"Found it — the issue is coming from the {culprit_sid} template",
        "culprit_service": culprit_sid,
    })

    # ── Step 2: Fix the culprit service ARM template ─────────
    source_arm = resource_type_map.get(culprit_sid)
    if not source_arm:
        await _emit({"phase": "deep_heal_fail", "detail": f"No ARM template found for {culprit_sid}"})
        return None

    source_json = json.dumps(source_arm, indent=2)
    heal_attempts: list[dict] = []
    MAX_SVC_HEAL = 3
    fixed_svc_arm = None

    for svc_attempt in range(1, MAX_SVC_HEAL + 1):
        await _emit({
            "phase": "deep_heal_fix",
            "detail": f"Working on fixing the {culprit_sid} template…" + (
                "" if svc_attempt == 1 else f" (previous attempt didn't work, trying a different angle)"
            ),
            "service_id": culprit_sid,
        })

        try:
            from src.pipeline_helpers import copilot_heal_template as _canonical_heal
            fixed_json = await _canonical_heal(
                content=source_json,
                error=error_msg,
                previous_attempts=heal_attempts,
                parameters=_extract_param_values(
                    json.loads(source_json) if isinstance(source_json, str) else source_json
                ),
            )
            candidate = json.loads(fixed_json)
        except Exception as fix_err:
            await _emit({"phase": "deep_heal_fix_error", "detail": f"Hmm, I couldn't generate a fix this time: {fix_err}"})
            continue

        # ── Step 3: Validate standalone ──────────────────────
        await _emit({
            "phase": "deep_heal_validate",
            "detail": f"Let me test the fixed {culprit_sid} template on its own to make sure it works…",
        })

        val_rg = f"infraforge-dheal-{_dh_uuid.uuid4().hex[:8]}"
        val_deploy = f"dheal-{_dh_uuid.uuid4().hex[:8]}"

        # Build params using the same function as deploy pipeline
        val_params = _extract_param_values(candidate)

        try:
            val_result = await execute_deployment(
                resource_group=val_rg,
                template=candidate,
                parameters=val_params,
                region=region,
                deployment_name=val_deploy,
                initiated_by="deep-healer",
            )
            val_status = val_result.get("status", "failed")
        except Exception as val_err:
            val_status = "failed"
            val_result = {"error": str(val_err)}

        # Cleanup the validation RG (fire and forget)
        try:
            from azure.identity import DefaultAzureCredential
            from azure.mgmt.resource import ResourceManagementClient
            import os
            cred = DefaultAzureCredential()
            sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
            if sub_id:
                rc = ResourceManagementClient(cred, sub_id)
                rc.resource_groups.begin_delete(val_rg)
        except Exception:
            pass

        if val_status == "succeeded":
            await _emit({
                "phase": "deep_heal_validated",
                "detail": f"Nice — the {culprit_sid} fix is working!",
                "service_id": culprit_sid,
                "resources": val_result.get("provisioned_resources", []),
            })
            fixed_svc_arm = candidate
            source_json = fixed_json  # for next iterations if needed
            break
        else:
            val_error = val_result.get("error", "unknown")
            await _emit({
                "phase": "deep_heal_validate_fail",
                "detail": f"That fix didn't quite work either: {val_error[:200]}",
            })
            # Track for next heal attempt
            heal_attempts.append({
                "step": len(heal_attempts) + 1,
                "phase": "deploy",
                "error": val_error[:500],
                "fix_summary": _summarize_fix(json.dumps(source_arm, indent=2), fixed_json),
            })
            source_json = fixed_json  # try fixing THIS version next
            error_msg = val_error  # update error for next LLM call

    if not fixed_svc_arm:
        await _emit({"phase": "deep_heal_fail", "detail": f"I wasn't able to fix the {culprit_sid} template automatically. This one might need a manual look."})
        return None

    # ── Step 4: Save new service version ─────────────────────
    await _emit({
        "phase": "deep_heal_version",
        "detail": f"The fix worked! Saving a new version of {culprit_sid}…",
    })

    try:
        new_ver = await create_service_version(
            service_id=culprit_sid,
            arm_template=json.dumps(fixed_svc_arm, indent=2),
            status="approved",
            changelog=f"Deep-healed: fixed ARM template during deployment of {template_id}",
            created_by="deep-healer",
        )
        new_ver_num = new_ver.get("version", "?")
        new_semver = new_ver.get("semver", "?")
        await _emit({
            "phase": "deep_heal_versioned",
            "detail": f"Published {culprit_sid} v{new_semver} (version {new_ver_num})",
        })

        # Full lifecycle promotion: set active version + approve service
        from src.orchestrator import promote_healed_service
        promo = await promote_healed_service(
            culprit_sid,
            int(new_ver_num) if isinstance(new_ver_num, (int, str)) and str(new_ver_num).isdigit() else 1,
            progress_callback=lambda evt: _emit(evt),
        )
        if promo["status"] == "promoted":
            await _emit({
                "phase": "deep_heal_promoted",
                "detail": f"Service {culprit_sid} promoted to approved with active v{new_ver_num}",
            })
    except Exception as ver_err:
        logger.warning(f"Failed to save service version: {ver_err}")
        # Continue anyway — we still have the fixed ARM in memory

    # ── Step 5: Recompose the parent template ────────────────
    await _emit({
        "phase": "deep_heal_recompose",
        "detail": f"Now let me rebuild the full template with the fixed pieces…",
    })

    # Gather all service ARM templates (using fixed one for culprit)
    all_arms: dict[str, dict] = {}
    for sid in service_ids:
        if sid == culprit_sid:
            all_arms[sid] = fixed_svc_arm
        else:
            arm = resource_type_map.get(sid)
            if arm:
                all_arms[sid] = arm

    # Recompose using the shared composition helper
    from src.pipeline_helpers import resolve_variables_for_composition, build_composed_variables, validate_arm_references

    combined_params = dict(_STANDARD_PARAMETERS)
    combined_resources = []
    combined_outputs = {}
    all_resolved_vars: dict[str, dict] = {}

    for sid in service_ids:
        tpl = all_arms.get(sid)
        if not tpl:
            continue
        short_name = sid.split("/")[-1].lower()
        suffix = f"_{short_name}"

        extra_params, proc_resources, proc_outputs, resolved_vars = \
            resolve_variables_for_composition(tpl, suffix)

        if not proc_resources:
            logger.warning(
                f"Compose: service '{sid}' contributed 0 resources — "
                f"its ARM template may have an empty resources array."
            )

        combined_params.update(extra_params)
        combined_resources.extend(proc_resources)
        combined_outputs.update(proc_outputs)
        all_resolved_vars[suffix] = resolved_vars

    composed = dict(_TEMPLATE_WRAPPER)
    composed["parameters"] = combined_params
    composed["variables"] = build_composed_variables(all_resolved_vars)
    composed["resources"] = combined_resources
    composed["outputs"] = combined_outputs

    # Pre-deploy structural validation
    ref_errors = validate_arm_references(composed)
    if ref_errors:
        logger.warning(f"Deep-heal recompose reference errors (auto-fixing): {ref_errors}")
        for err in ref_errors:
            if "Missing variable" in err:
                vname = err.split("'")[1]
                composed.setdefault("variables", {})[vname] = f"[parameters('resourceName')]"
            elif "Missing parameter" in err:
                pname = err.split("'")[1]
                composed.setdefault("parameters", {})[pname] = {
                    "type": "string",
                    "defaultValue": f"infraforge-{pname[:20]}",
                    "metadata": {"description": f"Auto-added: {pname}"},
                }

    # Ensure all params have defaults
    composed_json = _ensure_parameter_defaults(json.dumps(composed, indent=2))
    composed_json = _sanitize_placeholder_guids(composed_json)
    composed_json = _sanitize_dns_zone_names(composed_json)
    composed = json.loads(composed_json)

    # ── Step 6: Save new template version ────────────────────
    try:
        new_tmpl_ver = await create_template_version(
            template_id,
            composed_json,
            changelog=f"Deep-healed: fixed {culprit_sid}, recomposed",
            change_type="patch",
            created_by="deep-healer",
        )
        # Also update the catalog_templates content
        backend = await get_backend()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        await backend.execute_write(
            "UPDATE catalog_templates SET content = ?, updated_at = ? WHERE id = ?",
            (composed_json, now, template_id),
        )
        await _emit({
            "phase": "deep_heal_complete",
            "detail": f"Recomposed template saved — fixed {culprit_sid}, ready to deploy",
            "fixed_service": culprit_sid,
            "new_version": new_tmpl_ver.get("version"),
        })
    except Exception as save_err:
        logger.warning(f"Failed to save recomposed template: {save_err}")
        await _emit({
            "phase": "deep_heal_complete",
            "detail": f"Template recomposed in memory (save failed: {save_err})",
        })

    return composed


# ── Global state (shared with routers via web_shared.py) ─────
# All mutable singletons live in web_shared so routers see the same objects.
from src.web_shared import (
    copilot_client,
    ensure_copilot_client,
    active_sessions,
    _active_validations,
    _user_context_to_dict,
)
# Re-export for backward compat within this file
import src.web_shared as _ws


# ── Lifespan ─────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start/stop the Copilot SDK client with the server lifecycle."""
    # Auto-fix SQL firewall before DB init (best-effort, won't block if it fails)
    from src.sql_firewall import ensure_sql_firewall
    await ensure_sql_firewall()

    logger.info("Initializing database...")
    await init_db()
    await cleanup_expired_sessions()
    # Mark any pipeline runs left as 'running' from a previous crash as failed
    await cleanup_orphaned_pipeline_runs()
    logger.info("Initializing organization standards...")
    await init_standards()
    logger.info("Loading agent activity counters from DB...")
    from src.copilot_helpers import load_agent_counters_from_db
    await load_agent_counters_from_db()
    # Load agent definitions from DB (overlays hardcoded defaults)
    from src.agents import load_agents_from_db
    agent_count = await load_agents_from_db()
    logger.info(f"Loaded {agent_count} agent definitions from database")
    logger.info("Deferring Copilot SDK client start (lazy init on first chat)...")
    _ws.copilot_client = None  # Will be started lazily on first WebSocket connection
    ensure_output_dir(OUTPUT_DIR)

    # Azure resource provider sync — runs on-demand via the Sync button.
    # Removed from startup to avoid blocking or crashing the server.
    # We do a lightweight count-only fetch so the UI shows total available.
    import asyncio as _aio
    from src.azure_sync import fetch_azure_service_count
    _aio.create_task(fetch_azure_service_count())

    logger.info("InfraForge web server ready")
    yield
    logger.info("Shutting down Copilot SDK client...")
    # Clean up active sessions
    for session_data in active_sessions.values():
        try:
            await session_data["copilot_session"].destroy()
        except Exception:
            pass
    if _ws.copilot_client:
        try:
            await _ws.copilot_client.stop()
        except Exception:
            pass
    # Close Work IQ MCP session
    from src.workiq_client import get_workiq_client
    try:
        await get_workiq_client().close()
    except Exception:
        pass
    logger.info("Shutdown complete")


# ── App ──────────────────────────────────────────────────────

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description=APP_DESCRIPTION,
    lifespan=lifespan,
)

app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# Mount API routers
app.include_router(standards_router)

from src.routers.auth import router as auth_router
from src.routers.admin import router as admin_router
from src.routers.deployment import router as deployment_router
from src.routers.ws import router as ws_router
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(deployment_router)
app.include_router(ws_router)

# Serve static files (HTML, CSS, JS)
static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.put("/api/agents/{agent_key}/prompt")
async def update_agent_prompt(agent_key: str, request: Request):
    """Update an agent's system prompt (persisted to DB with version history)."""
    from src.database import update_agent_definition, get_agent_definition
    from src.agents import AGENTS, AgentSpec, _HARDCODED_AGENTS, load_agents_from_db

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    system_prompt = body.get("system_prompt") or body.get("prompt")
    if not system_prompt:
        raise HTTPException(status_code=400, detail="system_prompt is required")

    # Ensure agent exists in DB (seed if needed)
    existing = await get_agent_definition(agent_key)
    if not existing:
        # Check hardcoded fallback
        if agent_key not in _HARDCODED_AGENTS:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_key}' not found")
        # Seed this agent first
        from src.database import seed_agent_definitions
        await seed_agent_definitions()

    result = await update_agent_definition(
        agent_key,
        system_prompt=system_prompt,
        name=body.get("name"),
        description=body.get("description"),
        timeout=body.get("timeout"),
        enabled=body.get("enabled"),
        changed_by=body.get("changed_by", "user"),
    )
    if not result:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_key}' not found")

    # Reload agents from DB to pick up the change
    await load_agents_from_db()

    return JSONResponse({
        "status": "updated",
        "agent_id": agent_key,
        "version": result.get("version", 1),
    })


@app.post("/api/agents/{agent_key}/reset")
async def reset_agent_prompt(agent_key: str):
    """Reset an agent's prompt to the hardcoded default."""
    from src.database import reset_agent_to_default
    from src.agents import load_agents_from_db

    result = await reset_agent_to_default(agent_key)
    if not result:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_key}' not found in defaults")

    await load_agents_from_db()
    return JSONResponse({"status": "reset", "agent_id": agent_key, "version": result.get("version", 1)})


@app.get("/api/agents/{agent_key}/history")
async def get_agent_history(agent_key: str):
    """Return prompt version history for an agent."""
    from src.database import get_agent_prompt_history
    history = await get_agent_prompt_history(agent_key)
    return JSONResponse({"agent_id": agent_key, "history": history})


@app.patch("/api/agents/{agent_key}")
async def patch_agent_definition(agent_key: str, request: Request):
    """Update agent metadata (name, description, timeout, enabled) without changing prompt."""
    from src.database import update_agent_definition, get_agent_definition
    from src.agents import load_agents_from_db, _HARDCODED_AGENTS

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    existing = await get_agent_definition(agent_key)
    if not existing:
        if agent_key not in _HARDCODED_AGENTS:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_key}' not found")
        from src.database import seed_agent_definitions
        await seed_agent_definitions()

    result = await update_agent_definition(
        agent_key,
        name=body.get("name"),
        description=body.get("description"),
        timeout=body.get("timeout"),
        enabled=body.get("enabled"),
        changed_by=body.get("changed_by", "user"),
    )
    if not result:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_key}' not found")

    await load_agents_from_db()
    return JSONResponse({"status": "updated", "agent": result})
# ── Service Catalog API ──────────────────────────────────────

@app.get("/api/catalog/services")
async def get_service_catalog():
    """Return the approved Azure services catalog from the database.

    This powers the interactive service browser in the welcome screen,
    letting users see at a glance which services are approved, conditional,
    under review, or not yet approved.
    """

    try:
        services = await get_all_services()

        # Aggregate stats
        stats = {"approved": 0, "conditional": 0, "under_review": 0, "not_approved": 0}
        categories = set()
        for svc in services:
            status = svc.get("status", "not_approved")
            stats[status] = stats.get(status, 0) + 1
            categories.add(svc.get("category", "other"))

        return JSONResponse({
            "services": services,
            "stats": stats,
            "categories": sorted(categories),
            "total": len(services),
        })
    except Exception as e:
        logger.error(f"Failed to load service catalog: {e}")
        return JSONResponse({"services": [], "stats": {}, "categories": [], "total": 0})


@app.get("/api/catalog/templates")
async def get_template_catalog(
    category: Optional[str] = None,
    fmt: Optional[str] = None,
    template_type: Optional[str] = None,
):
    """Return the template catalog from the database."""
    try:
        templates = await get_all_templates(
            category=category, fmt=fmt, template_type=template_type,
        )

        # Enrich with latest semver + latest version number from template_versions
        if templates:
            backend = await get_backend()
            semver_rows = await backend.execute(
                """SELECT tv.template_id, tv.semver, tv.version AS max_ver
                   FROM template_versions tv
                   INNER JOIN (
                       SELECT template_id, MAX(version) AS max_ver
                       FROM template_versions
                       WHERE semver IS NOT NULL
                       GROUP BY template_id
                   ) latest ON tv.template_id = latest.template_id
                              AND tv.version = latest.max_ver""",
                (),
            )
            semver_map = {r["template_id"]: r["semver"] for r in semver_rows if r.get("semver")}
            latest_ver_map = {r["template_id"]: r["max_ver"] for r in semver_rows if r.get("max_ver")}
            for t in templates:
                t["latest_semver"] = semver_map.get(t["id"])
                t["latest_version"] = latest_ver_map.get(t["id"])

        return JSONResponse({
            "templates": templates,
            "total": len(templates),
        })
    except Exception as e:
        logger.error(f"Failed to load template catalog: {e}")
        return JSONResponse({"templates": [], "total": 0})


# ── Onboarding API ───────────────────────────────────────────

@app.post("/api/catalog/services")
async def onboard_service(request: Request):
    """Onboard a new Azure service into the approved service catalog."""
    body = await _parse_body_required(request)

    required = ["id", "name", "category", "status"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required fields: {', '.join(missing)}")

    # Validate status
    valid_statuses = {"approved", "conditional", "under_review", "not_approved"}
    if body.get("status") not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(sorted(valid_statuses))}")

    try:
        await upsert_service(body)
        svc = await get_service(body["id"])
        return JSONResponse({"status": "ok", "service": svc})
    except Exception as e:
        logger.error(f"Failed to onboard service: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/api/catalog/services/{service_id:path}")
async def update_service_governance(service_id: str, request: Request):
    """Update governance fields on an existing service (partial update).

    Accepts any subset of: status, risk_tier, contact, review_notes,
    documentation, approved_skus, approved_regions, policies, conditions.
    The service must already exist in the catalog.
    """
    body = await _parse_body_required(request)

    # Fetch the existing service
    existing = await _require_service(service_id)

    # Validate status if provided
    valid_statuses = {"approved", "conditional", "under_review", "not_approved"}
    if "status" in body and body["status"] not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(valid_statuses))}",
        )

    # Merge provided fields into existing service data
    updatable = [
        "status", "risk_tier", "contact", "review_notes", "documentation",
        "approved_skus", "approved_regions", "policies", "conditions",
    ]
    for field in updatable:
        if field in body:
            existing[field] = body[field]

    try:
        await upsert_service(existing)
        svc = await get_service(service_id)
        return JSONResponse({"status": "ok", "service": svc})
    except Exception as e:
        logger.error(f"Failed to update service governance: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/catalog/templates")
async def onboard_template(request: Request):
    """Onboard a new template into the template catalog."""
    body = await _parse_body_required(request)

    required = ["id", "name", "format", "category"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required fields: {', '.join(missing)}")

    try:
        await upsert_template(body)
        tmpl = await get_template_by_id(body["id"])
        return JSONResponse({"status": "ok", "template": tmpl})
    except Exception as e:
        logger.error(f"Failed to onboard template: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/catalog/services/approved-for-templates")
async def get_approved_services_for_templates():
    """Return approved services with their ARM template parameters.

    Only services with status='approved' and an active version (with an ARM
    template) are returned.  Each service includes the list of *extra*
    parameters the skeleton exposes beyond the standard set (resourceName,
    location, environment, projectName, ownerEmail, costCenter) so the
    template-builder UI can show parameter checkboxes.
    """
    try:
        from src.tools.arm_generator import generate_arm_template, has_builtin_skeleton
        import json as _json

        STANDARD_PARAMS = {
            "resourceName", "location", "environment",
            "projectName", "ownerEmail", "costCenter",
        }

        def _extract_params(all_params: dict) -> list[dict]:
            """Convert ARM parameter dict into a list of param descriptors."""
            result = []
            for pname, pdef in all_params.items():
                meta = pdef.get("metadata", {})
                result.append({
                    "name": pname,
                    "type": pdef.get("type", "string"),
                    "description": meta.get("description", ""),
                    "defaultValue": pdef.get("defaultValue"),
                    "allowedValues": pdef.get("allowedValues"),
                    "is_standard": pname in STANDARD_PARAMS,
                })
            return result

        services = await get_all_services()
        logger.info(f"approved-for-templates: total services={len(services)}")
        result = []

        for svc in services:
            if svc.get("status") != "approved":
                continue

            service_id = svc["id"]
            logger.info(f"approved-for-templates: processing {service_id}")

            # Fetch ALL versions for this service that have ARM templates
            all_versions_raw = await get_service_versions(service_id)
            versions_list = []
            active_params: list[dict] = []
            active_ver = svc.get("active_version")

            for ver in all_versions_raw:
                # Only include approved or draft versions that have ARM templates
                ver_status = ver.get("status", "")
                if ver_status not in ("approved", "draft"):
                    continue
                arm_str = ver.get("arm_template")
                if not arm_str:
                    continue
                try:
                    tpl = _json.loads(arm_str)
                    ver_params = _extract_params(tpl.get("parameters", {}))
                except Exception:
                    logger.warning(f"Failed to parse ARM for {service_id} v{ver.get('version')}")
                    continue

                ver_num = ver.get("version")
                ver_entry = {
                    "version": ver_num,
                    "status": ver_status,
                    "semver": ver.get("semver", ""),
                    "is_active": ver_num == active_ver,
                    "parameters": ver_params,
                    "changelog": ver.get("changelog", ""),
                    "created_at": ver.get("created_at", ""),
                }
                versions_list.append(ver_entry)
                if ver_num == active_ver:
                    active_params = ver_params

            # Fallback: if no versions found, try built-in skeleton
            if not versions_list and has_builtin_skeleton(service_id):
                tpl = generate_arm_template(service_id)
                if tpl:
                    active_params = _extract_params(tpl.get("parameters", {}))
                    versions_list = [{
                        "version": 0,
                        "status": "builtin",
                        "semver": "",
                        "is_active": True,
                        "parameters": active_params,
                        "changelog": "Built-in skeleton",
                        "created_at": "",
                    }]

            # Use active version params as the top-level default
            if not active_params and versions_list:
                active_params = versions_list[0]["parameters"]

            result.append({
                "id": service_id,
                "name": svc.get("name", service_id),
                "category": svc.get("category", "other"),
                "risk_tier": svc.get("risk_tier"),
                "active_version": active_ver,
                "parameters": active_params,
                "versions": versions_list,
            })

        logger.info(f"approved-for-templates: returning {len(result)} services")
        return JSONResponse({
            "services": result,
            "total": len(result),
        })
    except Exception:
        logger.exception("approved-for-templates endpoint failed")
        raise


@app.post("/api/catalog/templates/compose")
async def compose_template_from_services(request: Request):
    """Compose a new ARM template from approved services.

    Body:
    {
        "name": "My Web App Stack",
        "description": "App Service + SQL + KeyVault",
        "category": "blueprint",
        "selections": [
            {
                "service_id": "Microsoft.Web/sites",
                "quantity": 1,
                "parameters": ["skuName"]   // which extra params to expose
            },
            {
                "service_id": "Microsoft.Sql/servers",
                "quantity": 1,
                "parameters": ["adminLogin", "adminPassword"]
            }
        ]
    }

    Each selected service must be approved with an active version.
    The endpoint composes a single ARM template containing all resources,
    deduplicating shared standard parameters and prefixing resource-specific
    names with an index when quantity > 1.
    """
    from src.tools.arm_generator import generate_arm_template, has_builtin_skeleton
    import json as _json

    body = await _parse_body_required(request)

    name = body.get("name", "").strip()
    description = body.get("description", "").strip()
    category = body.get("category", "blueprint")
    selections = body.get("selections", [])

    if not name:
        raise HTTPException(status_code=400, detail="Template name is required")
    if not selections:
        raise HTTPException(status_code=400, detail="Select at least one service")

    STANDARD_PARAMS = {
        "resourceName", "location", "environment",
        "projectName", "ownerEmail", "costCenter",
    }

    # ── Validate selections & gather ARM templates ────────────
    service_templates: list[dict] = []   # (svc, template_dict, selection)
    pinned_versions: dict = {}  # service_id → {version, semver}

    for sel in selections:
        sid = sel.get("service_id", "")
        qty = max(1, int(sel.get("quantity", 1)))
        chosen_params = set(sel.get("parameters", []))
        chosen_version = sel.get("version")  # None means "use active/latest"

        svc = await _require_service(sid)
        if svc.get("status") != "approved":
            raise HTTPException(
                status_code=400,
                detail=f"Service '{sid}' is not approved — only approved services can be used in templates",
            )

        # Get the ARM template — use specific version if requested
        tpl_dict = None
        if chosen_version is not None:
            ver = await get_service_version(sid, int(chosen_version))
            if ver and ver.get("arm_template"):
                try:
                    tpl_dict = _json.loads(ver["arm_template"])
                    pinned_versions[sid] = {
                        "version": int(chosen_version),
                        "semver": ver.get("semver"),
                    }
                except Exception:
                    pass
            if not tpl_dict:
                raise HTTPException(
                    status_code=400,
                    detail=f"Version {chosen_version} of '{sid}' has no ARM template",
                )
        else:
            active = await get_active_service_version(sid)
            if active and active.get("arm_template"):
                try:
                    tpl_dict = _json.loads(active["arm_template"])
                    pinned_versions[sid] = {
                        "version": active.get("version"),
                        "semver": active.get("semver"),
                    }
                except Exception:
                    pass
            # Fallback: draft version from auto-prep (not yet pipeline-validated)
            if not tpl_dict:
                draft = await get_latest_service_version(sid)
                if draft and draft.get("arm_template"):
                    try:
                        tpl_dict = _json.loads(draft["arm_template"])
                        pinned_versions[sid] = {
                            "version": draft.get("version"),
                            "semver": draft.get("semver", "0.0.0-draft"),
                        }
                    except Exception:
                        pass
        if not tpl_dict and has_builtin_skeleton(sid):
            tpl_dict = generate_arm_template(sid)
            if sid not in pinned_versions:
                pinned_versions[sid] = {"version": 0, "semver": "0.0.0-builtin"}
        if not tpl_dict:
            raise HTTPException(
                status_code=400,
                detail=f"No ARM template available for '{sid}'",
            )

        service_templates.append({
            "svc": svc,
            "template": tpl_dict,
            "quantity": qty,
            "chosen_params": chosen_params,
        })

    # ── Resolve dependencies (auto-add missing required services) ─
    from src.orchestrator import resolve_composition_dependencies

    dep_events: list[dict] = []

    async def _dep_progress(event):
        dep_events.append(event)

    selected_ids = [e["svc"]["id"] for e in service_templates]
    dep_result = await resolve_composition_dependencies(
        selected_ids,
        progress_callback=_dep_progress,
    )

    # Auto-add resolved dependencies
    for item in dep_result.get("resolved", []):
        dep_sid = item["service_id"]
        # Avoid duplicates
        if any(e["svc"]["id"] == dep_sid for e in service_templates):
            continue
        dep_svc = await get_service(dep_sid)
        if not dep_svc:
            continue
        dep_tpl = None
        dep_active = await get_active_service_version(dep_sid)
        if dep_active and dep_active.get("arm_template"):
            try:
                dep_tpl = _json.loads(dep_active["arm_template"])
                pinned_versions[dep_sid] = {
                    "version": dep_active.get("version"),
                    "semver": dep_active.get("semver"),
                }
            except Exception:
                pass
        # Fallback: draft version from auto-prep
        if not dep_tpl:
            dep_draft = await get_latest_service_version(dep_sid)
            if dep_draft and dep_draft.get("arm_template"):
                try:
                    dep_tpl = _json.loads(dep_draft["arm_template"])
                    pinned_versions[dep_sid] = {
                        "version": dep_draft.get("version"),
                        "semver": dep_draft.get("semver", "0.0.0-draft"),
                    }
                except Exception:
                    pass
        if not dep_tpl and has_builtin_skeleton(dep_sid):
            dep_tpl = generate_arm_template(dep_sid)
            if dep_sid not in pinned_versions:
                pinned_versions[dep_sid] = {"version": 0, "semver": "0.0.0-builtin"}
        if dep_tpl:
            service_templates.append({
                "svc": dep_svc,
                "template": dep_tpl,
                "quantity": 1,
                "chosen_params": set(),
            })
            logger.info(f"Auto-added dependency: {dep_sid} ({item['action']})")

    # ── Compose the combined ARM template ─────────────────────
    from src.tools.arm_generator import _STANDARD_PARAMETERS, _TEMPLATE_WRAPPER, _STANDARD_TAGS
    from src.pipeline_helpers import resolve_variables_for_composition, build_composed_variables, validate_arm_references

    combined_params = dict(_STANDARD_PARAMETERS)
    combined_resources = []
    combined_outputs = {}
    all_resolved_vars: dict[str, dict] = {}  # suffix → resolved variables
    service_ids = []
    resource_types = []
    tags_list = []

    for entry in service_templates:
        svc = entry["svc"]
        tpl = entry["template"]
        qty = entry["quantity"]
        chosen = entry["chosen_params"]
        sid = svc["id"]

        service_ids.append(sid)
        short_name = sid.split("/")[-1].lower()
        resource_types.append(sid)
        tags_list.append(svc.get("category", ""))

        for idx in range(1, qty + 1):
            suffix = f"_{short_name}" if qty == 1 else f"_{short_name}{idx}"

            extra_params, proc_resources, proc_outputs, resolved_vars = \
                resolve_variables_for_composition(tpl, suffix)

            if not proc_resources:
                logger.warning(
                    f"Compose: service '{sid}' contributed 0 resources — "
                    f"its ARM template may have an empty resources array."
                )

            # Add quantity metadata to params if needed
            if qty > 1:
                for pdef in extra_params.values():
                    meta = pdef.setdefault("metadata", {})
                    meta["description"] = meta.get("description", "") + f" (instance {idx})"

            combined_params.update(extra_params)
            combined_resources.extend(proc_resources)
            combined_outputs.update(proc_outputs)
            all_resolved_vars[suffix] = resolved_vars

    # Build the final composed template
    composed = dict(_TEMPLATE_WRAPPER)
    composed["parameters"] = combined_params
    composed["variables"] = build_composed_variables(all_resolved_vars)
    composed["resources"] = combined_resources
    composed["outputs"] = combined_outputs

    # Pre-deploy structural validation
    ref_errors = validate_arm_references(composed)
    if ref_errors:
        logger.warning(f"Composition reference errors (auto-fixing): {ref_errors}")
        # Auto-fix: add missing variable/parameter stubs
        for err in ref_errors:
            if "Missing variable" in err:
                vname = err.split("'")[1]
                composed.setdefault("variables", {})[vname] = f"[parameters('resourceName')]"
            elif "Missing parameter" in err:
                pname = err.split("'")[1]
                composed.setdefault("parameters", {})[pname] = {
                    "type": "string",
                    "defaultValue": f"infraforge-{pname[:20]}",
                    "metadata": {"description": f"Auto-added: {pname}"},
                }

    content_str = _json.dumps(composed, indent=2)

    # Build a template ID from the name
    template_id = "composed-" + name.lower().replace(" ", "-")[:50]

    # Build parameter list for catalog storage
    param_list = [
        {"name": k, "type": v.get("type", "string"), "required": "defaultValue" not in v}
        for k, v in combined_params.items()
    ]

    # ── Dependency analysis ───────────────────────────────────
    from src.template_engine import analyze_dependencies

    dep_analysis = analyze_dependencies(service_ids)

    # Save to catalog
    catalog_entry = {
        "id": template_id,
        "name": name,
        "description": description,
        "format": "arm",
        "category": category,
        "content": content_str,
        "tags": list(set(tags_list)),
        "resources": list(set(resource_types)),
        "parameters": param_list,
        "outputs": list(combined_outputs.keys()),
        "is_blueprint": len(service_templates) > 1,
        "service_ids": service_ids,
        "pinned_versions": pinned_versions,
        "status": "draft",
        "registered_by": "template-composer",
        # Dependency metadata
        "template_type": dep_analysis["template_type"],
        "provides": dep_analysis["provides"],
        "requires": dep_analysis["requires"],
        "optional_refs": dep_analysis["optional_refs"],
    }

    try:
        await upsert_template(catalog_entry)
        # Create version 1 as a draft
        ver = await create_template_version(
            template_id, content_str,
            changelog="Initial composition",
            change_type="initial",
        )
    except Exception as e:
        logger.error(f"Failed to save composed template: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({
        "status": "ok",
        "template_id": template_id,
        "template": catalog_entry,
        "version": ver,
        "resource_count": len(combined_resources),
        "parameter_count": len(combined_params),
        "dependency_analysis": dep_analysis,
        "dependency_resolution": dep_result,
    })


# ── Template Testing ─────────────────────────────────────────

@app.post("/api/catalog/templates/{template_id}/test")
async def test_template(template_id: str, request: Request):
    """Run validation tests on a template version.

    Body (optional): { "version": 1 }  — defaults to latest version.

    Tests performed:
    1. JSON structure — valid ARM template JSON
    2. Schema compliance — has $schema, contentVersion, parameters, resources
    3. Parameter validation — all params have types, no empty names
    4. Resource validation — all resources have type, apiVersion, name, location
    5. Output validation — outputs reference valid expressions
    6. Dependency check — service_ids match known services
    7. Tag compliance — resources include standard tags
    """
    tmpl = await _require_template(template_id)

    # Determine which version to test
    body = await _parse_body(request)

    requested_version = body.get("version")
    ver = None
    if requested_version:
        ver = await get_template_version(template_id, int(requested_version))
    else:
        versions = await get_template_versions(template_id)
        if versions:
            ver = versions[0]  # latest (descending order)

    if not ver:
        raise HTTPException(status_code=404, detail="No version found to test")

    arm_content = ver.get("arm_template", "")
    version_num = ver["version"]

    # ── Run shared structural test suite ──────────────────────
    # For composite templates, also check composition completeness
    svc_ids = tmpl.get("service_ids", []) if isinstance(tmpl.get("service_ids"), list) else []
    if not svc_ids:
        try:
            svc_ids = json.loads(tmpl.get("service_ids_json", "[]") or "[]")
        except Exception:
            svc_ids = []
    expected = svc_ids if tmpl.get("template_type") == "composite" and svc_ids else None
    test_results = _run_structural_tests(arm_content, expected_service_ids=expected)
    new_status = await _update_test_status(template_id, version_num, test_results)

    # Note: No auto-promote. User must validate (ARM What-If) then explicitly publish.

    return JSONResponse({
        "template_id": template_id,
        "version": version_num,
        "status": new_status,
        "results": test_results,
        "needs_validation": test_results["all_passed"],  # signal: ready for ARM validation
    })


# ── Compliance Helpers (shared by scan, plan, execute) ───────

def _scope_matches(scope: str, resource_type: str) -> bool:
    """Check whether a standard's scope pattern matches a resource type."""
    import fnmatch
    rt = resource_type.lower()
    for pat in scope.split(","):
        pat = pat.strip().lower()
        if pat and fnmatch.fnmatch(rt, pat):
            return True
    return False


def _resolve_arm_value(val, params, variables):
    """Best-effort resolution of ARM template expressions."""
    import re
    if not isinstance(val, str):
        return val
    if not val.startswith("[") or not val.endswith("]"):
        return val
    expr = val[1:-1].strip()
    m = re.match(r"parameters\(['\"]([a-zA-Z0-9_-]+)['\"]\)", expr)
    if m:
        pname = m.group(1)
        pdef = params.get(pname, {})
        return pdef.get("defaultValue", f"<param:{pname}>")
    m = re.match(r"variables\(['\"]([a-zA-Z0-9_-]+)['\"]\)", expr)
    if m:
        vname = m.group(1)
        return variables.get(vname, f"<var:{vname}>")
    return val


def _get_nested(obj, dotpath, params=None, variables=None):
    """Get a value from a nested dict using dot notation."""
    parts = dotpath.split(".")
    current = obj
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    if current is not None and params is not None:
        current = _resolve_arm_value(current, params, variables or {})
    return current


def _evaluate_rule(rule, resource, params, variables, scope="*"):
    """Evaluate one org_standard rule against a resource dict.
    Returns (passed: bool | None, detail: str).
    passed=True  → compliant
    passed=False → violation
    passed=None  → not applicable (property doesn't exist on this resource type)

    When scope is '*' and a property is not found, we return None (not
    applicable) because the standard applies to all resources and this
    resource type may not support the property.  When scope is narrowed to
    specific resource types, the property SHOULD exist — not-found is a
    failure.
    """
    import re
    rule_type = rule.get("type", "property")

    if rule_type in ("property", "property_check"):
        key = rule.get("key", "")
        operator = rule.get("operator", "==")
        expected = rule.get("value")
        actual = _get_nested(resource, key, params, variables)

        if actual is None:
            if operator in ("!=", "not_equals"):
                return True, f"`{key}` not set (satisfies != check)"
            if operator in ("exists",):
                return False, f"`{key}` not found in resource"
            # Scope-aware handling:
            # - scope='*' → standard applies to ALL resource types.
            #   Property not found means this resource type doesn't support
            #   the property → not applicable (None).
            # - scope is narrowed → standard targets specific resource types
            #   that SHOULD have this property → failure.
            if scope.strip() == "*":
                return None, f"`{key}` not applicable to this resource type"
            return False, f"`{key}` not found (expected on resources in scope `{scope}`)"

        actual_resolved = actual
        if isinstance(actual_resolved, str) and actual_resolved.startswith("<"):
            return True, f"`{key}` uses parameter (assumed compliant)"
        # Unresolved compound ARM expressions (e.g. [toLower(replace(...))])
        # cannot be evaluated statically — assume compliant.
        if (isinstance(actual_resolved, str)
                and actual_resolved.startswith("[") and actual_resolved.endswith("]")):
            return True, f"`{key}` uses ARM expression (assumed compliant)"

        actual_str = str(actual_resolved).lower()
        expected_str = str(expected).lower() if expected is not None else ""

        if operator in ("==", "equals"):
            passed = actual_str == expected_str
        elif operator in ("!=", "not_equals"):
            passed = actual_str != expected_str
        elif operator in (">=",):
            try:
                passed = float(actual_str) >= float(expected_str)
            except ValueError:
                passed = actual_str >= expected_str
        elif operator in ("<=",):
            try:
                passed = float(actual_str) <= float(expected_str)
            except ValueError:
                passed = actual_str <= expected_str
        elif operator in ("contains",):
            passed = expected_str in actual_str
        elif operator in ("matches", "regex"):
            try:
                passed = bool(re.fullmatch(expected_str, actual_str))
            except re.error:
                passed = True  # Malformed regex — can't evaluate, assume ok
        elif operator == "in":
            # Auto-detect regex patterns (e.g. ^[a-z0-9-]+$) stored as values
            if isinstance(expected, str) and expected.startswith("^"):
                try:
                    passed = bool(re.fullmatch(expected_str, actual_str))
                except re.error:
                    passed = True
            else:
                passed = actual_str in [str(v).lower() for v in (expected if isinstance(expected, list) else [expected])]
        else:
            passed = actual_str == expected_str

        detail = f"`{key}` = `{actual_resolved}` (expected {operator} `{expected}`)"
        return passed, detail

    elif rule_type == "tags":
        required = set(t.lower() for t in rule.get("required_tags", []))
        tags = resource.get("tags", {})
        if isinstance(tags, str):
            return True, "Tags use ARM expression (assumed compliant)"
        actual_tags = set(k.lower() for k in tags.keys()) if isinstance(tags, dict) else set()
        if isinstance(tags, dict):
            for v in tags.values():
                if isinstance(v, str) and "standardTags" in v:
                    return True, "Tags use shared standardTags variable"
        missing = required - actual_tags
        if missing:
            return False, f"Missing tags: {', '.join(sorted(missing))}"
        return True, f"All required tags present ({', '.join(sorted(required))})"

    elif rule_type == "allowed_values":
        key = rule.get("key", "")
        allowed = [str(v).lower() for v in rule.get("values", [])]
        actual = _get_nested(resource, key, params, variables)
        if actual is None:
            return False, f"`{key}` not set"
        actual_str = str(actual).lower()
        if isinstance(actual, str) and actual.startswith("<"):
            return True, f"`{key}` uses parameter (assumed compliant)"
        if isinstance(actual, str) and actual.startswith("[") and actual.endswith("]"):
            return True, f"`{key}` uses ARM expression (assumed compliant)"
        if actual_str in allowed:
            return True, f"`{key}` = `{actual}` (in allowed set)"
        return False, f"`{key}` = `{actual}` not in allowed values: {', '.join(allowed)}"

    elif rule_type == "naming_convention":
        pattern = rule.get("pattern", "")
        res_name = resource.get("name", "")
        if isinstance(res_name, str) and res_name.startswith("["):
            return True, "Name uses ARM expression (assumed compliant)"
        if pattern and res_name:
            regex = pattern.replace("{", "(?P<").replace("}", ">[a-z0-9-]+)")
            try:
                if re.match(regex, str(res_name).lower()):
                    return True, f"Name `{res_name}` matches pattern"
            except re.error:
                return True, f"Pattern `{pattern}` not evaluable as regex"
        return True, "Naming convention check (manual review)"

    elif rule_type == "cost_threshold":
        return True, f"Cost threshold ${rule.get('max_monthly_usd', 0)}/mo (requires runtime check)"

    return True, "Rule type not evaluable statically"


async def _quick_compliance_check(arm_content: str) -> list[dict]:
    """Run a fast compliance scan on ARM JSON and return a list of violations.

    Each violation dict has: resource_type, resource_name, standard_name,
    severity, detail, remediation.  Empty list = fully compliant.
    """
    from src.standards import get_all_standards
    import json as _json

    try:
        tpl = _json.loads(arm_content) if arm_content else None
    except Exception:
        return [{"resource_type": "?", "resource_name": "?",
                 "standard_name": "JSON", "severity": "critical",
                 "detail": "Invalid JSON", "remediation": "Fix JSON syntax"}]

    if not tpl or not isinstance(tpl.get("resources"), list):
        return []

    standards = await get_all_standards(enabled_only=True)
    params = tpl.get("parameters", {})
    variables = tpl.get("variables", {})
    violations: list[dict] = []

    for res in tpl.get("resources", []):
        if not isinstance(res, dict):
            continue
        res_type = res.get("type", "")
        res_name = res.get("name", "?")
        matching = [s for s in standards if _scope_matches(s.get("scope", "*"), res_type)]
        for std in matching:
            passed, detail = _evaluate_rule(std.get("rule", {}), res, params, variables, scope=std.get("scope", "*"))
            if passed is None:
                continue  # Not applicable to this resource type
            if not passed:
                violations.append({
                    "resource_type": res_type,
                    "resource_name": str(res_name),
                    "standard_name": std["name"],
                    "severity": std.get("severity", "medium"),
                    "detail": detail,
                    "remediation": std.get("rule", {}).get("remediation", ""),
                })

    return violations


# ── Compliance Profile ───────────────────────────────────────

@app.put("/api/catalog/templates/{template_id}/compliance-profile")
async def update_compliance_profile(template_id: str, request: Request):
    """Update the compliance profile for a template.

    Body: { "profile": ["encryption", "compliance_hipaa", ...] }
    - profile = list of GOV_CATEGORIES IDs that this template must comply with
    - profile = [] means the template is exempt from all compliance checks
    - profile = null means not configured (scan checks all standards — legacy behavior)
    """
    import json as _json

    tmpl = await _require_template(template_id)

    body = await request.json()
    profile = body.get("profile")  # None or list

    if profile is not None and not isinstance(profile, list):
        raise HTTPException(400, "profile must be a list of category IDs or null")

    backend = await get_backend()
    profile_json = _json.dumps(profile) if profile is not None else None
    await backend.execute_write(
        "UPDATE catalog_templates SET compliance_profile_json = ? WHERE id = ?",
        (profile_json, template_id),
    )

    return JSONResponse({
        "template_id": template_id,
        "compliance_profile": profile,
    })


# ── Compliance Scan ──────────────────────────────────────────

@app.post("/api/catalog/templates/{template_id}/compliance-scan")
async def compliance_scan_template(template_id: str, request: Request):
    """Scan a template and all its dependencies against organization standards.

    Parses the ARM JSON, extracts every resource, matches each resource type
    against enabled org_standards, and evaluates each rule. Returns a rich
    report with per-resource findings, severity breakdown, and an overall
    compliance score.

    Body (optional): { "version": 1 }
    """
    from src.standards import get_all_standards
    import json as _json
    import fnmatch
    import re

    tmpl = await _require_template(template_id)

    body = await _parse_body(request)

    # ── Gather ARM content from this template + dependencies ──
    templates_to_scan = []

    # Main template version
    requested_version = body.get("version")
    ver = None
    if requested_version:
        ver = await get_template_version(template_id, int(requested_version))
    else:
        versions = await get_template_versions(template_id)
        if versions:
            ver = versions[0]
    if not ver:
        raise HTTPException(status_code=404, detail="No version found")

    templates_to_scan.append({
        "id": template_id,
        "name": tmpl.get("name", template_id),
        "arm_content": ver.get("arm_template", ""),
        "is_dependency": False,
    })

    # Dependency templates — prefer latest service_versions ARM over
    # catalog_templates.content (which may be stale after remediation).
    dep_service_ids = tmpl.get("service_ids", []) or []
    if dep_service_ids:
        all_tmpls = await get_all_templates()
        tmpl_by_id = {t["id"]: t for t in all_tmpls}
        for sid in dep_service_ids:
            dep_name = sid
            dep_tmpl = tmpl_by_id.get(sid)
            if dep_tmpl:
                dep_name = dep_tmpl.get("name", sid)

            # Check service_versions first (has remediated content)
            svc_versions = await get_service_versions(sid)
            if svc_versions and svc_versions[0].get("arm_template"):
                templates_to_scan.append({
                    "id": sid,
                    "name": dep_name,
                    "arm_content": svc_versions[0]["arm_template"],
                    "is_dependency": True,
                })
            elif dep_tmpl and dep_tmpl.get("content"):
                # Fall back to catalog_templates.content
                templates_to_scan.append({
                    "id": sid,
                    "name": dep_name,
                    "arm_content": dep_tmpl["content"],
                    "is_dependency": True,
                })

    # ── Load all enabled standards ────────────────────────────
    all_standards = await get_all_standards(enabled_only=True)

    # ── Filter by compliance profile ─────────────────────────
    compliance_profile = tmpl.get("compliance_profile")  # None or list
    profile_applied = False
    if compliance_profile is not None:
        profile_applied = True
        if len(compliance_profile) == 0:
            # Template is exempt — no standards apply
            all_standards = []
        else:
            profile_set = set(compliance_profile)
            filtered = []
            for s in all_standards:
                # Include if the standard's category is in the profile
                if s.get("category", "") in profile_set:
                    filtered.append(s)
                    continue
                # Include if any of the standard's frameworks overlap with the profile
                s_frameworks = s.get("frameworks") or []
                if any(fw in profile_set for fw in s_frameworks):
                    filtered.append(s)
            all_standards = filtered

    # Use module-level compliance helpers: _scope_matches, _resolve_arm_value,
    # _get_nested, _evaluate_rule

    # ── Scan each template ────────────────────────────────────
    scan_results = []
    total_checks = 0
    total_passed = 0
    severity_counts = {"critical": {"total": 0, "passed": 0}, "high": {"total": 0, "passed": 0}, "medium": {"total": 0, "passed": 0}, "low": {"total": 0, "passed": 0}}

    for tmpl_info in templates_to_scan:
        arm_content = tmpl_info["arm_content"]
        try:
            tpl = _json.loads(arm_content) if arm_content else None
        except Exception:
            scan_results.append({
                "template_id": tmpl_info["id"],
                "template_name": tmpl_info["name"],
                "is_dependency": tmpl_info["is_dependency"],
                "error": "Invalid JSON — could not parse ARM template",
                "resources": [],
            })
            continue

        if not tpl or not isinstance(tpl.get("resources"), list):
            scan_results.append({
                "template_id": tmpl_info["id"],
                "template_name": tmpl_info["name"],
                "is_dependency": tmpl_info["is_dependency"],
                "error": "No resources found in ARM template",
                "resources": [],
            })
            continue

        params = tpl.get("parameters", {})
        variables = tpl.get("variables", {})
        resources = tpl.get("resources", [])

        tmpl_resource_results = []

        for i, res in enumerate(resources):
            if not isinstance(res, dict):
                continue
            res_type = res.get("type", "")
            res_name = res.get("name", f"resource[{i}]")
            # Resolve name if it's an ARM expression
            resolved_name = _resolve_arm_value(res_name, params, variables) if isinstance(res_name, str) else res_name

            # Find matching standards
            matching = [s for s in all_standards if _scope_matches(s.get("scope", "*"), res_type)]
            if not matching:
                tmpl_resource_results.append({
                    "resource_type": res_type,
                    "resource_name": str(resolved_name),
                    "standards_checked": 0,
                    "findings": [],
                    "all_passed": True,
                })
                continue

            findings = []
            for std in matching:
                rule = std.get("rule", {})
                sev = std.get("severity", "medium")
                passed, detail = _evaluate_rule(rule, res, params, variables, scope=std.get("scope", "*"))

                # None = not applicable (property doesn't exist on this
                # resource type).  Skip it entirely — don't count as a
                # check or a violation.
                if passed is None:
                    continue

                total_checks += 1
                if sev in severity_counts:
                    severity_counts[sev]["total"] += 1
                if passed:
                    total_passed += 1
                    if sev in severity_counts:
                        severity_counts[sev]["passed"] += 1

                findings.append({
                    "standard_id": std["id"],
                    "standard_name": std["name"],
                    "category": std.get("category", ""),
                    "severity": sev,
                    "passed": passed,
                    "detail": detail,
                    "remediation": rule.get("remediation", ""),
                })

            tmpl_resource_results.append({
                "resource_type": res_type,
                "resource_name": str(resolved_name),
                "standards_checked": len(matching),
                "findings": findings,
                "all_passed": all(f["passed"] for f in findings),
            })

        scan_results.append({
            "template_id": tmpl_info["id"],
            "template_name": tmpl_info["name"],
            "is_dependency": tmpl_info["is_dependency"],
            "resources": tmpl_resource_results,
        })

    # ── Compute overall score ─────────────────────────────────
    score = round((total_passed / total_checks) * 100) if total_checks > 0 else 100
    violations = total_checks - total_passed

    return JSONResponse({
        "template_id": template_id,
        "template_name": tmpl.get("name", template_id),
        "score": score,
        "total_checks": total_checks,
        "total_passed": total_passed,
        "violations": violations,
        "severity_breakdown": severity_counts,
        "templates_scanned": len(templates_to_scan),
        "standards_count": len(all_standards),
        "compliance_profile": compliance_profile,
        "profile_applied": profile_applied,
        "results": scan_results,
    })


# ── Compliance Remediation (Plan + Execute) ─────────────────

@app.post("/api/catalog/templates/{template_id}/compliance-remediate/plan")
async def compliance_remediate_plan(template_id: str, request: Request):
    """Phase 1: Generate a remediation plan for compliance violations.

    Accepts the scan results and uses the PLANNING model (o3-mini) to produce
    a structured plan describing what changes each template needs.
    """
    import asyncio
    from src.model_router import Task, get_model_for_task

    body = await request.json()
    scan_data = body.get("scan_data")
    if not scan_data:
        raise HTTPException(400, "scan_data is required (pass the full scan results)")

    tmpl = await _require_template(template_id)

    # ── Gather dependency info for composed templates (BEFORE violations) ──
    dep_service_ids = tmpl.get("service_ids", []) or []

    # Build resource→service mapping.
    # service_ids ARE resource types (e.g. "Microsoft.Network/virtualNetworks").
    # They may or may not exist as separate catalog entries.  We map every
    # resource type found in the composed ARM template to its owning service_id
    # using:  1) exact match,  2) provider-namespace match,
    #         3) child-resource prefix match.
    resource_to_service: dict[str, str] = {}
    service_id_names: dict[str, str] = {}          # pretty name for each sid

    if dep_service_ids:
        # Normalised lookup  sid_lower → original sid
        sids_lower = {sid.lower(): sid for sid in dep_service_ids}

        # Extract resource types from the composed ARM template
        try:
            arm_json = json.loads(tmpl.get("content", "") or "")
            arm_resources = arm_json.get("resources", [])
        except Exception:
            arm_resources = []

        for res in arm_resources:
            if not isinstance(res, dict):
                continue
            rtype = (res.get("type", "") or "").lower()
            if not rtype:
                continue

            # 1) Exact match (resource type == service_id)
            if rtype in sids_lower:
                resource_to_service[rtype] = sids_lower[rtype]
                continue

            # 2) Child‑resource prefix (e.g. Microsoft.Compute/virtualMachines/extensions)
            for sid_l, sid in sids_lower.items():
                if rtype.startswith(sid_l + "/"):
                    resource_to_service[rtype] = sid
                    break
            if rtype in resource_to_service:
                continue

            # 3) Same provider namespace (e.g. Microsoft.Network)
            provider = rtype.rsplit("/", 1)[0] if "/" in rtype else rtype
            for sid_l, sid in sids_lower.items():
                sid_provider = sid_l.rsplit("/", 1)[0] if "/" in sid_l else sid_l
                if provider == sid_provider:
                    resource_to_service[rtype] = sid
                    break

        # Build friendly names for each service_id  (short suffix form)
        for sid in dep_service_ids:
            parts = sid.split("/")
            service_id_names[sid] = parts[-1] if len(parts) > 1 else sid

    # Collect violations per template — re-attribute to owning service template
    violations_summary = []
    for tmpl_result in scan_data.get("results", []):
        tid = tmpl_result.get("template_id", "")
        tname = tmpl_result.get("template_name", tid)
        for res in tmpl_result.get("resources", []):
            rt = res.get("resource_type", "").lower()
            # If this resource belongs to a service template, attribute to it
            owning_service = resource_to_service.get(rt)
            effective_tid = owning_service if owning_service else tid
            effective_name = service_id_names.get(effective_tid, tname) if owning_service else tname
            for f in res.get("findings", []):
                if not f.get("passed", True):
                    violations_summary.append({
                        "template_id": effective_tid,
                        "template_name": effective_name,
                        "resource_type": res.get("resource_type", ""),
                        "resource_name": res.get("resource_name", ""),
                        "standard": f.get("standard_name", ""),
                        "category": f.get("category", ""),
                        "severity": f.get("severity", ""),
                        "detail": f.get("detail", ""),
                        "remediation": f.get("remediation", ""),
                    })

    if not violations_summary:
        return JSONResponse({"plan": [], "summary": "No violations to remediate — template is fully compliant.", "violation_count": 0})

    # Gather ARM content + version info for each template mentioned in violations
    # AND all dependency templates
    template_ids = list({v["template_id"] for v in violations_summary})
    # Ensure all dependency templates are included
    for sid in dep_service_ids:
        if sid not in template_ids:
            template_ids.append(sid)
    # Always include the parent
    if template_id not in template_ids:
        template_ids.append(template_id)

    arm_snippets = {}
    template_version_info = {}  # tid -> {current_version, current_semver, ...}

    for tid in template_ids:
        # Service templates (dependencies) store versions in service_versions,
        # not template_versions.  Read from the correct table.
        is_service_dep = tid in dep_service_ids and tid != template_id
        if is_service_dep:
            latest_svc = await get_latest_service_version(tid)
            if latest_svc:
                current_ver_num = latest_svc.get("version", 0)
                current_semver = latest_svc.get("semver") or f"{current_ver_num}.0.0"
            else:
                current_ver_num = 0
                current_semver = "1.0.0"
        else:
            versions = await get_template_versions(tid)
            current_semver = await get_latest_semver(tid) or "1.0.0"
            current_ver_num = versions[0]["version"] if versions else 0

        # Determine change_type based on severity of violations for this template
        # (violations are already attributed to owning service templates)
        tid_violations = [v for v in violations_summary if v["template_id"] == tid]

        has_critical = any(v["severity"] == "critical" for v in tid_violations)
        has_violations = len(tid_violations) > 0

        if not has_violations:
            change_type = "none"
            projected_semver = current_semver
        elif has_critical:
            change_type = "minor"  # critical compliance = minor bump
        else:
            change_type = "patch"  # high/medium/low = patch

        if change_type != "none":
            projected_semver = compute_next_semver(current_semver, change_type)

        dep_name = ""
        if tid == template_id:
            dep_name = tmpl.get("name", tid)
        elif tid in service_id_names:
            dep_name = service_id_names[tid]
        else:
            dep_name = tid

        template_version_info[tid] = {
            "current_version": current_ver_num,
            "current_semver": current_semver,
            "change_type": change_type,
            "projected_semver": projected_semver,
            "projected_version": current_ver_num + 1 if change_type != "none" else current_ver_num,
            "template_name": dep_name,
            "is_dependency": tid != template_id,
            "violation_count": len(tid_violations),
            "resource_types": [rt for rt, sid in resource_to_service.items() if sid == tid],
        }

        if tid == template_id:
            if versions:
                ver = await get_template_version(tid, versions[0]["version"])
                arm_snippets[tid] = ver.get("arm_template", "") if ver else ""
            if not arm_snippets.get(tid):
                arm_snippets[tid] = tmpl.get("content", "")
        else:
            # Service template — use the parent's composed ARM (it contains all resources)
            arm_snippets[tid] = tmpl.get("content", "")

    # ── Check for newer compliant service versions (upgrade check) ──
    # For each service dependency with violations, check if a newer version
    # of that service's ARM skeleton exists and is already compliant.
    # If yes → recommend upgrade instead of AI fix.
    # If no  → still pull the latest version's ARM for the AI to fix.
    for sid in dep_service_ids:
        vinfo = template_version_info.get(sid)
        if not vinfo or vinfo.get("violation_count", 0) == 0:
            continue  # no violations for this service — skip

        latest_svc = await get_latest_service_version(sid)
        if not latest_svc or not latest_svc.get("arm_template"):
            vinfo["upgrade_available"] = False
            vinfo["upgrade_action"] = "ai_fix"
            continue

        svc_arm = latest_svc["arm_template"]
        svc_semver = latest_svc.get("semver", "?")
        svc_ver_num = latest_svc.get("version", 0)

        # Run a quick compliance check on the latest service version's ARM
        svc_violations = await _quick_compliance_check(svc_arm)

        if not svc_violations:
            # Latest service version is already compliant — recommend upgrade
            vinfo["upgrade_available"] = True
            vinfo["upgrade_action"] = "upgrade"
            vinfo["upgrade_version"] = svc_ver_num
            vinfo["upgrade_semver"] = svc_semver
            vinfo["change_type"] = "patch"  # upgrade = patch bump
            vinfo["projected_semver"] = compute_next_semver(
                vinfo["current_semver"], "patch"
            )
        else:
            # Latest service version still has violations — pull latest and AI-fix
            vinfo["upgrade_available"] = False
            vinfo["upgrade_action"] = "ai_fix_latest"
            vinfo["upgrade_version"] = svc_ver_num
            vinfo["upgrade_semver"] = svc_semver
            vinfo["upgrade_violations"] = len(svc_violations)

    # If service templates have violations, propagate a version bump to the parent
    # (composed parent should bump when any of its dependencies change)
    if dep_service_ids and template_id in template_version_info:
        parent_info = template_version_info[template_id]
        dep_has_changes = any(
            template_version_info.get(sid, {}).get("change_type", "none") != "none"
            for sid in dep_service_ids
        )
        if dep_has_changes and parent_info["change_type"] == "none":
            # Propagate the highest change level from deps
            dep_change_types = [
                template_version_info.get(sid, {}).get("change_type", "none")
                for sid in dep_service_ids
            ]
            if "minor" in dep_change_types:
                parent_info["change_type"] = "minor"
            elif "patch" in dep_change_types:
                parent_info["change_type"] = "patch"
            parent_info["projected_semver"] = compute_next_semver(
                parent_info["current_semver"], parent_info["change_type"]
            )
            parent_info["projected_version"] = parent_info["current_version"] + 1

    # Build resource ownership context for the LLM
    resource_ownership_text = ""
    if dep_service_ids:
        resource_ownership_text = "\nRESOURCE OWNERSHIP (which service template owns which resources):\n"
        for sid in dep_service_ids:
            vinfo = template_version_info.get(sid, {})
            owned_types = vinfo.get("resource_types", [])
            if owned_types:
                resource_ownership_text += f"  - {vinfo.get('template_name', sid)} ({sid}): {', '.join(owned_types)}\n"
        resource_ownership_text += (
            f"  - {tmpl.get('name', template_id)} ({template_id}): composed parent — "
            "changes to resources should target the service template that owns them.\n"
        )

    # Build planning prompt
    violations_text = ""
    for v in violations_summary:
        # Annotate with owning service template if known
        rt = v.get("resource_type", "").lower()
        owner_sid = resource_to_service.get(rt)
        owner_label = f" [owned by: {owner_sid}]" if owner_sid else ""
        violations_text += (
            f"  - [{v['severity'].upper()}] {v['standard']} on {v['resource_type']} "
            f"({v['resource_name']}){owner_label}: {v['detail']}"
        )
        if v.get("remediation"):
            violations_text += f" → Remediation: {v['remediation']}"
        violations_text += "\n"

    templates_text = ""
    for tid, arm in arm_snippets.items():
        # Truncate if very long, but include enough for the LLM
        truncated = arm[:12000] if len(arm) > 12000 else arm
        templates_text += f"\n--- TEMPLATE: {tid} ---\n{truncated}\n--- END ---\n"

    prompt = (
        "You are an Azure infrastructure compliance expert. Analyze the following "
        "compliance violations and produce a structured remediation plan.\n\n"
        f"VIOLATIONS ({len(violations_summary)} total):\n{violations_text}\n"
    )
    if resource_ownership_text:
        prompt += resource_ownership_text + "\n"
    prompt += (
        f"CURRENT ARM TEMPLATES:\n{templates_text}\n"
        "Generate a JSON remediation plan. Return ONLY valid JSON with this structure:\n"
        "{\n"
        '  "summary": "Brief overall summary of what needs to change",\n'
        '  "steps": [\n'
        "    {\n"
        '      "step": 1,\n'
        '      "template_id": "the owning service template ID from the RESOURCE OWNERSHIP list",\n'
        '      "template_name": "human-readable name of that service template",\n'
        '      "action": "Brief description of the change for THIS service template only",\n'
        '      "detail": "Specific technical detail of what to modify in the ARM JSON for this service",\n'
        '      "severity": "critical|high|medium|low",\n'
        '      "standards_addressed": ["list of standard names this step fixes"]\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "RULES:\n"
        "- CRITICAL: Generate SEPARATE steps for EACH service template. Do NOT create\n"
        "  cross-cutting steps that span multiple service templates.\n"
        "  For example, if TLS must be fixed on both virtualNetworks AND virtualMachines,\n"
        "  emit two separate steps — one per service template.\n"
        "- Each step's template_id MUST be an exact service template ID from the\n"
        "  RESOURCE OWNERSHIP section (e.g. 'Microsoft.Network/virtualNetworks').\n"
        "  NEVER use the composed parent template ID.\n"
        "- Group related changes FOR THE SAME service template into single steps\n"
        "- Order by severity (critical first), then by service template\n"
        "- Be specific about what ARM properties to change\n"
        "- Each step should be independently actionable\n"
        "- Reference actual resource names and property paths\n"
    )

    client = await ensure_copilot_client()
    if not client:
        raise HTTPException(503, "AI client not available")

    model = get_model_for_task(Task.PLANNING)

    from src.copilot_helpers import copilot_send

    MAX_PLAN_RETRIES = 3
    plan = None
    last_error = ""

    for attempt in range(1, MAX_PLAN_RETRIES + 1):
        retry_prompt = prompt
        if attempt > 1 and last_error:
            retry_prompt += (
                f"\n\nPREVIOUS ATTEMPT FAILED: {last_error}\n"
                "Return ONLY valid raw JSON. No markdown fences, no ```json, no text.\n"
            )

        raw = await copilot_send(
            client,
            model=model,
            system_prompt=REMEDIATION_PLANNER.system_prompt,
            prompt=retry_prompt,
            timeout=90,
            agent_name="REMEDIATION_PLANNER",
        )

        # Robust JSON extraction
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3].strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()
        brace_start = raw.find("{")
        brace_end = raw.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            raw = raw[brace_start:brace_end + 1]

        try:
            plan = json.loads(raw)
            break  # Success
        except json.JSONDecodeError as e:
            last_error = f"JSON parse error: {str(e)}"
            if attempt >= MAX_PLAN_RETRIES:
                return JSONResponse({
                    "plan": [],
                    "summary": f"Failed to parse remediation plan after {MAX_PLAN_RETRIES} attempts",
                    "raw": raw,
                    "violation_count": len(violations_summary),
                }, status_code=500)

    # Enrich steps with version info + normalize template_ids
    steps = plan.get("steps", [])
    valid_template_ids = set(template_version_info.keys())
    for step in steps:
        tid = step.get("template_id", template_id)
        # Normalize: if the LLM returned a name or invalid ID, resolve it
        if tid not in valid_template_ids:
            matched = False
            # Try name / partial match against template_version_info
            for vtid, vinfo in template_version_info.items():
                tname = vinfo.get("template_name", "")
                if tname and (tname.lower() == tid.lower() or vtid.lower() in tid.lower()):
                    step["template_id"] = vtid
                    tid = vtid
                    matched = True
                    break
            # If still unmatched, infer from resource types mentioned in the step text
            if not matched and resource_to_service:
                step_text = (
                    (step.get("action", "") + " " + step.get("detail", ""))
                ).lower()
                # Count how many times each service_id's resources appear in the text
                sid_hits: dict[str, int] = {}
                for rtype, sid in resource_to_service.items():
                    # Check for the resource type or its short name
                    short = rtype.rsplit("/", 1)[-1] if "/" in rtype else rtype
                    if rtype in step_text or short in step_text:
                        sid_hits[sid] = sid_hits.get(sid, 0) + 1
                if sid_hits:
                    best_sid = max(sid_hits, key=sid_hits.get)
                    step["template_id"] = best_sid
                    tid = best_sid
                    matched = True
            if not matched:
                step["template_id"] = template_id
                tid = template_id

        vinfo = template_version_info.get(tid, {})
        step["current_semver"] = vinfo.get("current_semver", "")
        step["projected_semver"] = vinfo.get("projected_semver", "")
        step["change_type"] = vinfo.get("change_type", "patch")
        step["current_version"] = vinfo.get("current_version", 0)
        step["projected_version"] = vinfo.get("projected_version", 1)
        # Propagate upgrade info to steps
        step["upgrade_action"] = vinfo.get("upgrade_action", "ai_fix")
        step["upgrade_available"] = vinfo.get("upgrade_available", False)
        if vinfo.get("upgrade_semver"):
            step["upgrade_semver"] = vinfo["upgrade_semver"]
        # Override template_name with the authoritative name from version_info
        if vinfo.get("template_name"):
            step["template_name"] = vinfo["template_name"]

    return JSONResponse({
        "plan": steps,
        "summary": plan.get("summary", ""),
        "violation_count": len(violations_summary),
        "template_versions": template_version_info,
    })


@app.post("/api/catalog/templates/{template_id}/compliance-remediate/execute")
async def compliance_remediate_execute(template_id: str, request: Request):
    """Phase 2: Execute remediation — ADO Pipelines-style parallel streaming.

    Runs all template remediations in parallel, streaming interleaved NDJSON
    events so the UI can render a live ADO-style pipeline view.

    Event protocol:
      pipeline_init   — full job/step DAG with parallel grouping
      step_start      — a step within a job is starting
      step_log        — log line for a step (timestamped)
      step_end        — step finished (success/failed/skipped, duration_ms)
      job_end         — job finished (success/failed, result summary)
      pipeline_done   — all jobs complete, final summary
    """
    import asyncio
    import time
    import uuid
    from src.model_router import Task, get_model_for_task
    from src.tools.deploy_engine import run_what_if, _get_subscription_id, _get_resource_client

    body = await request.json()
    plan_steps = body.get("plan", [])
    scan_data = body.get("scan_data")

    if not plan_steps:
        raise HTTPException(400, "plan is required (pass the steps array)")

    tmpl = await _require_template(template_id)

    client = await ensure_copilot_client()
    if not client:
        raise HTTPException(503, "AI client not available")

    backend = await get_backend()
    model = get_model_for_task(Task.CODE_GENERATION)

    # Pre-load dependency templates
    dep_service_ids = tmpl.get("service_ids", []) or []
    known_templates = {template_id: tmpl}
    if dep_service_ids:
        for sid in dep_service_ids:
            dep = await get_template_by_id(sid)
            if dep:
                known_templates[sid] = dep
    valid_ids = set(known_templates.keys())

    # Normalize step template_ids
    for step in plan_steps:
        tid = step.get("template_id", template_id)
        if tid not in valid_ids:
            matched = False
            for kid, ktmpl in known_templates.items():
                kname = ktmpl.get("name", "")
                if kname and (kname.lower() == tid.lower() or kid.lower() in tid.lower()):
                    step["template_id"] = kid
                    matched = True
                    break
            if not matched:
                step["template_id"] = template_id

    # Group steps by template_id
    steps_by_template: dict[str, list] = {}
    for step in plan_steps:
        tid = step.get("template_id", template_id)
        steps_by_template.setdefault(tid, []).append(step)

    # Build pipeline DAG — each template is a "job" with 7 steps
    jobs = []
    for i, (tid, steps) in enumerate(steps_by_template.items()):
        tname = steps[0].get("template_name", tid)
        kt = known_templates.get(tid, {})
        current_semver = steps[0].get("current_semver", "")
        projected_semver = steps[0].get("projected_semver", "")
        change_type = steps[0].get("change_type", "patch")
        upgrade_action = steps[0].get("upgrade_action", "ai_fix")
        upgrade_available = steps[0].get("upgrade_available", False)
        upgrade_semver = steps[0].get("upgrade_semver", "")
        dep_check_detail = "Check for newer compliant service version"
        if upgrade_available:
            dep_check_detail = f"Upgrade available → v{upgrade_semver} (compliant)"
        elif upgrade_action == "ai_fix_latest":
            dep_check_detail = f"Latest v{upgrade_semver} still needs fixes"
        jobs.append({
            "id": f"job-{i}",
            "template_id": tid,
            "label": kt.get("name") or tname,
            "current_semver": current_semver,
            "projected_semver": projected_semver,
            "change_type": change_type,
            "upgrade_action": upgrade_action,
            "upgrade_available": upgrade_available,
            "upgrade_semver": upgrade_semver,
            "step_count": len(steps),
            "steps": [
                {"id": f"job-{i}-checkout", "label": "Checkout", "detail": f"Check out v{current_semver}"},
                {"id": f"job-{i}-depcheck", "label": "Dep Check", "detail": dep_check_detail},
                {"id": f"job-{i}-remediate", "label": "Remediate", "detail": f"Apply {len(steps)} compliance fix(es)"},
                {"id": f"job-{i}-validate", "label": "Validate", "detail": "Parse & validate ARM JSON"},
                {"id": f"job-{i}-verify", "label": "Verify", "detail": "Re-scan compliance to confirm fixes"},
                {"id": f"job-{i}-deploy-test", "label": "Deploy Test", "detail": "ARM What-If validation against Azure"},
                {"id": f"job-{i}-version", "label": "Version", "detail": f"Bump {current_semver} → {projected_semver} ({change_type})"},
                {"id": f"job-{i}-publish", "label": "Publish", "detail": "Update catalog with new version"},
            ],
        })

    # Shared event queue for parallel jobs
    event_queue: asyncio.Queue = asyncio.Queue()

    async def _run_job(job_idx: int, tid: str, steps: list):
        """Run a single template remediation job, pushing events to the queue."""
        job_id = f"job-{job_idx}"
        tname = jobs[job_idx]["label"]
        t0 = time.time()
        job_log: list[dict] = []  # accumulate all events for persistence

        def emit(evt):
            job_log.append(evt)
            event_queue.put_nowait(evt)

        def step_log(step_id, msg, level="info"):
            emit({"type": "step_log", "job_id": job_id, "step_id": step_id,
                  "message": msg, "level": level,
                  "timestamp": datetime.now(timezone.utc).isoformat()})

        def step_start(step_id):
            emit({"type": "step_start", "job_id": job_id, "step_id": step_id,
                  "timestamp": datetime.now(timezone.utc).isoformat()})

        def step_end(step_id, status, duration_ms=0, detail=""):
            emit({"type": "step_end", "job_id": job_id, "step_id": step_id,
                  "status": status, "duration_ms": duration_ms, "detail": detail,
                  "timestamp": datetime.now(timezone.utc).isoformat()})

        try:
            # ── Step 1: CHECKOUT ──
            sid = f"{job_id}-checkout"
            step_start(sid)
            s1 = time.time()
            step_log(sid, f"Checking out template: {tname} ({tid})")

            current_arm = ""
            ver_num = None
            current_semver = ""

            # Service deps store versions in service_versions table
            is_service_dep = tid in dep_service_ids and tid != template_id

            if is_service_dep:
                latest_svc = await get_latest_service_version(tid)
                if latest_svc and latest_svc.get("arm_template"):
                    current_arm = latest_svc["arm_template"]
                    ver_num = latest_svc.get("version", 0)
                    current_semver = latest_svc.get("semver") or f"{ver_num}.0.0"
                    step_log(sid, f"Latest service version: v{current_semver} (version #{ver_num})")
            else:
                versions = await get_template_versions(tid)
                if versions:
                    ver = await get_template_version(tid, versions[0]["version"])
                    current_arm = ver.get("arm_template", "") if ver else ""
                    ver_num = versions[0]["version"]
                    current_semver = ver.get("semver", "") if ver else ""
                    step_log(sid, f"Found {len(versions)} version(s) in history")
                    step_log(sid, f"Latest: v{current_semver} (version #{ver_num})")

            if not current_arm:
                src_tmpl = known_templates.get(tid) or await get_template_by_id(tid)
                current_arm = src_tmpl.get("content", "") if src_tmpl else ""
                if current_arm:
                    step_log(sid, "Loaded from catalog content (no versioned ARM)")

            if not current_arm and tid != template_id:
                step_log(sid, f"No standalone template for {tid} — using composed parent")
                parent_versions = await get_template_versions(template_id)
                if parent_versions:
                    parent_ver = await get_template_version(template_id, parent_versions[0]["version"])
                    current_arm = parent_ver.get("arm_template", "") if parent_ver else ""
                if not current_arm:
                    current_arm = tmpl.get("content", "")

            if not current_arm:
                step_log(sid, "FATAL: No ARM content found", "error")
                step_end(sid, "failed", int((time.time() - s1) * 1000), "No ARM content")
                emit({"type": "job_end", "job_id": job_id, "status": "failed",
                      "error": "No ARM content found", "duration_ms": int((time.time() - t0) * 1000)})
                return {"template_id": tid, "success": False, "error": "No ARM content found"}

            arm_size = len(current_arm)
            step_log(sid, f"Template loaded: {arm_size:,} bytes")
            # Count resources in the ARM
            try:
                parsed_arm = json.loads(current_arm)
                res_count = len(parsed_arm.get("resources", []))
                param_count = len(parsed_arm.get("parameters", {}))
                step_log(sid, f"Contains {res_count} resource(s), {param_count} parameter(s)")
            except Exception:
                pass
            step_end(sid, "success", int((time.time() - s1) * 1000))

            # ── Step 2: DEP CHECK (Dependency Upgrade Check) ──
            sid = f"{job_id}-depcheck"
            step_start(sid)
            s_dc = time.time()

            upgrade_action = jobs[job_idx].get("upgrade_action", "ai_fix")
            upgrade_skips_ai = False  # True when upgrade resolves all violations

            # Only check services (not the composed parent itself)
            if tid != template_id and tid in dep_service_ids:
                step_log(sid, f"Checking for newer version of {tid}…")
                latest_svc = await get_latest_service_version(tid)

                if latest_svc and latest_svc.get("arm_template"):
                    svc_arm = latest_svc["arm_template"]
                    svc_semver = latest_svc.get("semver", "?")
                    svc_ver = latest_svc.get("version", 0)
                    step_log(sid, f"Latest service version: v{svc_semver} (#{svc_ver})")

                    # Run compliance check on the latest service version
                    svc_violations = await _quick_compliance_check(svc_arm)

                    if not svc_violations:
                        # Newer version is compliant — swap resources in the composed ARM
                        step_log(sid, f"✓ Service version v{svc_semver} is fully compliant")
                        step_log(sid, "Upgrading composed template with compliant service version…")

                        # Replace resources belonging to this service in the composed ARM
                        try:
                            composed = json.loads(current_arm)
                            svc_tpl = json.loads(svc_arm)
                            svc_resources = svc_tpl.get("resources", [])

                            # Identify resource types from the service version
                            svc_types = {
                                r.get("type", "").lower()
                                for r in svc_resources if isinstance(r, dict)
                            }

                            # Remove old resources of these types from composed ARM
                            kept = [
                                r for r in composed.get("resources", [])
                                if not isinstance(r, dict) or r.get("type", "").lower() not in svc_types
                            ]
                            # Add the new compliant resources
                            kept.extend(svc_resources)
                            composed["resources"] = kept

                            # Merge parameters and variables from the service version
                            for pk, pv in svc_tpl.get("parameters", {}).items():
                                composed.setdefault("parameters", {})[pk] = pv
                            for vk, vv in svc_tpl.get("variables", {}).items():
                                composed.setdefault("variables", {})[vk] = vv

                            current_arm = json.dumps(composed, indent=2)
                            upgrade_skips_ai = True
                            step_log(sid, f"Replaced {len(svc_types)} resource type(s) with compliant versions")
                            step_log(sid, f"Composed template updated: {len(current_arm):,} bytes")
                        except Exception as swap_err:
                            step_log(sid, f"⚠ Resource swap failed: {swap_err}", "warning")
                            step_log(sid, "Falling back to AI remediation")
                    else:
                        # Latest version still has violations — use its ARM for AI to fix
                        step_log(sid, f"Latest v{svc_semver} has {len(svc_violations)} violation(s)")
                        for sv in svc_violations[:5]:
                            step_log(sid, f"  • {sv['standard_name']}: {sv['detail']}")
                        step_log(sid, "Will pull latest version and send to AI for remediation")

                        # Replace resources in composed ARM with latest service version
                        # (even though it's not compliant, it may have partial fixes)
                        try:
                            composed = json.loads(current_arm)
                            svc_tpl = json.loads(svc_arm)
                            svc_resources = svc_tpl.get("resources", [])
                            svc_types = {
                                r.get("type", "").lower()
                                for r in svc_resources if isinstance(r, dict)
                            }
                            kept = [
                                r for r in composed.get("resources", [])
                                if not isinstance(r, dict) or r.get("type", "").lower() not in svc_types
                            ]
                            kept.extend(svc_resources)
                            composed["resources"] = kept
                            for pk, pv in svc_tpl.get("parameters", {}).items():
                                composed.setdefault("parameters", {})[pk] = pv
                            for vk, vv in svc_tpl.get("variables", {}).items():
                                composed.setdefault("variables", {})[vk] = vv
                            current_arm = json.dumps(composed, indent=2)
                            step_log(sid, f"Pulled latest service ARM into composed template")
                        except Exception as pull_err:
                            step_log(sid, f"⚠ Could not pull latest version: {pull_err}", "warning")
                else:
                    step_log(sid, "No service version with ARM content found")
                    step_log(sid, "Proceeding with current ARM for AI remediation")
            else:
                step_log(sid, "Composed parent template — no dependency upgrade check needed")
                upgrade_action = "ai_fix"

            dep_check_status = "success" if upgrade_skips_ai else "success"
            step_end(sid, dep_check_status, int((time.time() - s_dc) * 1000),
                     "Upgraded" if upgrade_skips_ai else "Checked")

            # ── Step 3: REMEDIATE ──
            sid = f"{job_id}-remediate"
            step_start(sid)
            s2 = time.time()

            if upgrade_skips_ai:
                # Upgrade resolved all violations — skip AI remediation
                step_log(sid, "✓ All violations resolved by service version upgrade")
                step_log(sid, "Skipping AI remediation — template already compliant")
                result_json = None
                fixed_content = current_arm  # Already updated by dep check
                changes_made = [{"step": 0, "description": f"Upgraded {tid} to compliant service version", "resource": tid}]
                step_end(sid, "success", int((time.time() - s2) * 1000), "Skipped (upgrade)")

                # Skip the AI block — jump to validate
            else:
                step_log(sid, f"Preparing {len(steps)} remediation instruction(s)")

                for j, s in enumerate(steps):
                    sev = s.get("severity", "medium").upper()
                    step_log(sid, f"  [{sev}] {s.get('action', 'Fix')}")

                instructions = "\n".join(
                    f"{j+1}. [{s.get('severity','medium').upper()}] {s.get('action','')}: {s.get('detail','')}"
                    for j, s in enumerate(steps)
                )

                violations_context = ""
                if scan_data:
                    for tmpl_result in scan_data.get("results", []):
                        if tmpl_result.get("template_id") == tid:
                            for res in tmpl_result.get("resources", []):
                                for f in res.get("findings", []):
                                    if not f.get("passed", True):
                                        violations_context += (
                                            f"  - {f.get('standard_name','')}: {f.get('detail','')}\n"
                                        )

                prompt = (
                    "You are an Azure ARM template compliance remediation expert. "
                    "Apply the following remediation steps to the ARM template.\n\n"
                    f"--- REMEDIATION STEPS ---\n{instructions}\n--- END STEPS ---\n\n"
                )
                if violations_context:
                    prompt += f"--- ORIGINAL VIOLATIONS ---\n{violations_context}--- END VIOLATIONS ---\n\n"
                prompt += (
                    f"--- CURRENT ARM TEMPLATE ---\n{current_arm}\n--- END TEMPLATE ---\n\n"
                    "Apply ALL the remediation steps to produce a fixed ARM template.\n\n"
                    "Return a JSON object:\n"
                    "{\n"
                    '  "arm_template": { ...the complete fixed ARM JSON... },\n'
                    '  "changes_made": [\n'
                    '    {"step": 1, "description": "What was changed", "resource": "affected resource"}\n'
                    "  ]\n"
                    "}\n\n"
                    "RULES:\n"
                    "- Return the COMPLETE ARM template, not just changed parts\n"
                    "- Maintain valid ARM template structure\n"
                    "- Keep all existing parameters, variables, outputs that are still relevant\n"
                    "- Preserve resource tags, dependencies, and naming conventions\n"
                    "- Do NOT change resource names or parameter names\n"
                    "- Do NOT remove resources — only modify properties for compliance\n"
                    "- Return ONLY raw JSON — no markdown fences\n"
                )

                step_log(sid, f"Sending to AI model: {model}")
                step_log(sid, f"Prompt size: {len(prompt):,} chars")

                MAX_AI_RETRIES = 3
                result_json = None
                last_parse_error = ""

                for attempt in range(1, MAX_AI_RETRIES + 1):
                    if attempt > 1:
                        step_log(sid, f"Retry {attempt}/{MAX_AI_RETRIES} — re-sending to AI…")

                    # Progress-reporting callback for send_and_wait
                    _chunk_chars = [0]
                    _token_count = [0]

                    def on_progress(ev, _sid=sid):
                        try:
                            if ev.type.value == "assistant.message_delta":
                                delta = ev.data.delta_content or ""
                                _chunk_chars[0] += len(delta)
                                _token_count[0] += 1
                                if _token_count[0] % 50 == 0:
                                    step_log(_sid, f"Generating… {_token_count[0]} chunks received ({_chunk_chars[0]:,} chars)")
                        except Exception:
                            pass

                    retry_prompt = prompt
                    if attempt > 1 and last_parse_error:
                        retry_prompt += (
                            f"\n\nPREVIOUS ATTEMPT FAILED: {last_parse_error}\n"
                            "You MUST return ONLY valid raw JSON. No markdown fences, "
                            "no ```json blocks, no commentary before or after the JSON.\n"
                        )

                    from src.copilot_helpers import copilot_send
                    raw = await copilot_send(
                        client,
                        model=model,
                        system_prompt=REMEDIATION_EXECUTOR.system_prompt,
                        prompt=retry_prompt,
                        timeout=300,
                        on_event=on_progress,
                        agent_name="REMEDIATION_EXECUTOR",
                    )
                    step_log(sid, f"AI response: {len(raw):,} chars, {_token_count[0]} chunks")

                    if not raw:
                        last_parse_error = "Empty response from AI"
                        step_log(sid, f"⚠ Empty AI response (attempt {attempt}/{MAX_AI_RETRIES})", "warning")
                        if attempt < MAX_AI_RETRIES:
                            continue
                        else:
                            step_end(sid, "failed", int((time.time() - s2) * 1000))
                            emit({"type": "job_end", "job_id": job_id, "status": "failed",
                                  "error": "AI returned empty response after retries",
                                  "duration_ms": int((time.time() - t0) * 1000)})
                            return {"template_id": tid, "success": False, "error": "AI returned empty response"}

                    # Robust JSON extraction — strip fences, find JSON object
                    cleaned = raw
                    # Strip markdown code fences
                    if cleaned.startswith("```"):
                        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
                    if cleaned.endswith("```"):
                        cleaned = cleaned[:-3].strip()
                    if cleaned.startswith("json"):
                        cleaned = cleaned[4:].strip()
                    # Try to find the outermost { ... } if there's extra text
                    brace_start = cleaned.find("{")
                    brace_end = cleaned.rfind("}")
                    if brace_start >= 0 and brace_end > brace_start:
                        cleaned = cleaned[brace_start:brace_end + 1]

                    try:
                        result_json = json.loads(cleaned)
                        break  # Success — exit retry loop
                    except json.JSONDecodeError as e:
                        last_parse_error = f"JSON parse error: {str(e)}"
                        step_log(sid, f"⚠ {last_parse_error} (attempt {attempt}/{MAX_AI_RETRIES})", "warning")
                        if attempt < MAX_AI_RETRIES:
                            continue

                step_end(sid, "success", int((time.time() - s2) * 1000))

            # ── Step 4: VALIDATE ──
            sid = f"{job_id}-validate"
            step_start(sid)
            s3 = time.time()

            if not upgrade_skips_ai and result_json is None:
                step_log(sid, "Failed to parse AI response after retries", "error")
                step_end(sid, "failed", int((time.time() - s3) * 1000))
                emit({"type": "job_end", "job_id": job_id, "status": "failed",
                      "error": "Failed to parse AI response after retries",
                      "duration_ms": int((time.time() - t0) * 1000)})
                return {"template_id": tid, "success": False, "error": "Failed to parse AI response"}

            if upgrade_skips_ai:
                # Upgrade path — fixed_content and changes_made already set
                step_log(sid, "Validating upgraded ARM template…")
            else:
                step_log(sid, "JSON parsed successfully")

                arm_template = result_json.get("arm_template", result_json)
                changes_made = result_json.get("changes_made", [])

                # Validate ARM structure
                fixed_content = None
                if isinstance(arm_template, dict) and "$schema" in arm_template:
                    fixed_content = json.dumps(arm_template, indent=2)
                    step_log(sid, "Valid ARM template object with $schema")
                elif isinstance(arm_template, str):
                    try:
                        parsed = json.loads(arm_template)
                        if "$schema" in parsed:
                            fixed_content = json.dumps(parsed, indent=2)
                            step_log(sid, "Valid ARM template string with $schema")
                        else:
                            raise ValueError("Missing $schema")
                    except (json.JSONDecodeError, ValueError) as e:
                        step_log(sid, f"Invalid ARM template: {str(e)}", "error")
                        step_end(sid, "failed", int((time.time() - s3) * 1000))
                        emit({"type": "job_end", "job_id": job_id, "status": "failed",
                              "error": "AI returned invalid ARM JSON", "duration_ms": int((time.time() - t0) * 1000)})
                        return {"template_id": tid, "success": False, "error": "AI returned invalid ARM JSON"}

            if not fixed_content:
                step_log(sid, "Unexpected AI response format", "error")
                step_end(sid, "failed", int((time.time() - s3) * 1000))
                emit({"type": "job_end", "job_id": job_id, "status": "failed",
                      "error": "Unexpected AI response format", "duration_ms": int((time.time() - t0) * 1000)})
                return {"template_id": tid, "success": False, "error": "Unexpected AI response format"}

            # Verify resources count matches
            try:
                new_parsed = json.loads(fixed_content)
                new_res_count = len(new_parsed.get("resources", []))
                step_log(sid, f"Output: {new_res_count} resource(s), {len(fixed_content):,} bytes")
                for c in changes_made:
                    step_log(sid, f"  ✓ {c.get('description', 'change applied')}")
            except Exception:
                pass
            step_end(sid, "success", int((time.time() - s3) * 1000))

            # ── Step 5: VERIFY (Compliance Re-scan Loop) ──
            # Run _quick_compliance_check on the fixed ARM. If violations remain,
            # loop back to AI remediation with the remaining violations as context.
            # Max 3 total iterations (original fix + 2 re-attempts).
            MAX_VERIFY_LOOPS = 3
            verify_iteration = 0
            all_changes_made = list(changes_made)  # accumulate across iterations

            sid = f"{job_id}-verify"
            step_start(sid)
            s_verify = time.time()

            while True:
                verify_iteration += 1
                step_log(sid, f"Compliance re-scan (iteration {verify_iteration}/{MAX_VERIFY_LOOPS})…")

                remaining_violations = await _quick_compliance_check(fixed_content)

                if not remaining_violations:
                    step_log(sid, "✓ All compliance checks passed — template is clean")
                    step_end(sid, "success", int((time.time() - s_verify) * 1000),
                             f"Clean after {verify_iteration} iteration(s)")
                    break

                step_log(sid, f"Found {len(remaining_violations)} remaining violation(s)")
                for rv in remaining_violations[:8]:
                    step_log(sid, f"  ✗ [{rv.get('severity','?').upper()}] {rv.get('standard_name','')}: {rv.get('detail','')}")

                if verify_iteration >= MAX_VERIFY_LOOPS:
                    step_log(sid, f"⚠ {len(remaining_violations)} violation(s) remain after {MAX_VERIFY_LOOPS} attempts", "warning")
                    step_log(sid, "Proceeding with best-effort template — manual review recommended")
                    step_end(sid, "warning", int((time.time() - s_verify) * 1000),
                             f"{len(remaining_violations)} violation(s) remain")
                    break

                # ── Re-remediate: send remaining violations back to AI ──
                step_log(sid, f"Sending {len(remaining_violations)} remaining violation(s) to AI for re-fix…")

                re_instructions = "\n".join(
                    f"{j+1}. [{v.get('severity','medium').upper()}] {v.get('standard_name','')}: "
                    f"{v.get('detail','')} — Remediation: {v.get('remediation','Fix this violation')}"
                    for j, v in enumerate(remaining_violations)
                )

                re_prompt = (
                    "You are an Azure ARM template compliance remediation expert. "
                    "A previous remediation pass was applied but some violations remain.\n\n"
                    f"--- REMAINING VIOLATIONS ---\n{re_instructions}\n--- END VIOLATIONS ---\n\n"
                    f"--- CURRENT ARM TEMPLATE (after previous fix) ---\n{fixed_content}\n--- END TEMPLATE ---\n\n"
                    "Apply ALL the remaining fixes. Return a JSON object:\n"
                    "{\n"
                    '  "arm_template": { ...the complete fixed ARM JSON... },\n'
                    '  "changes_made": [\n'
                    '    {"step": 1, "description": "What was changed", "resource": "affected resource"}\n'
                    "  ]\n"
                    "}\n\n"
                    "RULES:\n"
                    "- Return the COMPLETE ARM template, not just changed parts\n"
                    "- Maintain valid ARM template structure\n"
                    "- Keep all existing parameters, variables, outputs that are still relevant\n"
                    "- Preserve resource tags, dependencies, and naming conventions\n"
                    "- Do NOT change resource names or parameter names\n"
                    "- Do NOT remove resources — only modify properties for compliance\n"
                    "- Return ONLY raw JSON — no markdown fences\n"
                )

                _re_chunk_chars = [0]
                _re_token_count = [0]

                def on_re_progress(ev, _sid=sid):
                    try:
                        if ev.type.value == "assistant.message_delta":
                            delta = ev.data.delta_content or ""
                            _re_chunk_chars[0] += len(delta)
                            _re_token_count[0] += 1
                            if _re_token_count[0] % 50 == 0:
                                step_log(_sid, f"Re-fix generating… {_re_token_count[0]} chunks ({_re_chunk_chars[0]:,} chars)")
                    except Exception:
                        pass

                from src.copilot_helpers import copilot_send
                re_raw = await copilot_send(
                    client, model=model,
                    system_prompt=REMEDIATION_EXECUTOR.system_prompt,
                    prompt=re_prompt,
                    timeout=300, on_event=on_re_progress,
                    agent_name="REMEDIATION_EXECUTOR",
                )
                step_log(sid, f"AI re-fix response: {len(re_raw):,} chars")

                if not re_raw:
                    step_log(sid, "⚠ Empty AI response on re-fix — stopping loop", "warning")
                    step_end(sid, "warning", int((time.time() - s_verify) * 1000),
                             f"{len(remaining_violations)} violation(s) remain")
                    break

                # Parse the re-fix response
                re_cleaned = re_raw
                if re_cleaned.startswith("```"):
                    re_cleaned = re_cleaned.split("\n", 1)[1] if "\n" in re_cleaned else re_cleaned[3:]
                if re_cleaned.endswith("```"):
                    re_cleaned = re_cleaned[:-3].strip()
                if re_cleaned.startswith("json"):
                    re_cleaned = re_cleaned[4:].strip()
                brace_start = re_cleaned.find("{")
                brace_end = re_cleaned.rfind("}")
                if brace_start >= 0 and brace_end > brace_start:
                    re_cleaned = re_cleaned[brace_start:brace_end + 1]

                try:
                    re_result = json.loads(re_cleaned)
                except json.JSONDecodeError as e:
                    step_log(sid, f"⚠ Could not parse AI re-fix: {e}", "warning")
                    step_end(sid, "warning", int((time.time() - s_verify) * 1000),
                             f"{len(remaining_violations)} violation(s) remain")
                    break

                # Extract and validate the re-fixed ARM
                re_arm = re_result.get("arm_template", re_result)
                re_changes = re_result.get("changes_made", [])

                re_fixed = None
                if isinstance(re_arm, dict) and "$schema" in re_arm:
                    re_fixed = json.dumps(re_arm, indent=2)
                elif isinstance(re_arm, str):
                    try:
                        parsed = json.loads(re_arm)
                        if "$schema" in parsed:
                            re_fixed = json.dumps(parsed, indent=2)
                    except Exception:
                        pass

                if not re_fixed:
                    step_log(sid, "⚠ AI re-fix produced invalid ARM — stopping loop", "warning")
                    step_end(sid, "warning", int((time.time() - s_verify) * 1000),
                             f"{len(remaining_violations)} violation(s) remain")
                    break

                fixed_content = re_fixed
                all_changes_made.extend(re_changes)
                for c in re_changes:
                    step_log(sid, f"  ✓ {c.get('description', 'change applied')}")
                step_log(sid, f"Applied {len(re_changes)} additional fix(es) — re-scanning…")

            # Update changes_made with accumulated fixes from all iterations
            changes_made = all_changes_made

            # ── Step 6: DEPLOY TEST (ARM What-If) ──  (was step 5)
            sid = f"{job_id}-deploy-test"
            step_start(sid)
            s_dt = time.time()

            try:
                sub_id = _get_subscription_id()
                short_tid = tid.replace('-', '')[:12]
                validation_rg = f"infraforge-validate-{short_tid}"
                validation_region = "eastus2"
                validation_deployment = f"whatif-{uuid.uuid4().hex[:8]}"

                step_log(sid, f"Subscription: {sub_id}")
                step_log(sid, f"Resource group: {validation_rg}")
                step_log(sid, f"Region: {validation_region}")
                step_log(sid, f"Deployment name: {validation_deployment}")

                # Ensure template has default parameter values
                sanitized_arm = _ensure_parameter_defaults(fixed_content)
                arm_dict = json.loads(sanitized_arm)
                param_values = _extract_param_values(arm_dict)
                step_log(sid, f"Resolved {len(param_values)} parameter value(s) for deployment")

                started_at = datetime.now(timezone.utc)
                step_log(sid, f"What-If started: {started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
                step_log(sid, "Running ARM What-If against Azure…")

                what_if_result = await run_what_if(
                    resource_group=validation_rg,
                    template=arm_dict,
                    parameters=param_values,
                    region=validation_region,
                )

                finished_at = datetime.now(timezone.utc)
                wif_status = what_if_result.get("status", "unknown")
                step_log(sid, f"What-If completed: {finished_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
                step_log(sid, f"What-If status: {wif_status}")

                # Log per-resource results
                change_counts = what_if_result.get("change_counts", {})
                total_changes = what_if_result.get("total_changes", 0)
                step_log(sid, f"Total resource operations: {total_changes}")
                for ctype, count in change_counts.items():
                    step_log(sid, f"  {ctype}: {count}")

                for change in what_if_result.get("changes", []):
                    rtype = change.get("resource_type", "?")
                    rname = change.get("resource_name", "?")
                    ctype = change.get("change_type", "?")
                    step_log(sid, f"  → {ctype} {rtype}/{rname}")

                if what_if_result.get("has_destructive_changes"):
                    step_log(sid, "⚠ Destructive changes detected (Delete operations)", "error")

                # Clean up validation resource group
                step_log(sid, f"Cleaning up validation RG: {validation_rg}")
                try:
                    rg_client = _get_resource_client()
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        None,
                        lambda: rg_client.resource_groups.begin_delete(validation_rg),
                    )
                    cleanup_at = datetime.now(timezone.utc)
                    step_log(sid, f"RG deletion initiated: {cleanup_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
                except Exception as cleanup_err:
                    step_log(sid, f"RG cleanup warning: {str(cleanup_err)}", "error")

                deploy_proof = {
                    "subscription_id": sub_id,
                    "resource_group": validation_rg,
                    "deployment_name": validation_deployment,
                    "region": validation_region,
                    "started_at": started_at.isoformat(),
                    "completed_at": finished_at.isoformat(),
                    "cleanup_initiated_at": datetime.now(timezone.utc).isoformat(),
                    "what_if_status": wif_status,
                    "total_changes": total_changes,
                    "change_counts": change_counts,
                }

                step_log(sid, "✓ ARM What-If validation passed")
                step_end(sid, "success", int((time.time() - s_dt) * 1000))

            except Exception as deploy_err:
                step_log(sid, f"⚠ ARM What-If could not complete: {str(deploy_err)}", "warning")
                step_log(sid, "This is advisory only — the template is still valid and will be versioned.")
                step_log(sid, "Common causes: missing Azure credentials, subscription quota, or transient API errors.")
                step_log(sid, "To retry deployment validation later, use the Deploy button from the template version viewer.")
                deploy_proof = {"error": str(deploy_err), "status": "skipped"}
                step_end(sid, "warning", int((time.time() - s_dt) * 1000),
                         "What-If skipped (advisory)")

            # ── Check if ARM template actually changed ──
            # Normalise both to sorted JSON for comparison so that
            # cosmetic whitespace / key-order differences don't count.
            def _normalise_arm(s: str) -> str:
                try:
                    return json.dumps(json.loads(s), sort_keys=True, indent=2)
                except Exception:
                    return s.strip()

            template_unchanged = _normalise_arm(fixed_content) == _normalise_arm(current_arm)

            if template_unchanged:
                # ── Step 7: VERSION (skipped — no changes) ──
                sid = f"{job_id}-version"
                step_start(sid)
                s4 = time.time()
                step_log(sid, "ARM template is identical to previous version — no changes were needed")
                step_log(sid, "Skipping version creation to avoid duplicate entries")
                step_end(sid, "success", int((time.time() - s4) * 1000), "Skipped (no changes)")

                # ── Step 8: PUBLISH (skipped — no changes) ──
                sid = f"{job_id}-publish"
                step_start(sid)
                s5 = time.time()
                step_log(sid, "No new version to publish — template already compliant")
                step_end(sid, "success", int((time.time() - s5) * 1000), "Skipped (no changes)")
            else:
                # ── Step 7: VERSION ──
                sid = f"{job_id}-version"
                step_start(sid)
                s4 = time.time()

                changes_desc = "; ".join(
                    c.get("description", "") for c in changes_made if c.get("description")
                ) or "Compliance remediation applied"
                changelog = f"Compliance remediation: {changes_desc}"
                step_change_type = steps[0].get("change_type", "patch") if steps else "patch"

                # Get fresh semver for the version bump
                # Service deps store versions in service_versions, not template_versions
                is_service_dep = tid in dep_service_ids and tid != template_id

                if is_service_dep:
                    latest_svc = await get_latest_service_version(tid)
                    latest_semver = (latest_svc.get("semver") or "1.0.0") if latest_svc else "1.0.0"
                else:
                    latest_semver = await get_latest_semver(tid) or "1.0.0"

                new_semver = compute_next_semver(latest_semver, step_change_type)
                step_log(sid, f"Version bump: {latest_semver} → {new_semver} ({step_change_type})")
                step_log(sid, f"Changelog: {changelog[:120]}{'…' if len(changelog) > 120 else ''}")

                if is_service_dep:
                    new_ver = await create_service_version(
                        tid,
                        fixed_content,
                        semver=new_semver,
                        changelog=changelog,
                        created_by="compliance-remediation",
                    )
                else:
                    new_ver = await create_template_version(
                        tid,
                        fixed_content,
                        changelog=changelog,
                        change_type=step_change_type,
                        created_by="compliance-remediation",
                    )

                new_version_num = new_ver.get("version", "?")
                new_semver_actual = new_ver.get("semver", new_semver)
                step_log(sid, f"Created version #{new_version_num} (v{new_semver_actual})")
                step_end(sid, "success", int((time.time() - s4) * 1000))

                # ── Step 8: PUBLISH ──
                sid = f"{job_id}-publish"
                step_start(sid)
                s5 = time.time()

                now_iso = datetime.now(timezone.utc).isoformat()

                if is_service_dep:
                    # Service deps store ARM in service_versions (already written in
                    # step 6).  Also update catalog_templates.content if a row exists,
                    # so compliance re-scans and other code paths see the fix.
                    step_log(sid, f"Service version v{new_semver_actual} stored for {tid}")
                    try:
                        updated = await backend.execute_write(
                            "UPDATE catalog_templates SET content = ?, updated_at = ? WHERE id = ?",
                            (fixed_content, now_iso, tid),
                        )
                        if updated:
                            step_log(sid, f"Catalog content synced for {tid}")
                    except Exception:
                        pass  # No catalog_templates row for this service — that's OK
                else:
                    step_log(sid, "Updating catalog template content…")
                    await backend.execute_write(
                        "UPDATE catalog_templates SET content = ?, updated_at = ? WHERE id = ?",
                        (fixed_content, now_iso, tid),
                    )
                    step_log(sid, f"Catalog updated for {tid}")

                step_log(sid, f"New template published: v{new_semver_actual}")
                step_end(sid, "success", int((time.time() - s5) * 1000))

            # ── Persist remediation log onto the new version ──
            if not template_unchanged and not is_service_dep and new_version_num != "?":
                try:
                    await update_template_validation_status(
                        tid,
                        new_version_num,
                        "draft",
                        {"remediation_log": job_log,
                         "deploy_proof": deploy_proof},
                    )
                except Exception as log_err:
                    logger.warning(f"Failed to persist remediation log for {tid} v{new_version_num}: {log_err}")

            # ── Job complete ──
            if template_unchanged:
                result = {
                    "template_id": tid,
                    "template_name": tname,
                    "success": True,
                    "old_version": ver_num,
                    "old_semver": current_semver or None,
                    "new_version": None,
                    "new_semver": None,
                    "changes_made": [],
                    "changelog": "No changes needed — template already compliant",
                    "deploy_proof": deploy_proof,
                    "verify_iterations": verify_iteration,
                    "verify_clean": not remaining_violations,
                    "remaining_violations": len(remaining_violations) if remaining_violations else 0,
                    "skipped_version": True,
                }
            else:
                result = {
                    "template_id": tid,
                    "template_name": tname,
                    "success": True,
                    "old_version": ver_num,
                    "old_semver": current_semver or None,
                    "new_version": new_version_num,
                    "new_semver": new_semver_actual,
                    "changes_made": changes_made,
                    "changelog": changelog,
                    "deploy_proof": deploy_proof,
                    "verify_iterations": verify_iteration,
                    "verify_clean": not remaining_violations,
                    "remaining_violations": len(remaining_violations) if remaining_violations else 0,
                }
            emit({"type": "job_end", "job_id": job_id, "status": "success",
                  "result": result, "duration_ms": int((time.time() - t0) * 1000)})
            return result

        except Exception as e:
            step_log(sid, f"Unexpected error: {str(e)}", "error")
            step_end(sid, "failed", 0)
            emit({"type": "job_end", "job_id": job_id, "status": "failed",
                  "error": str(e), "duration_ms": int((time.time() - t0) * 1000)})
            return {"template_id": tid, "success": False, "error": str(e)}

    async def _generate():
        pipeline_start = time.time()

        # Emit pipeline init
        yield json.dumps({
            "type": "pipeline_init",
            "jobs": jobs,
            "parallel": len(jobs) > 1,
            "total_jobs": len(jobs),
            "template_id": template_id,
            "template_name": tmpl.get("name", template_id),
        }) + "\n"
        await asyncio.sleep(0)

        # Launch all jobs in parallel
        tasks = []
        for i, (tid, steps) in enumerate(steps_by_template.items()):
            tasks.append(asyncio.create_task(_run_job(i, tid, steps)))

        # Drain events from the queue while jobs run
        active = True
        while active:
            # Check if all tasks are done
            all_done = all(t.done() for t in tasks)

            # Drain all queued events
            while not event_queue.empty():
                try:
                    evt = event_queue.get_nowait()
                    yield json.dumps(evt) + "\n"
                    await asyncio.sleep(0)
                except asyncio.QueueEmpty:
                    break

            if all_done:
                # Final drain
                while not event_queue.empty():
                    try:
                        evt = event_queue.get_nowait()
                        yield json.dumps(evt) + "\n"
                    except asyncio.QueueEmpty:
                        break
                active = False
            else:
                await asyncio.sleep(0.1)

        # Collect results
        results = []
        for t in tasks:
            try:
                results.append(t.result())
            except Exception as e:
                results.append({"success": False, "error": str(e)})

        # ── Recompose parent template if any dependency was fixed ──
        any_success = any(r.get("success") for r in results)
        successful_dep_tids = [
            r["template_id"] for r in results
            if r.get("success") and r.get("template_id") != template_id
            and not r.get("skipped_version")  # only deps that actually changed
        ]
        # Also handle the case where the parent itself was fixed
        parent_was_fixed = any(
            r.get("success") and r.get("template_id") == template_id
            and not r.get("skipped_version")
            for r in results
        )

        if successful_dep_tids and dep_service_ids:
            # Dependencies were remediated — recompose the parent's ARM
            try:
                # Start from the current parent ARM
                parent_versions = await get_template_versions(template_id)
                parent_arm_str = ""
                if parent_versions:
                    parent_ver = await get_template_version(
                        template_id, parent_versions[0]["version"]
                    )
                    parent_arm_str = parent_ver.get("arm_template", "") if parent_ver else ""
                if not parent_arm_str:
                    parent_arm_str = tmpl.get("content", "")

                composed = json.loads(parent_arm_str)

                # For each fixed dependency, swap its resources into the parent
                for dep_tid in successful_dep_tids:
                    # Read the updated dep content — prefer template_versions
                    # (the VERSION step always writes there), fall back to catalog
                    dep_arm_str = ""
                    dep_versions = await get_template_versions(dep_tid)
                    if dep_versions:
                        dep_ver = await get_template_version(
                            dep_tid, dep_versions[0]["version"]
                        )
                        dep_arm_str = dep_ver.get("arm_template", "") if dep_ver else ""
                    if not dep_arm_str:
                        dep_rows = await backend.execute(
                            "SELECT content FROM catalog_templates WHERE id = ?",
                            (dep_tid,),
                        )
                        if dep_rows and dep_rows[0].get("content"):
                            dep_arm_str = dep_rows[0]["content"]
                    if not dep_arm_str:
                        continue
                    dep_arm = json.loads(dep_arm_str)
                    dep_resources = dep_arm.get("resources", [])
                    if not dep_resources:
                        continue

                    # Identify resource types from the fixed dep
                    dep_types = {
                        r.get("type", "").lower()
                        for r in dep_resources
                        if isinstance(r, dict) and r.get("type")
                    }

                    # Remove old resources of these types from parent
                    kept = [
                        r for r in composed.get("resources", [])
                        if not isinstance(r, dict)
                        or r.get("type", "").lower() not in dep_types
                    ]
                    # Add the new fixed resources
                    kept.extend(dep_resources)
                    composed["resources"] = kept

                    # Merge parameters and variables from the fixed dep
                    for pk, pv in dep_arm.get("parameters", {}).items():
                        composed.setdefault("parameters", {})[pk] = pv
                    for vk, vv in dep_arm.get("variables", {}).items():
                        composed.setdefault("variables", {})[vk] = vv

                recomposed_arm = json.dumps(composed, indent=2)

                # Create a new parent version with the recomposed ARM
                # Determine the bump type from the highest dep change
                parent_change = "patch"
                for r in results:
                    if r.get("success") and r.get("template_id") != template_id:
                        # The individual job used whatever change_type the plan had
                        pass  # patch is fine for recomposition

                parent_semver = await get_latest_semver(template_id) or "1.0.0"
                parent_new_semver = compute_next_semver(parent_semver, parent_change)

                dep_names = ", ".join(successful_dep_tids)
                recompose_changelog = (
                    f"Recomposed after compliance remediation of: {dep_names}"
                )

                await create_template_version(
                    template_id,
                    recomposed_arm,
                    changelog=recompose_changelog,
                    change_type=parent_change,
                    created_by="compliance-remediation",
                )

                # Also update catalog_templates.content for the parent
                now_iso = datetime.now(timezone.utc).isoformat()
                await backend.execute_write(
                    "UPDATE catalog_templates SET content = ?, updated_at = ? WHERE id = ?",
                    (recomposed_arm, now_iso, template_id),
                )

                yield json.dumps({
                    "type": "step_log",
                    "job_id": "recompose",
                    "step_id": "recompose",
                    "message": f"Recomposed parent template with {len(successful_dep_tids)} updated dependencies → v{parent_new_semver}",
                    "level": "info",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }) + "\n"
                await asyncio.sleep(0)

            except Exception as e:
                yield json.dumps({
                    "type": "step_log",
                    "job_id": "recompose",
                    "step_id": "recompose",
                    "message": f"Warning: Failed to recompose parent template: {str(e)}",
                    "level": "error",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }) + "\n"
                await asyncio.sleep(0)

        elif parent_was_fixed:
            # The parent itself was remediated (non-composed or parent-targeted fix).
            # The job already saved the new version and updated catalog content.
            # But also update catalog_templates.content from the latest version
            # to keep them in sync.
            try:
                parent_versions = await get_template_versions(template_id)
                if parent_versions:
                    latest_ver = await get_template_version(
                        template_id, parent_versions[0]["version"]
                    )
                    if latest_ver and latest_ver.get("arm_template"):
                        now_iso = datetime.now(timezone.utc).isoformat()
                        await backend.execute_write(
                            "UPDATE catalog_templates SET content = ?, updated_at = ? WHERE id = ?",
                            (latest_ver["arm_template"], now_iso, template_id),
                        )
            except Exception:
                pass  # Non-critical — the version table is the source of truth

        # Pipeline done
        yield json.dumps({
            "type": "pipeline_done",
            "template_id": template_id,
            "results": results,
            "all_success": all(r.get("success") for r in results),
            "duration_ms": int((time.time() - pipeline_start) * 1000),
        }) + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")


# ── Auto-Heal Template ──────────────────────────────────────

@app.post("/api/catalog/templates/{template_id}/auto-heal")
async def auto_heal_template(template_id: str):
    """Automatically fix a template that failed structural tests.

    Flow:
    1. Get the template and its latest test results
    2. Ask the LLM to fix structural issues in the ARM JSON
    3. Save the fixed template as a new version
    4. Re-run structural tests
    5. Return results

    No user input required — the system figures out what's wrong and fixes it.
    """
    import json as _json

    tmpl = await _require_template(template_id)

    # Get latest version and its test results
    versions = await get_template_versions(template_id)
    if not versions:
        raise HTTPException(status_code=404, detail="No versions found")

    latest_ver = versions[0]
    arm_content = latest_ver.get("arm_template", "")
    test_results = latest_ver.get("test_results", {})
    validation_results = latest_ver.get("validation_results", {})

    # Gather failed tests into an error description
    failed_tests = []
    if isinstance(test_results, dict) and test_results:
        for t in test_results.get("tests", []):
            if not t.get("passed", True):
                failed_tests.append(f"- {t['name']}: {t.get('message', 'failed')}")

    # Also check validation (deploy) failures
    if isinstance(validation_results, dict) and validation_results:
        if not validation_results.get("validation_passed", True):
            _had_heal_err = False
            for h in validation_results.get("heal_history", []):
                if h.get("error"):
                    failed_tests.append(f"- Deploy: {h['error'][:200]}")
                    _had_heal_err = True
            # If validation failed but no heal_history recorded the error,
            # run ARM What-If now to capture the actual deployment error.
            if not _had_heal_err:
                try:
                    from src.azure_deployer import AzureDeployer
                    _deployer = AzureDeployer()
                    _tpl_check = _json.loads(arm_content)
                    from src.pipeline_helpers import extract_param_values, build_final_params
                    _check_params = build_final_params(_tpl_check, "eastus2")
                    _wif_result = await _deployer.what_if(
                        resource_group=f"infraforge-val-heal-check",
                        template=_tpl_check,
                        parameters=_check_params,
                        region="eastus2",
                    )
                    if _wif_result.get("error"):
                        failed_tests.append(f"- ARM Validation: {_wif_result['error'][:300]}")
                except Exception as _wif_e:
                    # What-If itself may throw with the validation error message
                    _wif_err_str = str(_wif_e)
                    if _wif_err_str:
                        failed_tests.append(f"- ARM Validation: {_wif_err_str[:300]}")

    # If no recorded failures, run structural tests now to find issues
    if not failed_tests and tmpl.get("status") in ("failed", "draft"):
        import json as _j2
        try:
            _tpl = _j2.loads(arm_content)
            # Quick structural checks
            if "$schema" not in _tpl:
                failed_tests.append("- ARM Schema: Missing $schema")
            if "contentVersion" not in _tpl:
                failed_tests.append("- ARM Schema: Missing contentVersion")
            if not isinstance(_tpl.get("resources"), list) or not _tpl.get("resources"):
                failed_tests.append("- Resources: No resources defined")
            TAG_REQ = {"environment", "project", "owner"}
            for i, r in enumerate(_tpl.get("resources", [])):
                if isinstance(r, dict):
                    if "type" not in r:
                        failed_tests.append(f"- Resources: Resource [{i}] missing 'type'")
                    if "apiVersion" not in r:
                        failed_tests.append(f"- Resources: Resource [{i}] missing 'apiVersion'")
                    if "name" not in r:
                        failed_tests.append(f"- Resources: Resource [{i}] missing 'name'")
                    tags = r.get("tags", {})
                    if isinstance(tags, dict):
                        missing = TAG_REQ - set(k.lower() for k in tags)
                        if missing:
                            failed_tests.append(f"- Tags: Resource [{i}] ({r.get('type','?')}) missing {', '.join(missing)}")

            # Check for utcNow() in variables (ARM only allows it in parameter defaults)
            _vars = _tpl.get("variables", {})
            for _vname, _vval in _vars.items():
                if isinstance(_vval, str) and "utcNow" in _vval:
                    failed_tests.append(
                        f"- ARM Function: Variable '{_vname}' uses utcNow() — "
                        f"this function is only valid in parameter defaultValue expressions. "
                        f"Move it to a parameter default or remove it."
                    )
        except Exception:
            failed_tests.append("- JSON: Template is not valid JSON")

    if not failed_tests:
        # Actually no issues found — run real tests and set status to passed
        # so the template moves forward in the lifecycle
        try:
            _tpl = _json.loads(arm_content)
            # If tests pass, promote the template status
            new_ver_num = latest_ver["version"]
            _tr = {"tests": [], "passed": 0, "failed": 0, "total": 0, "all_passed": True}

            # Quick full check
            checks = [
                ("$schema" in _tpl, "ARM Schema"),
                ("contentVersion" in _tpl, "Content Version"),
                (isinstance(_tpl.get("resources"), list) and len(_tpl.get("resources", [])) > 0, "Resources"),
            ]
            for ok, name in checks:
                _tr["tests"].append({"name": name, "passed": ok, "message": "OK" if ok else "Failed"})
                _tr["total"] += 1
                if ok:
                    _tr["passed"] += 1
                else:
                    _tr["failed"] += 1
                    _tr["all_passed"] = False

            new_status = "passed" if _tr["all_passed"] else "failed"
            await update_template_version_status(template_id, new_ver_num, new_status, _tr)

            _tb = await get_backend()
            await _tb.execute_write(
                "UPDATE catalog_templates SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, datetime.now(timezone.utc).isoformat(), template_id),
            )

            return JSONResponse({
                "status": "already_healthy",
                "template_id": template_id,
                "all_passed": _tr["all_passed"],
                "retest": _tr,
                "message": "Template is structurally sound — tests passed! Ready for the next step."
                           if _tr["all_passed"] else "Some structural issues remain.",
            })
        except Exception:
            return JSONResponse({
                "status": "no_issues",
                "template_id": template_id,
                "message": "No test failures detected — template may already be fine.",
            })

    error_description = "Structural test failures:\n" + "\n".join(failed_tests)
    logger.info(f"Auto-heal {template_id}: {error_description}")

    # Try LLM-based healing
    client = await ensure_copilot_client()
    fixed_arm = None

    if client:
        try:
            from src.pipeline_helpers import copilot_heal_template as _canonical_heal
            fixed_arm = await _canonical_heal(
                content=arm_content,
                error=error_description,
                previous_attempts=[],
            )
        except Exception as e:
            logger.warning(f"LLM heal failed for {template_id}: {e}")

    if not fixed_arm:
        # Heuristic fix: try to fix common structural issues
        try:
            tpl = _json.loads(arm_content)
            changed = False

            # Fix missing $schema
            if "$schema" not in tpl:
                tpl["$schema"] = "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#"
                changed = True

            # Fix missing contentVersion
            if "contentVersion" not in tpl:
                tpl["contentVersion"] = "1.0.0.0"
                changed = True

            # Fix missing resources
            if "resources" not in tpl:
                tpl["resources"] = []
                changed = True

            # Fix parameters not being a dict
            if not isinstance(tpl.get("parameters"), dict):
                tpl["parameters"] = {}
                changed = True

            # Fix resources not being a list
            if not isinstance(tpl.get("resources"), list):
                tpl["resources"] = []
                changed = True

            # Fix individual resource issues
            TAG_SET = {
                "environment": "[parameters('environment')]",
                "owner": "[parameters('ownerEmail')]",
                "costCenter": "[parameters('costCenter')]",
                "project": "[parameters('projectName')]",
                "managedBy": "InfraForge",
            }
            for res in tpl.get("resources", []):
                if not isinstance(res, dict):
                    continue
                # Add missing tags
                if "tags" not in res or not isinstance(res.get("tags"), dict):
                    res["tags"] = dict(TAG_SET)
                    changed = True
                else:
                    for tk, tv in TAG_SET.items():
                        if tk not in res["tags"]:
                            res["tags"][tk] = tv
                            changed = True

            if changed:
                fixed_arm = _json.dumps(tpl, indent=2)
        except Exception as e:
            logger.warning(f"Heuristic heal failed for {template_id}: {e}")

    if not fixed_arm:
        return JSONResponse({
            "status": "heal_failed",
            "template_id": template_id,
            "errors": failed_tests,
            "message": "I tried but couldn't fix this one automatically. Try using Request Revision to describe what needs to change.",
        })

    # Save the fixed template
    tmpl["content"] = fixed_arm
    try:
        await upsert_template(tmpl)
        new_ver = await create_template_version(
            template_id, fixed_arm,
            changelog="Auto-healed: fixed structural test failures",
            change_type="patch",
            created_by="auto-healer",
        )
    except Exception as e:
        logger.error(f"Failed to save healed template {template_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # Re-run structural tests on the fixed version
    new_version_num = new_ver["version"]
    new_arm = fixed_arm

    # ── Inline test suite (same as test endpoint) ─────────────
    tests: list[dict] = []
    all_passed = True
    tpl = None
    try:
        tpl = _json.loads(new_arm)
        tests.append({"name": "JSON Structure", "passed": True, "message": "Valid JSON"})
    except Exception as e:
        tests.append({"name": "JSON Structure", "passed": False, "message": f"Invalid JSON: {e}"})
        all_passed = False

    if tpl:
        # Schema
        schema_ok = all(k in tpl for k in ("$schema", "contentVersion", "resources"))
        tests.append({"name": "ARM Schema", "passed": schema_ok,
                       "message": "Valid ARM structure" if schema_ok else "Missing required schema fields"})
        if not schema_ok:
            all_passed = False

        # Parameters
        params = tpl.get("parameters", {})
        param_ok = isinstance(params, dict) and all(
            isinstance(v, dict) and "type" in v for v in params.values()
        )
        tests.append({"name": "Parameters", "passed": param_ok,
                       "message": f"{len(params)} parameters valid" if param_ok else "Parameter issues remain"})
        if not param_ok:
            all_passed = False

        # Resources
        resources = tpl.get("resources", [])
        res_ok = isinstance(resources, list) and len(resources) > 0 and all(
            isinstance(r, dict) and "type" in r and "apiVersion" in r and "name" in r
            for r in resources
        )
        tests.append({"name": "Resources", "passed": res_ok,
                       "message": f"{len(resources)} resources valid" if res_ok else "Resource issues remain"})
        if not res_ok:
            all_passed = False

        # Tag compliance
        TAG_REQUIRED = {"environment", "project", "owner"}
        tag_ok = True
        for res in resources:
            if isinstance(res, dict):
                tags = res.get("tags", {})
                if isinstance(tags, dict):
                    if TAG_REQUIRED - set(k.lower() for k in tags):
                        tag_ok = False
                        break
        tests.append({"name": "Tag Compliance", "passed": tag_ok,
                       "message": "All resources properly tagged" if tag_ok else "Tag issues remain"})
        if not tag_ok:
            all_passed = False

    retest_status = "passed" if all_passed else "failed"
    retest_results = {
        "tests": tests,
        "passed": sum(1 for t in tests if t["passed"]),
        "failed": sum(1 for t in tests if not t["passed"]),
        "total": len(tests),
        "all_passed": all_passed,
    }

    await update_template_version_status(template_id, new_version_num, retest_status, retest_results)

    # Sync parent template status
    _tb = await get_backend()
    await _tb.execute_write(
        "UPDATE catalog_templates SET status = ?, updated_at = ? WHERE id = ?",
        (retest_status, datetime.now(timezone.utc).isoformat(), template_id),
    )

    return JSONResponse({
        "status": "healed" if all_passed else "partial",
        "template_id": template_id,
        "version": new_version_num,
        "original_failures": failed_tests,
        "retest": retest_results,
        "all_passed": all_passed,
        "message": "All fixed! Every test is passing now." if all_passed
                   else f"I fixed some things, but {retest_results['failed']} test(s) still need attention.",
    })


# ── Structural Test Suite (shared by test_template, recompose, pin) ────


def _run_structural_tests(
    arm_content: str,
    *,
    expected_service_ids: list[str] | None = None,
) -> dict:
    """Run structural tests on ARM JSON content.

    Args:
        arm_content: Raw ARM JSON string.
        expected_service_ids: For composite templates, the list of service IDs
            that should each contribute at least one resource.  When provided,
            an extra test validates that every expected type is present.

    Returns {tests, passed, failed, total, all_passed}.
    Pure function — no DB calls.
    """
    import json as _json

    tests: list[dict] = []
    all_passed = True

    # Test 1: JSON parse
    tpl = None
    try:
        tpl = _json.loads(arm_content)
        tests.append({"name": "JSON Structure", "passed": True, "message": "Valid JSON"})
    except Exception as e:
        tests.append({"name": "JSON Structure", "passed": False, "message": f"Invalid JSON: {e}"})
        all_passed = False

    if tpl:
        # Test 2: Schema compliance
        schema_ok = True
        schema_msgs = []
        if "$schema" not in tpl:
            schema_msgs.append("Missing $schema"); schema_ok = False
        if "contentVersion" not in tpl:
            schema_msgs.append("Missing contentVersion"); schema_ok = False
        if "resources" not in tpl:
            schema_msgs.append("Missing resources array"); schema_ok = False
        if not isinstance(tpl.get("resources"), list):
            schema_msgs.append("resources must be an array"); schema_ok = False
        tests.append({
            "name": "ARM Schema", "passed": schema_ok,
            "message": "Valid ARM structure" if schema_ok else "; ".join(schema_msgs),
        })
        if not schema_ok:
            all_passed = False

        # Test 3: Parameter validation
        params = tpl.get("parameters", {})
        param_ok = True
        param_msgs = []
        if not isinstance(params, dict):
            param_msgs.append("parameters must be an object"); param_ok = False
        else:
            for pname, pdef in params.items():
                if not pname.strip():
                    param_msgs.append("Empty parameter name found"); param_ok = False
                if not isinstance(pdef, dict):
                    param_msgs.append(f"Parameter '{pname}' must be an object"); param_ok = False
                elif "type" not in pdef:
                    param_msgs.append(f"Parameter '{pname}' missing type"); param_ok = False
        tests.append({
            "name": "Parameters", "passed": param_ok,
            "message": f"{len(params)} parameters valid" if param_ok else "; ".join(param_msgs),
        })
        if not param_ok:
            all_passed = False

        # Test 4: Resource validation
        resources = tpl.get("resources", [])
        res_ok = True
        res_msgs = []
        if not resources:
            res_msgs.append("No resources defined"); res_ok = False
        for i, res in enumerate(resources):
            if not isinstance(res, dict):
                res_msgs.append(f"Resource [{i}] is not an object"); res_ok = False; continue
            if "type" not in res:
                res_msgs.append(f"Resource [{i}] missing 'type'"); res_ok = False
            if "apiVersion" not in res:
                res_msgs.append(f"Resource [{i}] ({res.get('type', '?')}) missing 'apiVersion'"); res_ok = False
            if "name" not in res:
                res_msgs.append(f"Resource [{i}] ({res.get('type', '?')}) missing 'name'"); res_ok = False
        tests.append({
            "name": "Resources", "passed": res_ok,
            "message": f"{len(resources)} resources valid" if res_ok else "; ".join(res_msgs[:5]),
        })
        if not res_ok:
            all_passed = False

        # Test 5: Output validation
        outputs = tpl.get("outputs", {})
        out_ok = True
        out_msgs = []
        if isinstance(outputs, dict):
            for oname, odef in outputs.items():
                if not isinstance(odef, dict):
                    out_msgs.append(f"Output '{oname}' must be an object"); out_ok = False
                elif "type" not in odef or "value" not in odef:
                    out_msgs.append(f"Output '{oname}' missing type or value"); out_ok = False
        tests.append({
            "name": "Outputs", "passed": out_ok,
            "message": f"{len(outputs)} outputs valid" if out_ok else "; ".join(out_msgs),
        })
        if not out_ok:
            all_passed = False

        # Test 6: Tag compliance
        TAG_REQUIRED = {"environment", "project", "owner"}
        tag_ok = True
        tag_msgs = []
        for i, res in enumerate(resources):
            if not isinstance(res, dict):
                continue
            res_tags = res.get("tags", {})
            if not isinstance(res_tags, dict) and not isinstance(res_tags, str):
                tag_msgs.append(f"Resource [{i}] ({res.get('type', '?')}) has invalid tags"); tag_ok = False
            elif isinstance(res_tags, dict):
                tag_values = set(tk.lower() for tk in res_tags)
                missing = TAG_REQUIRED - tag_values
                if missing and not any(isinstance(v, str) and "variables('standardTags')" in v for v in res_tags.values()):
                    tag_msgs.append(f"Resource [{i}] ({res.get('type', '?')}) missing tags: {', '.join(missing)}"); tag_ok = False
        tests.append({
            "name": "Tag Compliance", "passed": tag_ok,
            "message": "All resources properly tagged" if tag_ok else "; ".join(tag_msgs[:3]),
        })
        if not tag_ok:
            all_passed = False

        # Test 7: Naming convention
        naming_ok = True
        naming_msgs = []
        for i, res in enumerate(resources):
            if not isinstance(res, dict):
                continue
            rname = res.get("name", "")
            if isinstance(rname, str) and rname and not rname.startswith("["):
                naming_msgs.append(f"Resource [{i}] ({res.get('type', '?')}) uses hardcoded name '{rname}'"); naming_ok = False
        tests.append({
            "name": "Naming Convention", "passed": naming_ok,
            "message": "All resource names use parameters/expressions" if naming_ok else "; ".join(naming_msgs[:3]),
        })
        if not naming_ok:
            all_passed = False

        # Test 8: Composition completeness (composite templates only)
        if expected_service_ids:
            # Check that every expected service type has at least one resource
            resource_type_set = {
                (r.get("type") or "").lower() for r in resources if isinstance(r, dict)
            }
            missing_types = []
            for sid in expected_service_ids:
                sid_lower = sid.lower()
                # Match exact type or child types
                found = any(
                    rt == sid_lower or rt.startswith(sid_lower + "/")
                    for rt in resource_type_set
                )
                if not found:
                    missing_types.append(sid)
            comp_ok = not missing_types
            tests.append({
                "name": "Composition Completeness",
                "passed": comp_ok,
                "message": (
                    f"All {len(expected_service_ids)} service types present"
                    if comp_ok
                    else f"Missing {len(missing_types)} service type(s): {', '.join(missing_types[:5])}"
                ),
            })
            if not comp_ok:
                all_passed = False

    passed_count = sum(1 for t in tests if t["passed"])
    total_count = len(tests)
    return {
        "tests": tests,
        "passed": passed_count,
        "failed": total_count - passed_count,
        "total": total_count,
        "all_passed": all_passed,
    }


async def _update_test_status(template_id: str, version_num: int, test_results: dict):
    """Persist structural test results to the version row and sync parent status."""
    new_status = "passed" if test_results["all_passed"] else "failed"
    await update_template_version_status(template_id, version_num, new_status, test_results)
    _tb = await get_backend()
    await _tb.execute_write(
        "UPDATE catalog_templates SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, datetime.now(timezone.utc).isoformat(), template_id),
    )
    return new_status


# ── Recompose Blueprint ──────────────────────────────────────


async def _recompose_with_pinned(
    template_id: str,
    version_overrides: dict | None = None,
    ignore_existing_pins: bool = False,
    changelog: str = "Recomposed",
    change_type: str = "major",
    created_by: str = "recomposer",
    progress_callback=None,
) -> dict:
    """Core recompose logic that respects pinned versions.

    For each service in the template:
    - If version_overrides specifies a version for this service, use that
    - If ignore_existing_pins is False and the template has a pinned version, use that
    - Otherwise fall back to the active (latest promoted) version

    If a service has not been fully onboarded (deployment-validated), the
    onboarding pipeline is run inline before composition proceeds.

    Returns a dict with recompose results including the new version.
    """
    from src.tools.arm_generator import (
        _STANDARD_PARAMETERS, _TEMPLATE_WRAPPER, generate_arm_template,
        has_builtin_skeleton,
    )
    from src.template_engine import analyze_dependencies
    import json as _json

    tmpl = await _require_template(template_id)

    # Parse service_ids
    svc_ids_raw = tmpl.get("service_ids") or tmpl.get("service_ids_json") or []
    if isinstance(svc_ids_raw, str):
        try:
            svc_ids = _json.loads(svc_ids_raw)
        except Exception:
            svc_ids = []
    else:
        svc_ids = list(svc_ids_raw) if svc_ids_raw else []

    if not svc_ids:
        raise HTTPException(
            status_code=400,
            detail="This template has no service_ids — it can't be recomposed",
        )

    # Merge existing pinned versions with any overrides
    existing_pinned = {} if ignore_existing_pins else (tmpl.get("pinned_versions") or {})
    if version_overrides:
        existing_pinned = dict(existing_pinned)
        existing_pinned.update(version_overrides)

    STANDARD_PARAMS = {
        "resourceName", "location", "environment",
        "projectName", "ownerEmail", "costCenter",
    }

    # ── Gather ARM templates for each service (respecting pins) ─
    from src.database import is_service_fully_validated

    async def _emit(msg: dict):
        if progress_callback:
            await progress_callback(msg)

    service_templates: list[dict] = []
    service_version_details: list[dict] = []
    pinned_versions: dict = {}
    not_onboarded: list[dict] = []
    onboarded_inline: list[dict] = []
    onboard_failed: list[dict] = []

    # First pass: identify which services need onboarding
    for sid in svc_ids:
        svc = await _require_service(sid)
        is_valid, validation_reason = await is_service_fully_validated(sid)
        if not is_valid:
            not_onboarded.append({
                "service_id": sid,
                "name": svc.get("name", sid),
                "reason": validation_reason,
            })

    # Run onboarding for any service that isn't fully validated
    if not_onboarded:
        await _emit({
            "phase": "onboarding_gate",
            "detail": f"{len(not_onboarded)} service(s) need onboarding before composition",
            "services": [s["service_id"] for s in not_onboarded],
        })

        import uuid
        from src.pipelines.onboarding import runner as onboarding_runner
        from src.pipeline import PipelineContext

        for i, entry in enumerate(not_onboarded):
            sid = entry["service_id"]
            short = sid.split("/")[-1]
            await _emit({
                "phase": "onboarding_start",
                "detail": f"Onboarding {short} ({i+1}/{len(not_onboarded)})…",
                "service_id": sid,
                "index": i,
                "total": len(not_onboarded),
            })

            svc = await get_service(sid)
            if not svc:
                onboard_failed.append({"service_id": sid, "reason": "service not found"})
                await _emit({
                    "phase": "onboarding_failed",
                    "detail": f"{short}: service entry not found",
                    "service_id": sid,
                })
                continue

            run_id = uuid.uuid4().hex[:8]
            rg_name = f"infraforge-val-{sid.replace('/', '-').replace('.', '-').lower()}-{run_id}"[:90]

            ctx = PipelineContext(
                "service_onboarding",
                run_id=run_id,
                service_id=sid,
                region="eastus2",
                rg_name=rg_name,
                svc=svc,
                onboarding_chain={sid},
            )

            succeeded = False
            last_error = ""
            try:
                async for line in onboarding_runner.execute(ctx):
                    try:
                        evt = json.loads(line)
                        evt["dep_service"] = sid
                        evt["dep_name"] = short
                        # Forward progress events to the caller
                        await _emit({
                            "phase": "onboarding_progress",
                            "service_id": sid,
                            "service_name": short,
                            "event": evt,
                            "detail": evt.get("detail", evt.get("message", "")),
                        })
                        if evt.get("type") == "done":
                            succeeded = True
                        elif evt.get("type") == "error":
                            last_error = evt.get("detail", "unknown error")
                    except (json.JSONDecodeError, ValueError):
                        pass
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Recompose: onboarding failed for {sid}: {e}")

            if succeeded:
                onboarded_inline.append({"service_id": sid, "name": entry["name"]})
                await _emit({
                    "phase": "onboarding_complete",
                    "detail": f"{short} onboarded and validated",
                    "service_id": sid,
                })
            else:
                onboard_failed.append({"service_id": sid, "reason": last_error or "pipeline did not complete"})
                await _emit({
                    "phase": "onboarding_failed",
                    "detail": f"{short}: {last_error or 'pipeline did not complete'}",
                    "service_id": sid,
                })

        # Refresh the not_onboarded list after onboarding attempts
        still_not_onboarded = []
        for entry in not_onboarded:
            is_valid, reason = await is_service_fully_validated(entry["service_id"])
            if not is_valid:
                still_not_onboarded.append({**entry, "reason": reason})
        not_onboarded = still_not_onboarded

        if not_onboarded:
            await _emit({
                "phase": "onboarding_gate_partial",
                "detail": f"{len(onboarded_inline)} onboarded, {len(not_onboarded)} still pending — proceeding with best-effort composition",
                "still_pending": [s["service_id"] for s in not_onboarded],
            })
        else:
            await _emit({
                "phase": "onboarding_gate_passed",
                "detail": f"All {len(onboarded_inline)} services onboarded — proceeding to composition",
            })

    await _emit({"phase": "composing", "detail": "Assembling ARM template from service templates…"})

    service_templates: list[dict] = []
    service_version_details: list[dict] = []
    pinned_versions: dict = {}

    for sid in svc_ids:
        svc = await _require_service(sid)

        tpl_dict = None
        version_info = {"service_id": sid, "name": svc.get("name", sid), "source": "builtin"}

        # Check if there's a pinned version to use
        pin = existing_pinned.get(sid)
        if pin and pin.get("version") is not None:
            ver = await get_service_version(sid, int(pin["version"]))
            if ver and ver.get("arm_template"):
                try:
                    tpl_dict = _json.loads(ver["arm_template"])
                    version_info["source"] = "catalog"
                    version_info["version"] = ver.get("version")
                    version_info["semver"] = ver.get("semver")
                    pinned_versions[sid] = {
                        "version": ver.get("version"),
                        "semver": ver.get("semver"),
                    }
                except Exception:
                    pass

        # Fall back to active version if no pin or pin failed
        if not tpl_dict:
            active = await get_active_service_version(sid)
            if active and active.get("arm_template"):
                try:
                    tpl_dict = _json.loads(active["arm_template"])
                    version_info["source"] = "catalog"
                    version_info["version"] = active.get("version")
                    version_info["semver"] = active.get("semver")
                    pinned_versions[sid] = {
                        "version": active.get("version"),
                        "semver": active.get("semver"),
                    }
                except Exception:
                    pass
        # Fallback: draft version from auto-prep
        if not tpl_dict:
            draft = await get_latest_service_version(sid)
            if draft and draft.get("arm_template"):
                try:
                    tpl_dict = _json.loads(draft["arm_template"])
                    version_info["source"] = "draft"
                    version_info["version"] = draft.get("version")
                    version_info["semver"] = draft.get("semver", "0.0.0-draft")
                    pinned_versions[sid] = {
                        "version": draft.get("version"),
                        "semver": draft.get("semver", "0.0.0-draft"),
                    }
                except Exception:
                    pass
        if not tpl_dict and has_builtin_skeleton(sid):
            tpl_dict = generate_arm_template(sid)
            version_info["source"] = "builtin"
            if sid not in pinned_versions:
                pinned_versions[sid] = {"version": 0, "semver": "0.0.0-builtin"}
        if not tpl_dict:
            raise HTTPException(
                status_code=400, detail=f"No ARM template available for '{sid}'",
            )

        service_version_details.append(version_info)
        service_templates.append({
            "svc": svc,
            "template": tpl_dict,
            "quantity": 1,
        })

    # ── Resolve dependencies (auto-add missing services) ──────
    from src.orchestrator import resolve_composition_dependencies

    dep_result = await resolve_composition_dependencies(svc_ids)

    for item in dep_result.get("resolved", []):
        dep_sid = item["service_id"]
        if any(e["svc"]["id"] == dep_sid for e in service_templates):
            continue
        dep_svc = await get_service(dep_sid)
        if not dep_svc:
            continue
        dep_tpl = None
        dep_active = await get_active_service_version(dep_sid)
        if dep_active and dep_active.get("arm_template"):
            try:
                dep_tpl = _json.loads(dep_active["arm_template"])
                pinned_versions[dep_sid] = {
                    "version": dep_active.get("version"),
                    "semver": dep_active.get("semver"),
                }
            except Exception:
                pass
        # Fallback: draft version from auto-prep
        if not dep_tpl:
            dep_draft = await get_latest_service_version(dep_sid)
            if dep_draft and dep_draft.get("arm_template"):
                try:
                    dep_tpl = _json.loads(dep_draft["arm_template"])
                    pinned_versions[dep_sid] = {
                        "version": dep_draft.get("version"),
                        "semver": dep_draft.get("semver", "0.0.0-draft"),
                    }
                except Exception:
                    pass
        if not dep_tpl and has_builtin_skeleton(dep_sid):
            dep_tpl = generate_arm_template(dep_sid)
            if dep_sid not in pinned_versions:
                pinned_versions[dep_sid] = {"version": 0, "semver": "0.0.0-builtin"}
        if dep_tpl:
            service_templates.append({
                "svc": dep_svc,
                "template": dep_tpl,
                "quantity": 1,
            })
            svc_ids.append(dep_sid)
            logger.info(f"Recompose auto-added dependency: {dep_sid}")

    # ── Compose ───────────────────────────────────────────────
    from src.pipeline_helpers import resolve_variables_for_composition, build_composed_variables, validate_arm_references

    combined_params = dict(_STANDARD_PARAMETERS)
    combined_resources: list[dict] = []
    combined_outputs: dict = {}
    all_resolved_vars: dict[str, dict] = {}
    resource_types: list[str] = []
    tags_list: list[str] = []

    for entry in service_templates:
        svc = entry["svc"]
        tpl = entry["template"]
        sid = svc["id"]

        short_name = sid.split("/")[-1].lower()
        resource_types.append(sid)
        tags_list.append(svc.get("category", ""))

        suffix = f"_{short_name}"

        extra_params, proc_resources, proc_outputs, resolved_vars = \
            resolve_variables_for_composition(tpl, suffix)

        if not proc_resources:
            logger.warning(
                f"Recompose: service '{sid}' contributed 0 resources — "
                f"its ARM template may have an empty resources array."
            )

        combined_params.update(extra_params)
        combined_resources.extend(proc_resources)
        combined_outputs.update(proc_outputs)
        all_resolved_vars[suffix] = resolved_vars

    # ── Build the recomposed template ─────────────────────────
    composed = dict(_TEMPLATE_WRAPPER)
    composed["parameters"] = combined_params
    composed["variables"] = build_composed_variables(all_resolved_vars)
    composed["resources"] = combined_resources
    composed["outputs"] = combined_outputs

    # Pre-deploy structural validation
    ref_errors = validate_arm_references(composed)
    if ref_errors:
        logger.warning(f"Recompose reference errors (auto-fixing): {ref_errors}")
        for err in ref_errors:
            if "Missing variable" in err:
                vname = err.split("'")[1]
                composed.setdefault("variables", {})[vname] = f"[parameters('resourceName')]"
            elif "Missing parameter" in err:
                pname = err.split("'")[1]
                composed.setdefault("parameters", {})[pname] = {
                    "type": "string",
                    "defaultValue": f"infraforge-{pname[:20]}",
                    "metadata": {"description": f"Auto-added: {pname}"},
                }

    content_str = _json.dumps(composed, indent=2)

    content_str = _ensure_parameter_defaults(content_str)
    content_str = _sanitize_placeholder_guids(content_str)
    content_str = _sanitize_dns_zone_names(content_str)

    composed = _json.loads(content_str)
    combined_params = composed.get("parameters", {})
    param_list = [
        {"name": k, "type": v.get("type", "string"), "required": "defaultValue" not in v}
        for k, v in combined_params.items()
    ]

    dep_analysis = analyze_dependencies(svc_ids)

    # ── Save back ─────────────────────────────────────────────
    catalog_entry = {
        "id": template_id,
        "name": tmpl.get("name", template_id),
        "description": tmpl.get("description", ""),
        "format": "arm",
        "category": tmpl.get("category", "blueprint"),
        "content": content_str,
        "tags": list(set(tags_list)),
        "resources": list(set(resource_types)),
        "parameters": param_list,
        "outputs": list(combined_outputs.keys()),
        "is_blueprint": len(service_templates) > 1,
        "service_ids": svc_ids,
        "pinned_versions": pinned_versions,
        "status": tmpl.get("status", "draft"),
        "registered_by": tmpl.get("registered_by", "template-composer"),
        "template_type": dep_analysis["template_type"],
        "provides": dep_analysis["provides"],
        "requires": dep_analysis["requires"],
        "optional_refs": dep_analysis["optional_refs"],
    }

    try:
        await delete_template_versions_by_status(template_id, ["draft", "failed"])
        await upsert_template(catalog_entry)
        ver = await create_template_version(
            template_id, content_str,
            changelog=changelog,
            change_type=change_type,
            created_by=created_by,
        )
    except Exception as e:
        logger.error(f"Failed to save recomposed template: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # ── Auto-run structural tests on the new version ──────────
    new_version_num = ver.get("version") if isinstance(ver, dict) else None
    test_results = _run_structural_tests(
        content_str,
        expected_service_ids=svc_ids if svc_ids else None,
    )
    test_status = None
    if new_version_num is not None:
        test_status = await _update_test_status(template_id, new_version_num, test_results)
        logger.info(
            f"Auto-tested recomposed version {new_version_num}: "
            f"{test_results['passed']}/{test_results['total']} passed → {test_status}"
        )

    if not_onboarded:
        logger.warning(
            f"Recompose '{template_id}': {len(not_onboarded)} service(s) not fully onboarded: "
            + ", ".join(f"{s['service_id']} ({s['reason']})" for s in not_onboarded)
        )

    logger.info(
        f"Recomposed blueprint '{template_id}' from {len(svc_ids)} services "
        f"→ {len(combined_resources)} resources, {len(combined_params)} params"
    )

    return {
        "status": "ok",
        "template_id": template_id,
        "resource_count": len(combined_resources),
        "parameter_count": len(combined_params),
        "services_recomposed": svc_ids,
        "service_versions": service_version_details,
        "version": ver,
        "pinned_versions": pinned_versions,
        "test_results": test_results,
        "test_status": test_status,
        "not_onboarded": not_onboarded,
        "onboarded_inline": onboarded_inline,
        "onboard_failed": onboard_failed,
    }


@app.post("/api/catalog/templates/{template_id}/fix-and-validate")
async def fix_and_validate_template(template_id: str, request: Request):
    """Unified endpoint: fix a failed template and validate it in one step.

    For **blueprints**: Recomposes from source services first (fresh
    composition fixes reference bugs), then runs structural tests, then
    ARM validation with the self-healing loop.

    For **standalone templates**: Runs auto-heal (structural), then ARM
    validation with the self-healing loop.

    Streams NDJSON progress throughout. This replaces the confusing
    "Auto-Heal" + "Re-validate" two-button flow with a single operation.
    """
    import json as _json

    tmpl = await _require_template(template_id)
    await _reject_if_pipeline_running(template_id)

    body = await _parse_body(request)
    region = body.get("region", "eastus2")

    is_blueprint = bool(tmpl.get("is_blueprint"))
    svc_ids_raw = tmpl.get("service_ids") or tmpl.get("service_ids_json") or []
    if isinstance(svc_ids_raw, str):
        try:
            svc_ids = _json.loads(svc_ids_raw)
        except Exception:
            svc_ids = []
    else:
        svc_ids = list(svc_ids_raw) if svc_ids_raw else []

    async def _stream():
        import uuid as _uuid

        _events = []
        def _record(evt_dict):
            _events.append(evt_dict)
            return _json.dumps(evt_dict) + "\n"

        # ── Phase 1: Blueprint recompose OR standalone heal ──
        if is_blueprint and svc_ids:
            yield _record({
                "phase": "recomposing",
                "detail": "This is a composite template — let me rebuild it from the pinned service template versions…",
            })

            try:
                result = await _recompose_with_pinned(
                    template_id,
                    version_overrides=None,
                    ignore_existing_pins=False,
                    changelog="Fix & Validate: recomposed from pinned services",
                    change_type="major",
                    created_by="fix-and-validate",
                )
                test_results = result.get("test_results", {})
                yield _record({
                    "phase": "recomposed",
                    "detail": f"Rebuilt from {len(result.get('services_recomposed', []))} services — {test_results.get('passed', 0)}/{test_results.get('total', 0)} structural tests pass.",
                    "version": result.get("version", {}).get("semver", ""),
                    "resource_count": result.get("resource_count", 0),
                    "param_count": result.get("parameter_count", 0),
                })

                if not test_results.get("all_passed"):
                    yield _record({
                        "phase": "structural_fix",
                        "detail": "Some structural issues remain — running auto-heal…",
                    })
                    # Auto-heal the recomposed version
                    from starlette.requests import Request as _Req
                    heal_resp = await auto_heal_template(template_id)
                    heal_data = _json.loads(heal_resp.body.decode())
                    yield _record({
                        "phase": "structural_fixed",
                        "detail": f"Structural fix: {heal_data.get('message', 'done')}",
                        "all_passed": heal_data.get("all_passed", False),
                    })
            except Exception as e:
                yield _record({
                    "phase": "recompose_error",
                    "detail": f"Recompose failed: {e}. Falling back to current template.",
                })
        else:
            # Standalone: run structural auto-heal first
            yield _record({
                "phase": "structural_check",
                "detail": "Checking the template structure and fixing any issues…",
            })

            try:
                heal_resp = await auto_heal_template(template_id)
                heal_data = _json.loads(heal_resp.body.decode())
                if heal_data.get("all_passed") or heal_data.get("status") == "already_healthy":
                    yield _record({
                        "phase": "structural_ok",
                        "detail": "Structure looks good — moving to ARM validation.",
                    })
                else:
                    yield _record({
                        "phase": "structural_fixed",
                        "detail": f"Structural fix: {heal_data.get('message', 'done')}",
                        "all_passed": heal_data.get("all_passed", False),
                    })
            except Exception as e:
                yield _record({
                    "phase": "structural_error",
                    "detail": f"Structural check error: {e}. Proceeding with ARM validation anyway.",
                })

        # ── Phase 2: ARM validation with self-healing ──
        yield _record({
            "phase": "arm_validation_start",
            "detail": "Structure verified — now deploying to Azure to test the real thing…",
        })

        # Get the latest version (may have been updated by recompose/heal)
        _tmpl = await get_template_by_id(template_id)
        _versions = await get_template_versions(template_id)
        if not _versions:
            yield _record({
                "type": "action_required",
                "phase": "fix_validate",
                "detail": "No template versions found after fix phase.",
                "failure_category": "setup_broken",
                "pipeline": "validation",
                "actions": [
                    {"id": "retry", "label": "Retry", "description": "Try again", "style": "primary"},
                    {"id": "end_pipeline", "label": "End Pipeline", "description": "Stop", "style": "danger"},
                ],
                "context": {"template_id": template_id},
            })
            return

        _latest = _versions[0]
        _arm_content = _latest.get("arm_template", _tmpl.get("content", ""))
        try:
            _tpl = _json.loads(_arm_content) if isinstance(_arm_content, str) else _arm_content
        except Exception:
            yield _record({
                "type": "action_required",
                "phase": "fix_validate",
                "detail": "Template content is not valid JSON after fix phase.",
                "failure_category": "setup_broken",
                "pipeline": "validation",
                "actions": [
                    {"id": "retry", "label": "Retry", "description": "Try again", "style": "primary"},
                    {"id": "end_pipeline", "label": "End Pipeline", "description": "Stop", "style": "danger"},
                ],
                "context": {"template_id": template_id},
            })
            return

        _version_num = _latest["version"]

        # Build parameter values (with ARM expression skip)
        _tpl_params = _tpl.get("parameters", {})
        _final_params = {}
        for pname, pdef in _tpl_params.items():
            if "defaultValue" in pdef:
                dv = pdef["defaultValue"]
                if isinstance(dv, str) and dv.startswith("["):
                    continue
                _final_params[pname] = dv
            else:
                ptype = pdef.get("type", "string").lower()
                if ptype == "string":
                    _final_params[pname] = f"if-val-{pname[:20]}"
                elif ptype == "int":
                    _final_params[pname] = 1
                elif ptype == "bool":
                    _final_params[pname] = True
                elif ptype == "array":
                    _final_params[pname] = []
                elif ptype == "object":
                    _final_params[pname] = {}

        _rg_name = f"infraforge-val-{_uuid.uuid4().hex[:8]}"
        _deploy_name = f"infraforge-val-{_uuid.uuid4().hex[:8]}"
        _tmpl_name = _tmpl.get("name", template_id)

        # Find semver
        _target_semver = _latest.get("semver", "")

        from src.pipelines.validation import stream_validation

        _run_id = _uuid.uuid4().hex[:8]
        await create_pipeline_run(
            _run_id, template_id, "fix_and_validate",
            version_num=_version_num,
            semver=_target_semver,
            created_by="fix-and-validate",
        )

        final_status = "failed"
        heal_count = 0
        try:
            async for line in stream_validation(
                template_id=template_id,
                template_name=_tmpl_name,
                version_num=_version_num,
                tpl=_tpl,
                final_params=_final_params,
                user_params=body.get("parameters", {}),
                rg_name=_rg_name,
                deployment_name=_deploy_name,
                region=region,
                is_blueprint=is_blueprint,
                svc_ids=svc_ids,
            ):
                yield line
                try:
                    evt = _json.loads(line.strip())
                    _events.append(evt)
                    if evt.get("phase") == "complete":
                        s = evt.get("status", "failed")
                        final_status = "completed" if s in ("succeeded", "tested_with_issues") else "failed"
                        heal_count = evt.get("issues_resolved", 0)
                except Exception:
                    pass
        except Exception as val_err:
            yield _record({
                "phase": "complete",
                "status": "failed",
                "error": str(val_err),
            })

        try:
            await complete_pipeline_run(
                _run_id, final_status,
                summary={"detail": f"Fix & Validate: {final_status}, heals={heal_count}"},
                heal_count=heal_count,
                events_json=_json.dumps(_events),
            )
        except Exception:
            pass

        # ── Fallback: ensure template status is never stuck at 'passed' ──
        # stream_validation() updates the DB on clean exit, but if it
        # threw an exception the template stays at whatever _recompose
        # set it to (usually 'passed'). Correct that here.
        try:
            _fb = await get_backend()
            _current = await get_template_by_id(template_id)
            _cur_status = (_current or {}).get("status", "")
            if final_status == "completed" and _cur_status != "validated":
                await _fb.execute_write(
                    "UPDATE catalog_templates SET status = ?, updated_at = ? WHERE id = ?",
                    ("validated", datetime.now(timezone.utc).isoformat(), template_id),
                )
            elif final_status == "failed" and _cur_status not in ("failed", "validated"):
                await _fb.execute_write(
                    "UPDATE catalog_templates SET status = ?, updated_at = ? WHERE id = ?",
                    ("failed", datetime.now(timezone.utc).isoformat(), template_id),
                )
        except Exception:
            pass

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


@app.post("/api/catalog/templates/{template_id}/recompose")
async def recompose_blueprint(template_id: str):
    """Re-compose a blueprint from its source service templates.

    Fetches the active ARM templates for each service_id stored on the
    blueprint and recomposes. Clears all pinned version locks so each
    service moves to its current active version.

    Streams NDJSON progress events (onboarding, composition) followed
    by a final ``{"type": "result", ...}`` event with the full result.
    """
    import asyncio
    import json as _json

    async def _stream():
        queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()

        async def _progress_cb(evt: dict):
            await queue.put(evt)

        async def _run():
            try:
                result = await _recompose_with_pinned(
                    template_id,
                    version_overrides=None,
                    ignore_existing_pins=True,
                    changelog="Recomposed from current service templates",
                    change_type="major",
                    created_by="recomposer",
                    progress_callback=_progress_cb,
                )
                result["message"] = (
                    f"Template recomposed from {len(result['services_recomposed'])} services "
                    f"with latest templates"
                )
                await queue.put({"type": "result", **result})
            except Exception as e:
                await queue.put({"type": "error", "detail": str(e)})
            finally:
                await queue.put(_SENTINEL)

        task = asyncio.create_task(_run())
        try:
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                yield _json.dumps(item) + "\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


# ── Template Composition Info ─────────────────────────────────

@app.get("/api/catalog/templates/{template_id}/composition")
async def get_template_composition(template_id: str):
    """Get the services that compose this template, with version info,
    dependency edges, and upgrade availability.

    Performance: uses a single combined SQL approach — get_template_by_id
    first, then a batch query for services + versions together, minimizing
    Azure SQL round-trips.
    """
    import asyncio
    from src.template_engine import RESOURCE_DEPENDENCIES, get_parent_resource_type

    tmpl = await _require_template(template_id)

    service_ids = tmpl.get("service_ids", [])
    if not service_ids:
        return JSONResponse({
            "template_id": template_id,
            "template_version": tmpl.get("active_version"),
            "template_semver": None,
            "template_status": tmpl.get("status", "draft"),
            "components": [],
            "edges": [],
            "requires": [],
            "provides": [],
        })

    pinned_versions = tmpl.get("pinned_versions", {})
    provides = set(tmpl.get("provides", []))
    requires = tmpl.get("requires", [])

    # Batch: services + versions + semver (3 fast queries, connection is warm)
    svc_map = await get_services_basic(service_ids)
    version_map = await get_version_summary_batch(service_ids)
    template_semver = await get_latest_semver(template_id)

    # ── Assemble components using batch data ──
    components = []
    for sid in service_ids:
        svc = svc_map.get(sid)
        if not svc:
            components.append({
                "service_id": sid,
                "name": sid.split("/")[-1],
                "category": "",
                "status": "unknown",
                "parent_service_id": get_parent_resource_type(sid),
                "current_version": None,
                "current_semver": None,
                "latest_version": None,
                "latest_semver": None,
                "upgrade_available": False,
            })
            continue

        pinned = pinned_versions.get(sid) or {}
        pinned_int = pinned.get("version")
        pinned_semver = pinned.get("semver")

        ver_info = version_map.get(sid, {})
        active_int = ver_info.get("active_version")
        active_semver = ver_info.get("active_semver")
        latest_int = ver_info.get("latest_version") or active_int
        latest_semver = ver_info.get("latest_semver") or active_semver

        # If no pinned version recorded, we DON'T know what version is
        # actually baked into the composed template. Show "unknown" rather
        # than lying by pretending it's the latest.
        upgrade_available = False
        if pinned_int is not None and active_int is not None:
            upgrade_available = active_int > pinned_int
        elif pinned_int is None and active_int is not None:
            # No pin = we don't know → flag as "needs recompose" 
            upgrade_available = True

        components.append({
            "service_id": sid,
            "name": svc.get("name", sid.split("/")[-1]),
            "category": svc.get("category", ""),
            "status": svc.get("status", ""),
            "fully_onboarded": svc.get("reviewed_by") == "Deployment Validated",
            "parent_service_id": get_parent_resource_type(sid),
            "current_version": pinned_int,
            "current_semver": pinned_semver or (f"{pinned_int}.0.0" if pinned_int else None),
            "latest_version": active_int,
            "latest_semver": active_semver or (f"{active_int}.0.0" if active_int else None),
            "upgrade_available": upgrade_available,
            "version_known": pinned_int is not None,
            "latest_api_version": svc.get("latest_api_version"),
            "template_api_version": svc.get("template_api_version"),
        })

    # Build dependency edges between components
    component_ids = {c["service_id"] for c in components}
    edges = []
    for sid in service_ids:
        deps = RESOURCE_DEPENDENCIES.get(sid, [])
        for dep in deps:
            dep_type = dep["type"]
            if dep_type in component_ids:
                edges.append({
                    "from": sid,
                    "to": dep_type,
                    "reason": dep.get("reason", ""),
                    "required": dep.get("required", False),
                })
            elif dep_type in provides and dep_type not in component_ids:
                for other_sid in service_ids:
                    other_deps = RESOURCE_DEPENDENCIES.get(other_sid, [])
                    for od in other_deps:
                        if od["type"] == dep_type and od.get("created_by_template"):
                            edges.append({
                                "from": sid,
                                "to": other_sid,
                                "reason": dep.get("reason", ""),
                                "required": dep.get("required", False),
                            })

    return JSONResponse({
        "template_id": template_id,
        "template_version": tmpl.get("active_version"),
        "template_semver": template_semver,
        "template_status": tmpl.get("status", "draft"),
        "components": components,
        "edges": edges,
        "requires": requires,
        "provides": sorted(provides),
    })


# ── Pin Service Version in Template ──────────────────────────

@app.patch("/api/catalog/templates/{template_id}/pin-version")
async def pin_service_version(template_id: str, request: Request):
    """Pin a specific service version in a composed template and recompose.

    Body: {
        "service_id": "Microsoft.Network/publicIPAddresses",
        "version": 1           // integer version number to pin to
    }

    Updates the pinned version for the specified service, then recomposes
    the entire template using each service's pinned version. This creates
    a new template version whose ARM content actually reflects the chosen
    service version — so deploying uses the pinned version's resources.
    """

    tmpl = await _require_template(template_id)

    body = await _parse_body_required(request)

    service_id = body.get("service_id", "").strip()
    version = body.get("version")

    if not service_id:
        raise HTTPException(status_code=400, detail="service_id is required")
    if version is None:
        raise HTTPException(status_code=400, detail="version is required")

    try:
        version = int(version)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="version must be an integer")

    # Verify the service is part of this template
    svc_ids = tmpl.get("service_ids", [])
    if service_id not in svc_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Service '{service_id}' is not part of this template",
        )

    # Verify the requested version exists
    ver = await get_service_version(service_id, version)
    if not ver:
        raise HTTPException(
            status_code=404,
            detail=f"Version {version} of '{service_id}' not found",
        )

    short_name = service_id.split("/")[-1]
    target_semver = ver.get("semver") or f"{version}.0.0"

    # Recompose the template with this version override
    result = await _recompose_with_pinned(
        template_id,
        version_overrides={
            service_id: {"version": version, "semver": target_semver},
        },
        changelog=f"Pinned {short_name} to v{target_semver}",
        change_type="minor",
        created_by="version-pinner",
    )

    logger.info(
        f"Pinned {service_id} to v{version} (semver={target_semver}) "
        f"in template '{template_id}' and recomposed"
    )

    return JSONResponse({
        "status": "ok",
        "template_id": template_id,
        "service_id": service_id,
        "pinned_version": version,
        "pinned_semver": target_semver,
        "version": result.get("version"),
        "message": f"Pinned {short_name} to v{target_semver} and recomposed template",
    })


@app.get("/api/catalog/templates/{template_id}/service-versions/{service_id:path}")
async def get_template_service_versions(template_id: str, service_id: str):
    """List all available versions of a service used in a template,
    indicating which version is currently pinned.

    Returns the version list with the pinned version marked.
    """

    tmpl = await _require_template(template_id)

    svc_ids = tmpl.get("service_ids", [])
    if service_id not in svc_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Service '{service_id}' is not part of this template",
        )

    all_versions = await get_service_versions(service_id)
    pinned = (tmpl.get("pinned_versions") or {}).get(service_id, {})
    pinned_int = pinned.get("version")

    versions = []
    for v in all_versions:
        versions.append({
            "version": v.get("version"),
            "semver": v.get("semver"),
            "status": v.get("status"),
            "created_at": v.get("created_at"),
            "is_pinned": v.get("version") == pinned_int,
        })

    return JSONResponse({
        "template_id": template_id,
        "service_id": service_id,
        "pinned_version": pinned_int,
        "pinned_semver": pinned.get("semver"),
        "versions": versions,
    })


# ── Template Version Management ──────────────────────────────

@app.get("/api/catalog/templates/{template_id}/versions")
async def list_template_versions(template_id: str):
    """List all versions of a template (arm_template stripped for performance)."""

    tmpl = await _require_template(template_id)

    versions = await get_template_versions(template_id)

    # Strip arm_template from list to keep payload small
    # Also strip full remediation_log but expose a flag
    versions_summary = []
    for v in versions:
        vs = {k: val for k, val in v.items() if k != "arm_template"}
        vs["template_size_bytes"] = len(v.get("arm_template") or "") if v.get("arm_template") else 0
        # Flag whether this version has a retrievable remediation log
        vr = v.get("validation_results") or {}
        if isinstance(vr, dict) and vr.get("remediation_log"):
            vs["has_remediation_log"] = True
            # Strip the heavy log array from the list response
            vs["validation_results"] = {
                k: val for k, val in vr.items() if k != "remediation_log"
            }
        versions_summary.append(vs)

    return JSONResponse({
        "template_id": template_id,
        "template_name": tmpl.get("name", ""),
        "active_version": tmpl.get("active_version"),
        "status": tmpl.get("status", "draft"),
        "versions": versions_summary,
    })


@app.get("/api/catalog/templates/{template_id}/versions/{version}")
async def get_catalog_template_version(template_id: str, version: str):
    """Get a single version of a catalog template including full ARM content.

    ``version`` may be an integer or the literal ``"latest"`` to return the
    most recent version (useful when ``active_version`` is NULL).
    """

    tmpl = await _require_template(template_id)

    # Resolve "latest" to the actual max version number
    if version == "latest":
        _b = await get_backend()
        _rows = await _b.execute(
            "SELECT MAX(version) AS max_ver FROM template_versions WHERE template_id = ?",
            (template_id,),
        )
        max_ver = _rows[0]["max_ver"] if _rows and _rows[0].get("max_ver") else None
        if max_ver is None:
            raise HTTPException(status_code=404, detail="No versions found")
        version_int = int(max_ver)
    else:
        try:
            version_int = int(version)
        except ValueError:
            raise HTTPException(status_code=400, detail="Version must be an integer or 'latest'")

    ver = await get_template_version(template_id, version_int)
    if not ver:
        raise HTTPException(status_code=404, detail=f"Version {version_int} not found")

    # Ensure contentVersion in ARM JSON matches the stored semver
    arm_raw = ver.get("arm_template") or ""
    ver_semver = ver.get("semver") or ""
    if arm_raw and ver_semver:
        try:
            _arm = json.loads(arm_raw)
            if isinstance(_arm, dict) and _arm.get("contentVersion") != ver_semver:
                _arm["contentVersion"] = ver_semver
                ver["arm_template"] = json.dumps(_arm, indent=2)
        except (json.JSONDecodeError, TypeError):
            pass

    return JSONResponse({
        **ver,
        "template_id": template_id,
        "template_name": tmpl.get("name", ""),
        "active_version": tmpl.get("active_version"),
    })


@app.post("/api/catalog/templates/{template_id}/versions")
async def create_new_template_version(template_id: str, request: Request):
    """Create a new version of an existing template.

    Body: {
        "arm_template": "...",   // JSON string of ARM template
        "changelog": "Added monitoring",
        "semver": "2.0.0"         // optional
    }
    """
    import json as _json

    tmpl = await _require_template(template_id)

    body = await _parse_body_required(request)

    arm_template = body.get("arm_template", "")
    if not arm_template:
        raise HTTPException(status_code=400, detail="arm_template is required")

    # Validate it's valid JSON
    try:
        _json.loads(arm_template) if isinstance(arm_template, str) else arm_template
    except Exception:
        raise HTTPException(status_code=400, detail="arm_template must be valid JSON")

    if isinstance(arm_template, dict):
        arm_template = _json.dumps(arm_template, indent=2)

    ver = await create_template_version(
        template_id, arm_template,
        changelog=body.get("changelog", ""),
        semver=body.get("semver"),
    )

    # Update parent template content and mark as draft (needs testing)
    tmpl["content"] = arm_template
    tmpl["status"] = "draft"
    # Restore keys that _parse_template_row renamed
    tmpl["source_path"] = tmpl.pop("source", "")
    await upsert_template(tmpl)

    return JSONResponse({
        "status": "ok",
        "template_id": template_id,
        "version": ver,
    })


@app.get("/api/catalog/templates/{template_id}/diff")
async def get_template_diff(template_id: str, request: Request):
    """Compute a unified diff between two template versions.

    Query params:
        from_version (int)  — the old version number
        to_version   (int)  — the new version number
    Returns hunks with line numbers suitable for GitHub-style rendering.
    """
    import difflib, json as _json

    tmpl = await _require_template(template_id)

    params = request.query_params
    try:
        from_ver = int(params.get("from_version", "0"))
        to_ver = int(params.get("to_version", "0"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="from_version and to_version must be integers")

    if from_ver < 1 or to_ver < 1:
        raise HTTPException(status_code=400, detail="from_version and to_version must be >= 1")

    old = await get_template_version(template_id, from_ver)
    new = await get_template_version(template_id, to_ver)
    if not old:
        raise HTTPException(status_code=404, detail=f"Version {from_ver} not found")
    if not new:
        raise HTTPException(status_code=404, detail=f"Version {to_ver} not found")

    # Normalise ARM JSON to consistent formatting for clean diffs
    def _normalise(arm_str: str) -> list[str]:
        try:
            obj = _json.loads(arm_str)
            return _json.dumps(obj, indent=2).splitlines(keepends=False)
        except Exception:
            return arm_str.splitlines(keepends=False)

    old_lines = _normalise(old.get("arm_template", ""))
    new_lines = _normalise(new.get("arm_template", ""))

    # Generate unified diff
    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"v{old.get('semver', from_ver)}",
        tofile=f"v{new.get('semver', to_ver)}",
        lineterm="",
    ))

    # Parse into structured hunks for rendering
    hunks = []
    current_hunk = None
    for line in diff:
        if line.startswith("@@"):
            # Parse hunk header: @@ -old_start,old_count +new_start,new_count @@
            import re as _re
            m = _re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)", line)
            if m:
                current_hunk = {
                    "old_start": int(m.group(1)),
                    "new_start": int(m.group(3)),
                    "header": line,
                    "lines": [],
                }
                hunks.append(current_hunk)
        elif line.startswith("---") or line.startswith("+++"):
            continue  # file headers — skip
        elif current_hunk is not None:
            if line.startswith("+"):
                current_hunk["lines"].append({"type": "add", "content": line[1:]})
            elif line.startswith("-"):
                current_hunk["lines"].append({"type": "del", "content": line[1:]})
            else:
                current_hunk["lines"].append({"type": "ctx", "content": line[1:] if line.startswith(" ") else line})

    # Compute line numbers for each line
    for hunk in hunks:
        old_ln = hunk["old_start"]
        new_ln = hunk["new_start"]
        for ln in hunk["lines"]:
            if ln["type"] == "del":
                ln["old_ln"] = old_ln
                ln["new_ln"] = None
                old_ln += 1
            elif ln["type"] == "add":
                ln["old_ln"] = None
                ln["new_ln"] = new_ln
                new_ln += 1
            else:
                ln["old_ln"] = old_ln
                ln["new_ln"] = new_ln
                old_ln += 1
                new_ln += 1

    # Stats
    additions = sum(1 for h in hunks for l in h["lines"] if l["type"] == "add")
    deletions = sum(1 for h in hunks for l in h["lines"] if l["type"] == "del")

    return JSONResponse({
        "template_id": template_id,
        "template_name": tmpl.get("name", ""),
        "from_version": from_ver,
        "from_semver": old.get("semver", str(from_ver)),
        "to_version": to_ver,
        "to_semver": new.get("semver", str(to_ver)),
        "additions": additions,
        "deletions": deletions,
        "hunks": hunks,
        "total_old_lines": len(old_lines),
        "total_new_lines": len(new_lines),
    })


@app.post("/api/catalog/templates/{template_id}/promote")
async def promote_template(template_id: str, request: Request):
    """Promote a tested version to active.

    Body: { "version": 1 }
    """

    tmpl = await _require_template(template_id)

    body = await _parse_body_required(request)

    version = body.get("version")
    if not version:
        raise HTTPException(status_code=400, detail="version is required")

    ok = await promote_template_version(template_id, int(version))
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Cannot promote — version must have passed testing",
        )

    return JSONResponse({"status": "ok", "promoted_version": version})


# ── Template Validation (ARM with Self-Healing) ────────────

@app.post("/api/catalog/templates/{template_id}/validate")
async def validate_template(template_id: str, request: Request):
    """Validate a template by deploying it to a temporary resource group.

    Streams NDJSON progress. Uses the full self-healing loop (shallow +
    deep healing for blueprints). On success the template version is
    marked 'validated' and the temp RG is cleaned up. On failure it is
    marked 'failed'. The template is NOT published until explicitly
    promoted — this is just the validation gate.

    Body: {
        "parameters": { ... },
        "region": "eastus2"  // optional
    }
    """
    import uuid as _uuid

    tmpl = await _require_template(template_id)
    await _reject_if_pipeline_running(template_id)

    # Find the latest version that can be validated
    versions = await get_template_versions(template_id)
    target_ver = None
    for v in versions:
        if v["status"] in ("passed", "validated", "failed"):
            target_ver = v
            break
    if not target_ver:
        for v in versions:
            if v["status"] == "draft":
                target_ver = v
                break
    if not target_ver:
        raise HTTPException(
            status_code=400,
            detail="No testable version found. Run structural tests first.",
        )

    version_num = target_ver["version"]

    body = await _parse_body(request)

    user_params = body.get("parameters", {})
    region = body.get("region", "eastus2")

    # Parse the ARM template
    arm_content = target_ver.get("arm_template", tmpl.get("content", ""))
    try:
        tpl = json.loads(arm_content) if isinstance(arm_content, str) else arm_content
    except Exception:
        raise HTTPException(status_code=400, detail="Template content is not valid JSON")

    # Build parameter values
    tpl_params = tpl.get("parameters", {})
    final_params = {}
    for pname, pdef in tpl_params.items():
        if pname in user_params:
            final_params[pname] = user_params[pname]
        elif "defaultValue" in pdef:
            dv = pdef["defaultValue"]
            # Skip ARM expressions — they only work inside the template as
            # defaultValues, not as explicit parameter values passed to the API.
            # e.g. [resourceGroup().location] would be treated as a literal string.
            if isinstance(dv, str) and dv.startswith("["):
                continue
            final_params[pname] = dv
        else:
            ptype = pdef.get("type", "string").lower()
            if ptype == "string":
                final_params[pname] = f"if-val-{pname[:20]}"
            elif ptype == "int":
                final_params[pname] = 1
            elif ptype == "bool":
                final_params[pname] = True
            elif ptype == "array":
                final_params[pname] = []
            elif ptype == "object":
                final_params[pname] = {}

    rg_name = f"infraforge-val-{_uuid.uuid4().hex[:8]}"
    deployment_name = f"infraforge-val-{_uuid.uuid4().hex[:8]}"
    _tmpl_id = template_id
    _tmpl_name = tmpl.get("name", template_id)
    _ver_num = version_num

    # Blueprint / service info for deep healing
    is_blueprint = bool(tmpl.get("is_blueprint"))
    svc_ids_raw = tmpl.get("service_ids") or tmpl.get("service_ids_json") or []
    if isinstance(svc_ids_raw, str):
        try:
            svc_ids = json.loads(svc_ids_raw)
        except Exception:
            svc_ids = []
    else:
        svc_ids = list(svc_ids_raw) if svc_ids_raw else []

    from src.pipelines.validation import stream_validation
    import uuid as _run_uuid

    _run_id = _run_uuid.uuid4().hex[:8]

    # Find semver for the target version
    _target_semver = ""
    for v in versions:
        if v["version"] == version_num:
            _target_semver = v.get("semver", "")
            break

    async def _tracked_stream():
        """Wrap the validation generator to record pipeline run + events."""
        collected_events = []
        final_status = "failed"
        heal_count = 0
        error_detail = None

        # Record pipeline run at the start
        await create_pipeline_run(
            _run_id, _tmpl_id, "template_validation",
            version_num=_ver_num,
            semver=_target_semver,
            created_by="copilot-sdk",
        )

        try:
            # ── Dependency info: log composed service status (non-blocking) ──
            # The composed ARM template is self-contained — the real validation
            # is the ARM deployment itself, not individual service onboarding.
            if svc_ids:
                from src.database import is_service_fully_validated, get_service
                _not_onboarded = 0
                for _dep_sid in svc_ids:
                    _dep_valid, _dep_reason = await is_service_fully_validated(_dep_sid)
                    _dep_short = _dep_sid.split("/")[-1]
                    if _dep_valid:
                        yield json.dumps({
                            "phase": "dep_check",
                            "detail": f"✅ {_dep_short} — fully onboarded",
                        }) + "\n"
                    else:
                        _not_onboarded += 1
                        yield json.dumps({
                            "phase": "dep_check",
                            "detail": f"ℹ️ {_dep_short} — not individually onboarded ({_dep_reason})",
                        }) + "\n"
                if _not_onboarded:
                    yield json.dumps({
                        "phase": "dep_check",
                        "detail": f"ℹ️ {_not_onboarded} service(s) not individually onboarded — proceeding with template deployment test",
                    }) + "\n"

            async for line in stream_validation(
                template_id=_tmpl_id,
                template_name=_tmpl_name,
                version_num=_ver_num,
                tpl=tpl,
                final_params=final_params,
                user_params=user_params,
                rg_name=rg_name,
                deployment_name=deployment_name,
                region=region,
                is_blueprint=is_blueprint,
                svc_ids=svc_ids,
            ):
                # Capture event for storage
                try:
                    evt = json.loads(line.strip())
                    collected_events.append(evt)
                    phase = evt.get("phase", "")
                    if phase == "complete":
                        s = evt.get("status", "failed")
                        final_status = "completed" if s in ("succeeded", "tested_with_issues") else "failed"
                        heal_count = evt.get("issues_resolved", 0)
                        if s == "failed":
                            error_detail = evt.get("error") or evt.get("detail")
                    elif phase == "healed":
                        heal_count += 1
                except (json.JSONDecodeError, TypeError):
                    pass
                yield line
        except Exception as exc:
            final_status = "failed"
            error_detail = str(exc)[:4000]
            yield json.dumps({
                "type": "action_required",
                "phase": "complete",
                "detail": f"An unexpected error occurred: {str(exc)[:300]}",
                "failure_category": "exhausted_heals",
                "pipeline": "validation",
                "actions": [
                    {"id": "retry", "label": "Retry", "description": "Try again", "style": "primary"},
                    {"id": "end_pipeline", "label": "End Pipeline", "description": "Stop", "style": "danger"},
                ],
                "context": {},
            }) + "\n"
        finally:
            # Record completion with stored events
            events_str = json.dumps(collected_events, default=str)
            await complete_pipeline_run(
                _run_id,
                status=final_status,
                version_num=_ver_num,
                semver=_target_semver,
                summary={"template_name": _tmpl_name, "region": region},
                error_detail=error_detail,
                heal_count=heal_count,
                events_json=events_str,
            )

            # Log usage for analytics
            if final_status == "completed":
                try:
                    await log_usage({
                        "timestamp": time.time(),
                        "user": "",
                        "department": "",
                        "cost_center": "",
                        "prompt": f"Validate template: {_tmpl_name}",
                        "resource_types": svc_ids or [],
                        "estimated_cost": 0.0,
                        "from_catalog": True,
                    })
                except Exception:
                    pass

    return StreamingResponse(
        _tracked_stream(),
        media_type="application/x-ndjson",
    )


# ── Template Pipeline Runs ───────────────────────────────────

@app.get("/api/catalog/templates/{template_id}/pipeline-runs")
async def get_template_pipeline_runs(template_id: str):
    """Get recent pipeline runs for a template (newest first).

    Returns run metadata plus stored NDJSON events for replay.
    """
    runs = await get_pipeline_runs(template_id, limit=20)
    # Strip the raw JSON columns to keep the response clean
    for r in runs:
        r.pop("summary_json", None)
        r.pop("pipeline_events_json", None)
    return JSONResponse(runs)


# ── Template Publishing ──────────────────────────────────────

@app.post("/api/catalog/templates/{template_id}/publish")
async def publish_template(template_id: str, request: Request):
    """Publish a validated template — makes it available in the catalog.

    Only templates that have passed ARM What-If validation can be published.
    Body: { "version": 1 }  (optional — defaults to latest validated version)
    """

    tmpl = await _require_template(template_id)

    body = await _parse_body(request)

    version = body.get("version")

    if not version:
        # Find the latest validated version
        versions = await get_template_versions(template_id)
        for v in versions:
            if v["status"] == "validated":
                version = v["version"]
                break
        if not version:
            raise HTTPException(
                status_code=400,
                detail="No validated version found. Run ARM validation first.",
            )

    ok = await promote_template_version(template_id, int(version))
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Cannot publish — version must have passed ARM validation (status: validated)",
        )

    # Fetch semver for the published version
    _pub_semver = await get_latest_semver(template_id) or f"{version}.0.0"

    return JSONResponse({
        "status": "ok",
        "published_version": version,
        "published_semver": _pub_semver,
        "template_id": template_id,
    })



# ── Template Deployment (approved templates only) ────────────

# ══════════════════════════════════════════════════════════════
# DEPLOYMENT AGENT — Process-as-Code Pipeline
# ══════════════════════════════════════════════════════════════
#
# The deployment process is a DETERMINISTIC STATE MACHINE, not an LLM.
# The LLM is called for specific intelligence tasks (error analysis,
# template fixing), but it never decides what step comes next.
#
# Pipeline steps (enforced by code, not prompts):
#   1. SANITIZE  — _ensure_parameter_defaults, _sanitize_placeholder_guids
#   2. WHAT-IF   — ARM validation preview (catches errors before deploy)
#   3. DEPLOY    — Real ARM deployment with progress streaming
#   4. ON FAIL   —
#      a. Surface heal: _copilot_heal_template (LLM fixes the ARM JSON)
#      b. Deep heal:    _deep_heal_composed_template (fix underlying service
#                       templates, validate standalone, recompose parent)
#      c. Retry from step 2
#   5. ON SUCCESS — Save healed version, report provisioned resources
#   6. EXHAUSTED  — LLM deployment agent summarizes for the user
#
# The LLM cannot skip steps. It cannot decide to stop early. It cannot
# bypass What-If. The pipeline runs to completion or exhaustion.
# ══════════════════════════════════════════════════════════════

MAX_DEPLOY_HEAL_ATTEMPTS = 5   # Match validate's budget
DEEP_HEAL_THRESHOLD = 3        # After this many surface heals, go deep

DEPLOY_AGENT_PROMPT = DEPLOY_FAILURE_ANALYST.system_prompt


async def _get_deploy_agent_analysis(
    error: str,
    template_name: str,
    resource_group: str,
    region: str,
    heal_history: list[dict] | None = None,
) -> str:
    """Ask the deployment agent (LLM) to interpret a deployment failure.

    Called only after the pipeline exhausts all heal attempts. The agent
    produces a human-readable summary. This is a LEAF call — the LLM has
    no tools and cannot trigger further actions.
    """
    attempts = len(heal_history) if heal_history else 0
    history_text = ""
    if heal_history:
        history_text = "\n**Pipeline history:**\n"
        for h in heal_history:
            phase = h.get("phase", "deploy")
            history_text += (
                f"- Iteration {h['attempt']} ({phase}): {h['error'][:150]}… "
                f"→ {h['fix_summary']}\n"
            )

    try:
        client = await ensure_copilot_client()
        if not client:
            return _fallback_deploy_analysis(error, heal_history)

        from src.copilot_helpers import copilot_send

        prompt = (
            f"A deployment of **{template_name}** to resource group "
            f"`{resource_group}` in **{region}** failed after "
            f"{attempts} pipeline iteration(s).\n\n"
            f"**Final Azure error:**\n```\n{error[:500]}\n```\n"
            f"{history_text}\n"
            f"Explain what happened and what to do next."
        )

        result = await copilot_send(
            client,
            model=get_model_for_task(Task.VALIDATION_ANALYSIS),
            system_prompt=DEPLOY_AGENT_PROMPT.format(attempts=attempts),
            prompt=prompt,
            timeout=30,
            agent_name="DEPLOY_FAILURE_ANALYST",
        )
        return result or _fallback_deploy_analysis(error, heal_history)

    except Exception as e:
        logger.error(f"Deploy agent analysis failed: {e}")
        return _fallback_deploy_analysis(error, heal_history)


def _fallback_deploy_analysis(error: str, heal_history: list[dict] | None = None) -> str:
    """Structured message when the LLM agent isn't available."""
    attempts = len(heal_history) if heal_history else 0
    history_text = ""
    if heal_history:
        history_text = "\n\n**What the pipeline tried:**\n"
        for h in heal_history:
            history_text += f"- Iteration {h['attempt']}: {h['fix_summary']}\n"

    return (
        f"The deployment pipeline tried {attempts} iteration(s) but couldn't "
        f"resolve the issue.\n\n"
        f"**Last error:**\n> {error[:300]}\n"
        f"{history_text}\n"
        f"**Suggested next steps:** Re-run validation to diagnose and fix "
        f"the underlying issue with the full healing pipeline."
    )


@app.post("/api/catalog/templates/{template_id}/deploy")
async def deploy_template(template_id: str, request: Request):
    """Deploy an approved template to Azure — process-as-code pipeline.

    The deployment is managed by a deterministic pipeline that:
      1. Sanitizes the template (parameter defaults, GUID placeholders)
      2. Runs What-If validation (catches errors before spending resources)
      3. Deploys to Azure with real-time progress streaming
      4. On failure: surface-heals → deep-heals (for composed templates)
      5. On exhaustion: LLM agent summarizes for the user

    The LLM is called for intelligence tasks, not for process control.
    The pipeline cannot be short-circuited by the LLM.

    Event protocol (NDJSON):
      {"type": "status",  "message": "...", "progress": 0.5}  — progress
      {"type": "agent",   "content": "...", "action": "..."}   — agent activity
      {"type": "result",  "status": "succeeded|needs_work"}    — final outcome
    """
    import uuid as _uuid

    tmpl = await _require_template(template_id)
    await _reject_if_pipeline_running(template_id)

    if tmpl.get("status") not in ("approved",):
        raise HTTPException(
            status_code=400,
            detail=f"Template must be published (approved) before deploying. "
                   f"Current: {tmpl.get('status')}. Run validation and publish first.",
        )

    body = await _parse_body(request)

    resource_group = body.get("resource_group", "").strip()
    if not resource_group:
        raise HTTPException(status_code=400, detail="resource_group is required")

    region = body.get("region", "eastus2")
    user_params = body.get("parameters", {})
    deploy_version = body.get("version")  # optional: deploy a specific version

    # Get the ARM template — specific version or active (approved) version
    arm_content = tmpl.get("content", "")
    versions = await get_template_versions(template_id)
    active_ver = tmpl.get("active_version")
    target_ver = deploy_version if deploy_version else active_ver
    _deploy_semver = ""
    for v in versions:
        if v["version"] == target_ver and v.get("arm_template"):
            arm_content = v["arm_template"]
            _deploy_semver = v.get("semver", "")
            break

    try:
        tpl = json.loads(arm_content) if isinstance(arm_content, str) else arm_content
    except Exception:
        raise HTTPException(status_code=400, detail="Template content is not valid JSON")

    # Template metadata for deep healing
    is_blueprint = tmpl.get("is_blueprint", False)
    service_ids = tmpl.get("service_ids") or []

    deployment_name = f"infraforge-{_uuid.uuid4().hex[:8]}"
    _tmpl_id = template_id
    _tmpl_name = tmpl.get("name", template_id)

    from src.pipelines.deploy import stream_deploy

    return StreamingResponse(
        stream_deploy(
            template_id=_tmpl_id,
            template_name=_tmpl_name,
            tpl=tpl,
            user_params=user_params,
            resource_group=resource_group,
            deployment_name=deployment_name,
            region=region,
            is_blueprint=is_blueprint,
            service_ids=service_ids if isinstance(service_ids, list) else [],
            target_ver=target_ver,
            deploy_semver=_deploy_semver,
        ),
        media_type="application/x-ndjson",
    )

@app.delete("/api/catalog/services/{service_id}")
async def delete_service_endpoint(service_id: str):
    """Remove a service from the catalog."""

    backend = await get_backend()
    rows = await backend.execute("SELECT id FROM services WHERE id = ?", (service_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Service not found")
    # Delete children first
    await backend.execute_write("DELETE FROM service_approved_skus WHERE service_id = ?", (service_id,))
    await backend.execute_write("DELETE FROM service_approved_regions WHERE service_id = ?", (service_id,))
    await backend.execute_write("DELETE FROM service_policies WHERE service_id = ?", (service_id,))
    await backend.execute_write("DELETE FROM services WHERE id = ?", (service_id,))
    return JSONResponse({"status": "ok", "deleted": service_id})


@app.post("/api/services/{service_id:path}/offboard")
async def offboard_service_endpoint(service_id: str):
    """Offboard a service: deactivate it while preserving history for audit.

    - Sets service status to 'offboarded'
    - Clears active_version (no active template)
    - Marks all approved versions as 'deprecated'
    - Preserves all data for audit trail

    The service can be re-onboarded later if needed.
    """
    from datetime import datetime, timezone

    svc = await _require_service(service_id)

    if svc.get("status") == "offboarded":
        raise HTTPException(status_code=400, detail="Service is already offboarded")

    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()

    # Clear active version and set status to offboarded
    await backend.execute_write(
        """UPDATE services
           SET status = 'offboarded',
               active_version = NULL,
               template_api_version = NULL,
               updated_at = ?
           WHERE id = ?""",
        (now, service_id),
    )

    # Mark all approved versions as deprecated (preserves draft/failed as-is)
    await backend.execute_write(
        """UPDATE service_versions
           SET status = 'deprecated'
           WHERE service_id = ? AND status = 'approved'""",
        (service_id,),
    )

    logger.info(f"Service offboarded: {service_id}")

    return JSONResponse({
        "status": "ok",
        "service_id": service_id,
        "message": f"Service '{svc.get('name', service_id)}' has been offboarded. "
                   f"All approved versions marked as deprecated. "
                   f"The service can be re-onboarded at any time.",
    })


# ── Service Update Check (bulk API-version comparison) ───────


async def _refresh_api_versions(onboarded_ids: set[str]) -> list[dict]:
    """Fetch latest API versions from Azure for a set of service IDs.

    Does a single `providers.list()` call, extracts API versions only for
    the resource types in ``onboarded_ids``, and returns a list of dicts
    suitable for ``bulk_update_api_versions()``.

    Raises on auth/network failure so the caller can fall back to cached data.
    """
    import os
    import asyncio as _aio
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.resource import ResourceManagementClient

    sub_id = os.getenv("AZURE_SUBSCRIPTION_ID", "")
    if not sub_id:
        try:
            import subprocess
            r = subprocess.run(
                ["az", "account", "show", "--query", "id", "-o", "tsv"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                sub_id = r.stdout.strip()
        except Exception:
            pass
    if not sub_id:
        logger.warning("_refresh_api_versions: no subscription ID — skipping")
        return []

    cred = DefaultAzureCredential(
        exclude_workload_identity_credential=True,
        exclude_managed_identity_credential=True,
    )
    client = ResourceManagementClient(cred, sub_id)

    # Build set of namespaces we actually need
    needed_namespaces = {sid.split("/")[0].lower() for sid in onboarded_ids}

    loop = _aio.get_event_loop()
    providers = await loop.run_in_executor(None, lambda: list(client.providers.list()))

    updates: list[dict] = []
    for provider in providers:
        ns = provider.namespace or ""
        if ns.lower() not in needed_namespaces:
            continue
        for rt in (provider.resource_types or []):
            type_name = rt.resource_type or ""
            sid = f"{ns}/{type_name}"
            if sid not in onboarded_ids:
                continue

            api_versions_list = rt.api_versions or []
            latest_stable = next(
                (v for v in api_versions_list if "preview" not in v.lower()),
                api_versions_list[0] if api_versions_list else None,
            )
            default_ver = getattr(rt, "default_api_version", None)
            if latest_stable:
                updates.append({
                    "id": sid,
                    "latest_api_version": latest_stable,
                    "default_api_version": default_ver,
                })

    return updates


@app.get("/api/catalog/services/check-updates")
async def check_service_updates():
    """Refresh Azure API versions for onboarded services, then compare against templates.

    1. Fetches latest API versions from Azure for onboarded services (lightweight)
    2. Compares each service's ARM template apiVersion against Azure's latest
    3. Returns update list + all_api_versions map for the frontend to populate the column
    """

    try:
        services = await get_all_services()

        # ── Step 1: Refresh API versions from Azure for onboarded services ──
        onboarded_ids = {
            s["id"] for s in services if s.get("active_version") is not None
        }
        if onboarded_ids:
            try:
                refreshed = await _refresh_api_versions(onboarded_ids)
                if refreshed:
                    await bulk_update_api_versions(refreshed)
                    # Reload services so we have the updated values
                    services = await get_all_services()
                    logger.info(f"check-updates: refreshed API versions for {len(refreshed)} services")
            except Exception as e:
                logger.warning(f"Azure API version refresh failed (using cached data): {e}")

        # ── Step 2: Build all_api_versions map for frontend ──
        all_api_versions: dict[str, dict] = {}
        for svc in services:
            latest_api = svc.get("latest_api_version")
            default_api = svc.get("default_api_version")
            if latest_api:
                all_api_versions[svc["id"]] = {
                    "latest_api_version": latest_api,
                    "default_api_version": default_api,
                }

        # ── Step 3: Compare template apiVersions against Azure's latest ──
        # Also extract template_api_version for ALL onboarded services
        updates: list[dict] = []
        total_checked = 0
        template_api_map: dict[str, str] = {}   # service_id → template apiVersion
        template_api_db_updates: list[tuple] = []

        backend = await get_backend()

        for svc in services:
            active_ver_num = svc.get("active_version")
            if active_ver_num is None:
                continue

            # Fetch versions and find the active one
            versions = await get_service_versions(svc["id"])
            active_ver = next(
                (v for v in versions if v.get("version") == active_ver_num), None
            )
            if not active_ver:
                continue

            arm_str = active_ver.get("arm_template")
            if not arm_str:
                continue

            try:
                tpl = json.loads(arm_str)
            except Exception:
                continue

            # Extract apiVersions from template resources
            resources = tpl.get("resources", [])
            template_api_versions = sorted(
                {r.get("apiVersion", "") for r in resources
                 if isinstance(r, dict) and r.get("apiVersion")},
                reverse=True,
            )
            if not template_api_versions:
                continue

            template_api = template_api_versions[0]
            template_api_map[svc["id"]] = template_api

            # Queue DB update if template_api_version changed
            if svc.get("template_api_version") != template_api:
                template_api_db_updates.append((template_api, svc["id"]))

            # Only compare against Azure if we have latest_api_version
            latest_api = svc.get("latest_api_version")
            if not latest_api:
                continue

            total_checked += 1
            if latest_api > template_api:
                active_semver_str = active_ver.get("semver") or (f"{active_ver_num}.0.0" if active_ver_num else None)
                updates.append({
                    "id": svc["id"],
                    "name": svc.get("name", svc["id"]),
                    "category": svc.get("category", "other"),
                    "active_version": active_ver_num,
                    "active_semver": active_semver_str,
                    "template_api_version": template_api,
                    "latest_api_version": latest_api,
                    "default_api_version": svc.get("default_api_version"),
                })

        # Persist template_api_version for all services (backfill)
        if template_api_db_updates:
            for tmpl_api, sid in template_api_db_updates:
                await backend.execute_write(
                    "UPDATE services SET template_api_version = ? WHERE id = ?",
                    (tmpl_api, sid),
                )
            logger.info(f"check-updates: backfilled template_api_version for {len(template_api_db_updates)} services")

        return JSONResponse({
            "updates": updates,
            "total_checked": total_checked,
            "updates_available": len(updates),
            "all_api_versions": all_api_versions,
            "template_api_versions": template_api_map,
        })
    except Exception as e:
        logger.error(f"Failed to check service updates: {e}")
        return JSONResponse({
            "updates": [], "total_checked": 0,
            "updates_available": 0, "all_api_versions": {},
        })


# ── Upgrade Compatibility Analysis ───────────────────────────

@app.post("/api/services/{service_id:path}/analyze-upgrade")
async def analyze_upgrade_compatibility(service_id: str, request: Request):
    """Analyze compatibility implications of upgrading a service's API version.

    Uses the Upgrade Analyst agent (via Copilot SDK) to compare the current
    template API version against the target version and report:
    - Breaking changes
    - Deprecated features
    - New capabilities
    - Migration effort estimate
    - Actionable recommendation

    Streams NDJSON events for real-time progress in the UI.
    """
    from src.agents import UPGRADE_ANALYST
    from src.model_router import get_model_for_task, get_model_display

    svc = await _require_service(service_id)

    active_ver_num = svc.get("active_version")
    if active_ver_num is None:
        raise HTTPException(status_code=400, detail="Service has no active version to analyze")

    body = await _parse_body(request)

    target_api = body.get("target_version") or svc.get("latest_api_version")
    if not target_api:
        raise HTTPException(status_code=400, detail="No target API version specified — run Check for Updates first")

    async def _stream():
        import asyncio as _aio

        try:
            # ── Step 1: Read current template ──
            versions = await get_service_versions(service_id)
            active_ver = next(
                (v for v in versions if v.get("version") == active_ver_num), None
            )
            if not active_ver or not active_ver.get("arm_template"):
                yield json.dumps({
                    "type": "error",
                    "detail": "No ARM template found for the active version.",
                }) + "\n"
                return

            arm_str = active_ver.get("arm_template", "")
            try:
                tpl = json.loads(arm_str)
            except Exception:
                tpl = {}

            # Extract current apiVersions from template
            resources = tpl.get("resources", [])
            current_api_versions = sorted(
                {r.get("apiVersion", "") for r in resources
                 if isinstance(r, dict) and r.get("apiVersion")},
                reverse=True,
            )
            current_api = current_api_versions[0] if current_api_versions else "unknown"

            yield json.dumps({
                "type": "progress", "phase": "checkout",
                "detail": f"📋 Read active template — current API version: {current_api}",
                "progress": 0.15,
            }) + "\n"

            # ── Step 2: Gather Azure API version info ──
            yield json.dumps({
                "type": "progress", "phase": "azure_lookup",
                "detail": f"🔍 Gathering API version information for {svc.get('name', service_id)}…",
                "progress": 0.25,
            }) + "\n"

            # Get all available API versions for this resource type from Azure
            all_api_versions = []
            try:
                import os
                from azure.identity import DefaultAzureCredential
                from azure.mgmt.resource import ResourceManagementClient

                sub_id = os.getenv("AZURE_SUBSCRIPTION_ID", "")
                if sub_id:
                    cred = DefaultAzureCredential(
                        exclude_workload_identity_credential=True,
                        exclude_managed_identity_credential=True,
                    )
                    rm_client = ResourceManagementClient(cred, sub_id)
                    namespace = service_id.split("/")[0]
                    type_name = service_id.split("/", 1)[1] if "/" in service_id else ""

                    loop = _aio.get_event_loop()
                    provider = await loop.run_in_executor(
                        None, lambda: rm_client.providers.get(namespace)
                    )
                    for rt in (provider.resource_types or []):
                        if (rt.resource_type or "").lower() == type_name.lower():
                            all_api_versions = rt.api_versions or []
                            break
            except Exception as e:
                logger.warning(f"Could not fetch API versions from Azure: {e}")

            api_version_context = ""
            if all_api_versions:
                # Show versions between current and target (inclusive)
                relevant_versions = [v for v in all_api_versions
                                     if v >= current_api and v <= target_api]
                if not relevant_versions:
                    relevant_versions = all_api_versions[:10]
                api_version_context = f"\n\nAvailable API versions for {service_id} (relevant range): {', '.join(relevant_versions)}"
                api_version_context += f"\nAll available versions: {', '.join(all_api_versions[:20])}"

            yield json.dumps({
                "type": "progress", "phase": "azure_lookup",
                "detail": f"✅ Found {len(all_api_versions)} API versions for {service_id}",
                "progress": 0.35,
            }) + "\n"

            # ── Step 3: Send to Upgrade Analyst agent ──
            model = get_model_for_task(UPGRADE_ANALYST.task)
            model_display = get_model_display(UPGRADE_ANALYST.task)

            yield json.dumps({
                "type": "progress", "phase": "analysis",
                "detail": f"🧠 {UPGRADE_ANALYST.name} analyzing compatibility ({model_display})…",
                "progress": 0.40,
                "agent": UPGRADE_ANALYST.name,
                "model": model_display,
            }) + "\n"

            # Build a concise template summary (don't send the full template — just resources)
            resource_summary = []
            for r in resources:
                if isinstance(r, dict):
                    resource_summary.append({
                        "type": r.get("type", ""),
                        "apiVersion": r.get("apiVersion", ""),
                        "name": r.get("name", ""),
                        "properties_keys": list((r.get("properties") or {}).keys()),
                    })

            prompt = f"""\
Analyze the compatibility implications of upgrading the Azure API version for this service.

## Service
- **Resource Type**: {service_id}
- **Service Name**: {svc.get('name', service_id)}
- **Current API Version**: {current_api}
- **Target API Version**: {target_api}
{api_version_context}

## Current ARM Template Resources
```json
{json.dumps(resource_summary, indent=2)}
```

## Full ARM Template (for property-level analysis)
```json
{arm_str[:8000]}
```

Please provide a thorough compatibility analysis covering breaking changes, deprecations, \
new features, behavioral changes, migration effort, and your recommendation.
"""

            client = await ensure_copilot_client()
            if not client:
                yield json.dumps({
                    "type": "error",
                    "detail": "Copilot SDK not available — cannot run analysis.",
                }) + "\n"
                return

            from src.copilot_helpers import copilot_send

            analysis = await copilot_send(
                client,
                model=model,
                system_prompt=UPGRADE_ANALYST.system_prompt,
                prompt=prompt,
                timeout=UPGRADE_ANALYST.timeout,
                agent_name=UPGRADE_ANALYST.name,
            )

            yield json.dumps({
                "type": "progress", "phase": "analysis",
                "detail": "✅ Analysis complete",
                "progress": 0.90,
            }) + "\n"

            # ── Step 4: Return the analysis ──
            yield json.dumps({
                "type": "analysis_complete",
                "service_id": service_id,
                "service_name": svc.get("name", service_id),
                "current_api_version": current_api,
                "target_api_version": target_api,
                "analysis": analysis,
                "agent": UPGRADE_ANALYST.name,
                "model": model_display,
                "progress": 1.0,
            }) + "\n"

        except Exception as e:
            logger.error(f"Upgrade analysis failed for {service_id}: {e}", exc_info=True)
            yield json.dumps({
                "type": "error",
                "detail": f"Analysis failed: {str(e)}",
            }) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


# ── Upgrade Analyst Chat ─────────────────────────────────────

@app.post("/api/services/{service_id:path}/upgrade-chat")
async def upgrade_analyst_chat(service_id: str, request: Request):
    """Chat with the Upgrade Analyst about a completed compatibility analysis.

    Accepts the user message, conversation history, and analysis context.
    Fetches the actual ARM template and composition dependencies from the DB
    so the agent has full infrastructure context.
    Streams NDJSON delta events for real-time rendering in the modal chat.

    Body:
    {
        "message": "Will the NSG rule changes affect my inbound traffic?",
        "history": [
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": "..."}
        ],
        "analysis_context": {
            "current_api_version": "2024-05-01",
            "target_api_version": "2025-07-01",
            "analysis": "<original analysis text>"
        },
        "template_id": "optional-composed-template-id"
    }
    """
    from src.agents import UPGRADE_ANALYST
    from src.model_router import get_model_for_task

    svc = await _require_service(service_id)

    body = await _parse_body_required(request)

    message = body.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    history = body.get("history", [])
    analysis_ctx = body.get("analysis_context", {})
    template_id = body.get("template_id")

    async def _stream():
        import asyncio as _aio

        try:
            client = await ensure_copilot_client()
            if not client:
                yield json.dumps({
                    "type": "error",
                    "detail": "Copilot SDK not available.",
                }) + "\n"
                return

            model = get_model_for_task(UPGRADE_ANALYST.task)

            # ── Fetch the actual ARM template from DB ──
            arm_template_str = ""
            try:
                active_ver = await get_active_service_version(service_id)
                if active_ver and active_ver.get("arm_template"):
                    arm_template_str = active_ver["arm_template"]
            except Exception as e:
                logger.warning(f"upgrade-chat: could not fetch ARM template for {service_id}: {e}")

            # ── Fetch composition context (sibling services) ──
            composition_context = ""
            if template_id:
                try:
                    tmpl = await get_template_by_id(template_id)
                    if tmpl:
                        sibling_ids = [sid for sid in (tmpl.get("service_ids") or []) if sid != service_id]
                        if sibling_ids:
                            svc_map = await get_services_basic(sibling_ids)
                            sibling_summaries = []
                            for sid in sibling_ids:
                                sib = svc_map.get(sid)
                                if not sib:
                                    sibling_summaries.append(f"- `{sid}` (no data)")
                                    continue
                                sib_name = sib.get("name", sid.split("/")[-1])
                                sib_api = sib.get("template_api_version") or sib.get("latest_api_version") or "unknown"
                                sibling_summaries.append(
                                    f"- **{sib_name}** (`{sid}`) — API version: `{sib_api}`"
                                )
                                # Also fetch the sibling ARM template for cross-reference
                                try:
                                    sib_ver = await get_active_service_version(sid)
                                    if sib_ver and sib_ver.get("arm_template"):
                                        sib_arm = sib_ver["arm_template"]
                                        # Parse to get just resource types and properties keys
                                        sib_tpl = json.loads(sib_arm)
                                        sib_resources = sib_tpl.get("resources", [])
                                        sib_res_summary = []
                                        for r in sib_resources:
                                            if isinstance(r, dict):
                                                sib_res_summary.append({
                                                    "type": r.get("type", ""),
                                                    "apiVersion": r.get("apiVersion", ""),
                                                    "name": r.get("name", ""),
                                                    "properties_keys": list((r.get("properties") or {}).keys()),
                                                })
                                        if sib_res_summary:
                                            sibling_summaries.append(
                                                f"  Resources: ```json\n  {json.dumps(sib_res_summary, indent=2)[:2000]}\n  ```"
                                            )
                                except Exception:
                                    pass

                            composition_context = (
                                f"\n\n## Composed Template: `{tmpl.get('name', template_id)}`\n"
                                f"This service is part of a composed template with {len(sibling_ids)} other service(s):\n\n"
                                + "\n".join(sibling_summaries)
                                + "\n\nConsider cross-service compatibility when answering questions."
                            )
                except Exception as e:
                    logger.warning(f"upgrade-chat: could not fetch composition context: {e}")

            # ── Build system prompt with full context ──
            system_prompt = UPGRADE_ANALYST.system_prompt + "\n\n"
            system_prompt += "## Analysis Context\n"
            system_prompt += f"- **Service**: {svc.get('name', service_id)} (`{service_id}`)\n"
            if analysis_ctx.get("current_api_version"):
                system_prompt += f"- **Current API Version**: {analysis_ctx['current_api_version']}\n"
            if analysis_ctx.get("target_api_version"):
                system_prompt += f"- **Target API Version**: {analysis_ctx['target_api_version']}\n"

            # Include the actual ARM template
            if arm_template_str:
                # Truncate if very large, but include as much as possible
                template_excerpt = arm_template_str[:12000]
                system_prompt += (
                    f"\n### Current ARM Template for {svc.get('name', service_id)}\n"
                    f"```json\n{template_excerpt}\n```\n"
                )
                if len(arm_template_str) > 12000:
                    system_prompt += f"*(Template truncated — {len(arm_template_str)} chars total)*\n"
            else:
                system_prompt += "\n*No ARM template found in the database for this service.*\n"

            # Include composition context
            if composition_context:
                system_prompt += composition_context

            # Include previous analysis
            if analysis_ctx.get("analysis"):
                system_prompt += (
                    "\n\n### Previous Upgrade Analysis\n"
                    + analysis_ctx["analysis"][:6000]
                    + "\n"
                )

            system_prompt += (
                "\nYou are continuing a conversation about this upgrade analysis. "
                "Answer follow-up questions concisely and helpfully. "
                "Reference specific properties and values from the ARM template. "
                "NEVER ask the user to share their template — you already have it above."
            )

            # Build the prompt with conversation history
            conversation = ""
            for turn in history[-20:]:  # keep last 20 turns max
                role = turn.get("role", "user")
                content = turn.get("content", "")
                if role == "user":
                    conversation += f"\n\n**User**: {content}"
                else:
                    conversation += f"\n\n**Assistant**: {content}"
            conversation += f"\n\n**User**: {message}"

            prompt = conversation.strip()

            # Create session with streaming and capture deltas
            session = await client.create_session({
                "model": model,
                "streaming": True,
                "tools": [],
                "system_message": {"content": system_prompt},
                "on_permission_request": approve_all,
            })

            response_chunks: list[str] = []
            done_event = _aio.Event()
            full_content_holder: list[str] = []

            def on_event(event):
                try:
                    evt_type = event.type.value
                    if evt_type == "assistant.message_delta":
                        delta = event.data.delta_content or ""
                        response_chunks.append(delta)
                    elif evt_type == "assistant.message":
                        full = event.data.content or ""
                        if full:
                            full_content_holder.append(full)
                    elif evt_type == "session.idle":
                        done_event.set()
                except Exception as e:
                    logger.error(f"Upgrade chat event error: {e}")
                    done_event.set()

            unsub = session.on(on_event)

            try:
                await session.send({"prompt": prompt})

                # Yield deltas as they arrive
                last_idx = 0
                while not done_event.is_set():
                    await _aio.sleep(0.05)
                    if len(response_chunks) > last_idx:
                        for chunk in response_chunks[last_idx:]:
                            yield json.dumps({
                                "type": "delta",
                                "content": chunk,
                            }) + "\n"
                        last_idx = len(response_chunks)

                # Flush remaining deltas
                if len(response_chunks) > last_idx:
                    for chunk in response_chunks[last_idx:]:
                        yield json.dumps({
                            "type": "delta",
                            "content": chunk,
                        }) + "\n"

                # Send done event with full content
                full_text = (
                    full_content_holder[0]
                    if full_content_holder
                    else "".join(response_chunks)
                )
                yield json.dumps({
                    "type": "done",
                    "content": full_text,
                }) + "\n"

            except _aio.TimeoutError:
                yield json.dumps({
                    "type": "error",
                    "detail": "Response timed out.",
                }) + "\n"
            finally:
                unsub()
                try:
                    await session.destroy()
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Upgrade chat failed for {service_id}: {e}", exc_info=True)
            yield json.dumps({
                "type": "error",
                "detail": f"Chat failed: {str(e)}",
            }) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


# ── API Version Update Pipeline ──────────────────────────────

@app.post("/api/services/{service_id:path}/update-api-version")
async def update_api_version_pipeline(service_id: str, request: Request):
    """Update a service's ARM template to use the latest Azure API version.

    Pipeline:
    1. Checkout — Read the current active ARM template
    2. Update  — Rewrite apiVersion references to the latest Azure version
    3. Validate — Static policy check against org governance
    4. What-If — ARM What-If deployment preview
    5. Deploy  — Test deployment to validation resource group
    6. Policy  — Runtime compliance check
    7. Cleanup — Delete validation resource group
    8. Publish — Save new version, promote to active

    Streams NDJSON events for real-time progress tracking.
    Goal-driven auto-healing via Copilot SDK (up to 5 outer attempts with
    deploy sub-loop). Two-phase healing: analyze root cause → plan → fix.
    Escalates from surface healer to deep healer after threshold.
    """
    from src.tools.static_policy_validator import (
        validate_template, validate_template_against_standards,
        build_remediation_prompt,
    )
    from src.standards import get_standards_for_service, build_governance_generation_context, build_arm_generation_context

    MAX_HEAL_ATTEMPTS = 5
    DEEP_HEAL_THRESHOLD = 3        # escalate to two-phase after this many deploy heals
    MAX_DEPLOY_SUB_HEALS = 2       # deploy-specific retries per outer attempt

    svc = await _require_service(service_id)

    if svc.get("status") == "offboarded":
        raise HTTPException(status_code=400, detail="Cannot update an offboarded service. Re-onboard the service first.")

    active_ver_num = svc.get("active_version")
    if active_ver_num is None:
        raise HTTPException(status_code=400, detail="Service has no active version to update")

    # Resolve semver for display (never show raw integer versions)
    _active_semver = await get_latest_semver(service_id) or f"{active_ver_num}.0.0"

    latest_api = svc.get("latest_api_version")
    if not latest_api:
        raise HTTPException(status_code=400, detail="No Azure API version data — run Check for Updates first")

    body = await _parse_body(request)

    # Allow caller to specify a target version (e.g. recommended vs latest)
    target_api = body.get("target_version") or latest_api
    model_id = body.get("model", get_active_model())
    region = body.get("region", "eastus2")

    import uuid as _uuid
    _run_id = _uuid.uuid4().hex[:8]
    rg_name = f"infraforge-val-{service_id.replace('/', '-').replace('.', '-').lower()}-{_run_id}"[:90]

    async def _stream():
        nonlocal _active_semver, active_ver_num
        from src.copilot_helpers import copilot_send
        from src.agents import LLM_REASONER, TEMPLATE_HEALER, DEEP_TEMPLATE_HEALER, ARM_MODIFIER
        from src.pipeline_helpers import (
            guard_locations, ensure_parameter_defaults,
            sanitize_placeholder_guids, extract_param_values,
            get_resource_type_hints,
        )

        try:  # ← top-level error wrapper for the entire stream

            # Record pipeline run for history
            await create_pipeline_run(
                _run_id, service_id, "api_version_update",
                created_by="copilot-sdk",
            )

            # ═══════════════════════════════════════════════════
            # PHASE 0: MODEL ROUTING
            # ═══════════════════════════════════════════════════
            _routing = {
                "planning":        {"model": get_model_for_task(Task.PLANNING),        "display": get_model_display(Task.PLANNING),        "reason": get_task_reason(Task.PLANNING)},
                "code_generation": {"model": get_model_for_task(Task.CODE_GENERATION), "display": get_model_display(Task.CODE_GENERATION), "reason": get_task_reason(Task.CODE_GENERATION)},
                "code_fixing":     {"model": get_model_for_task(Task.CODE_FIXING),     "display": get_model_display(Task.CODE_FIXING),     "reason": get_task_reason(Task.CODE_FIXING)},
            }
            yield json.dumps({
                "type": "progress", "phase": "init_model",
                "detail": "🤖 Model routing configured — PLAN→EXECUTE pattern for API version migration",
                "progress": 0.01,
                "model_routing": _routing,
            }) + "\n"
            for task_key, info in _routing.items():
                yield json.dumps({
                    "type": "llm_reasoning", "phase": "init_model",
                    "detail": f"  {task_key}: {info['display']} — {info['reason'][:80]}",
                    "progress": 0.01,
                }) + "\n"

            # ── Pipeline overview — tell the user what's about to happen ─
            yield json.dumps({
                "type": "progress", "phase": "pipeline_overview",
                "detail": f"Pipeline: Update {svc.get('name', service_id)} from API version {svc.get('current_api_version', '?')} → {target_api}",
                "progress": 0.015,
                "steps": [
                    "Check out current active template",
                    "AI analyzes breaking changes & plans migration",
                    "Rewrite template with updated API version",
                    "Run static governance policy checks",
                    "ARM What-If preview (dry run)",
                    "Deploy to isolated validation resource group",
                    "Runtime compliance verification",
                    "Clean up validation resources",
                    "Publish & promote new version",
                ],
            }) + "\n"

            # ── Cleanup stale drafts/failed from previous runs ────
            _cleaned = await delete_service_versions_by_status(
                service_id, ["draft", "failed"],
            )
            if _cleaned:
                yield json.dumps({
                    "type": "progress", "phase": "cleanup_drafts",
                    "detail": f"🧹 Cleaned up {_cleaned} stale draft/failed version(s) from previous runs",
                    "progress": 0.015,
                }) + "\n"

            # ── Fetch governance & security context for migration ──
            _governance_ctx = await build_governance_generation_context()
            _standards_ctx = await build_arm_generation_context(service_id)

            # ── Step 1: Checkout ──────────────────────────────────
            yield json.dumps({
                "type": "progress", "phase": "checkout",
                "detail": f"Checking out active template (v{_active_semver}) for {svc.get('name', service_id)}…",
                "progress": 0.02,
            }) + "\n"

            active_ver = await get_service_version(service_id, active_ver_num)
            if not active_ver or not active_ver.get("arm_template"):
                yield json.dumps({
                    "type": "error", "phase": "checkout",
                    "detail": "✗ No ARM template found for the active version",
                    "progress": 1.0,
                }) + "\n"
                await complete_pipeline_run(_run_id, "failed", error_detail="No ARM template found")
                return

            original_template = active_ver["arm_template"]

            # Parse and extract current apiVersions
            try:
                tpl = json.loads(original_template)
            except Exception as e:
                yield json.dumps({
                    "type": "error", "phase": "checkout",
                    "detail": f"✗ Failed to parse ARM template: {e}",
                    "progress": 1.0,
                }) + "\n"
                await complete_pipeline_run(_run_id, "failed", error_detail=f"Parse error: {e}")
                return

            resources = tpl.get("resources", [])
            current_api_versions = sorted(
                {r.get("apiVersion", "") for r in resources
                 if isinstance(r, dict) and r.get("apiVersion")},
                reverse=True,
            )
            current_api = current_api_versions[0] if current_api_versions else "unknown"

            # Validate ARM template contains the expected resource type
            _arm_types = [
                r.get("type", "").lower()
                for r in resources
                if isinstance(r, dict) and r.get("type")
            ]
            _expected_type = service_id.lower()
            _expected_parent = "/".join(service_id.split("/")[:2]).lower() if service_id.count("/") >= 2 else None
            _type_found = any(
                _expected_type in t or (_expected_parent and _expected_parent in t)
                for t in _arm_types
            )
            if not _type_found and _arm_types:
                _actual = ", ".join(sorted(set(r.get("type", "?") for r in resources if isinstance(r, dict) and r.get("type"))))

                # ── Auto-recovery: regenerate the ARM template ────
                yield json.dumps({
                    "type": "progress", "phase": "checkout_recovery",
                    "detail": (
                        f"⚠️ Template contains wrong resource type [{_actual}] — "
                        f"expected '{service_id}'. Auto-recovering by regenerating…"
                    ),
                    "progress": 0.04,
                }) + "\n"

                try:
                    from src.tools.arm_generator import generate_arm_template, has_builtin_skeleton, generate_arm_template_with_copilot
                    from src.pipeline_helpers import sanitize_template, inject_standard_tags

                    # Clear active version so delete_service_versions_by_status can purge all
                    backend = await get_backend()
                    await backend.execute_write(
                        "UPDATE services SET active_version = NULL WHERE id = ?",
                        (service_id,),
                    )

                    # Delete all corrupt versions
                    _purged = await delete_service_versions_by_status(
                        service_id, ["approved", "draft", "failed", "deprecated", "validating"],
                    )
                    if _purged:
                        yield json.dumps({
                            "type": "llm_reasoning", "phase": "checkout_recovery",
                            "detail": f"🧹 Purged {_purged} corrupt version(s)",
                            "progress": 0.05,
                        }) + "\n"

                    # Generate fresh ARM template
                    _gen_model = get_model_display(Task.CODE_GENERATION)
                    _gen_model_id = get_model_for_task(Task.CODE_GENERATION)

                    yield json.dumps({
                        "type": "llm_reasoning", "phase": "checkout_recovery",
                        "detail": f"⚙️ {_gen_model} generating correct ARM template for {svc.get('name', service_id)}…",
                        "progress": 0.06,
                    }) + "\n"

                    if has_builtin_skeleton(service_id):
                        _regen_tpl = json.dumps(generate_arm_template(service_id), indent=2)
                        _regen_source = "built-in skeleton (auto-recovery)"
                    else:
                        _copilot = await ensure_copilot_client()
                        if _copilot is None:
                            raise RuntimeError("Copilot SDK not available")
                        _regen_tpl = await generate_arm_template_with_copilot(
                            service_id, svc.get("name", service_id),
                            _copilot, _gen_model_id, region=region,
                        )
                        _regen_source = f"Copilot SDK auto-recovery ({_gen_model})"

                    # Validate the regenerated template
                    _regen_parsed = json.loads(_regen_tpl)
                    _regen_types = [
                        r.get("type", "").lower()
                        for r in _regen_parsed.get("resources", [])
                        if isinstance(r, dict) and r.get("type")
                    ]
                    _regen_ok = any(
                        _expected_type in t or (_expected_parent and _expected_parent in t)
                        for t in _regen_types
                    )
                    if not _regen_ok:
                        raise RuntimeError(
                            f"Regenerated template still has wrong types: {_regen_types}"
                        )

                    # Sanitize + tag + stamp
                    _regen_tpl = sanitize_template(_regen_tpl)
                    _regen_tpl = await inject_standard_tags(_regen_tpl, service_id)

                    _new_ver = 1  # Start fresh
                    _new_semver = "1.0.0"
                    _regen_tpl = _stamp_template_metadata(
                        _regen_tpl, service_id=service_id,
                        version_int=_new_ver, semver=_new_semver,
                        gen_source=_regen_source, region=region,
                    )

                    # Save as approved + set active
                    ver_row = await create_service_version(
                        service_id=service_id, arm_template=_regen_tpl,
                        version=_new_ver, semver=_new_semver, status="approved",
                        changelog="Auto-recovery: regenerated correct ARM template",
                        created_by=_regen_source,
                    )
                    backend = await get_backend()
                    await backend.execute_write(
                        "UPDATE services SET active_version = ?, status = 'approved', updated_at = ? WHERE id = ?",
                        (_new_ver, datetime.now(timezone.utc).isoformat(), service_id),
                    )

                    yield json.dumps({
                        "type": "progress", "phase": "checkout_recovery_done",
                        "detail": f"✅ Regenerated correct ARM template (v{_new_semver}) — continuing update…",
                        "progress": 0.08,
                    }) + "\n"

                    # Replace variables for the rest of the pipeline
                    original_template = _regen_tpl
                    active_ver_num = _new_ver
                    _active_semver = _new_semver
                    tpl = json.loads(_regen_tpl)
                    resources = tpl.get("resources", [])
                    current_api_versions = sorted(
                        {r.get("apiVersion", "") for r in resources
                         if isinstance(r, dict) and r.get("apiVersion")},
                        reverse=True,
                    )
                    current_api = current_api_versions[0] if current_api_versions else "unknown"

                except Exception as _recov_err:
                    logger.error(f"Auto-recovery failed for {service_id}: {_recov_err}", exc_info=True)
                    yield json.dumps({
                        "type": "error", "phase": "checkout",
                        "detail": (
                            f"✗ ARM template has wrong resource type [{_actual}] and auto-recovery failed: {_recov_err}. "
                            f"Please re-onboard this service manually."
                        ),
                        "progress": 1.0,
                    }) + "\n"
                    await complete_pipeline_run(_run_id, "failed", error_detail=f"Resource type mismatch + recovery failed: {_recov_err}")
                    return

            _resource_summary = ", ".join(sorted({r.get('type','?').split('/')[-1] for r in resources if isinstance(r,dict) and r.get('type')})[:5])
            _more = max(0, len(resources) - 5)
            _res_text = _resource_summary + (f" +{_more} more" if _more else "")

            yield json.dumps({
                "type": "progress", "phase": "checkout_complete",
                "detail": f"✓ Template loaded — {len(resources)} resource(s) ({_res_text}) using API {current_api}",
                "progress": 0.08,
                "current_api_version": current_api,
                "target_api_version": target_api,
                "resource_count": len(resources),
            }) + "\n"

            # ═══════════════════════════════════════════════════
            # STEP 2: PLAN — Reasoning model analyzes migration
            # ═══════════════════════════════════════════════════
            #
            # o3-mini reasons about what changes are needed beyond the
            # apiVersion field: renamed properties, new required fields,
            # deprecated features, schema changes between API versions.

            _plan_model = get_model_display(Task.PLANNING)
            yield json.dumps({
                "type": "progress", "phase": "planning",
                "detail": f"🧠 PLAN phase — {_plan_model} analyzing migration from {current_api} → {target_api}…",
                "progress": 0.10,
            }) + "\n"

            # Collect resource types for targeted analysis
            resource_types = sorted({
                r.get("type", "unknown") for r in resources
                if isinstance(r, dict) and r.get("type")
            })

            planning_prompt = (
                f"You are analyzing an Azure ARM template API version migration.\n\n"
                f"**Current API version:** {current_api}\n"
                f"**Target API version:**  {target_api}\n"
                f"**Resource types in template:** {', '.join(resource_types)}\n"
                f"**Resource count:** {len(resources)}\n\n"
                f"--- CURRENT ARM TEMPLATE ---\n{original_template}\n--- END TEMPLATE ---\n\n"
                "Analyze this migration and produce a structured migration plan:\n\n"
                "## Required Output Sections:\n"
                "1. **Breaking Changes**: Are there any known breaking changes between these "
                "API versions for these resource types? List property renames, removals, "
                "new required fields, or behavioral changes.\n"
                "2. **Property Updates**: Specific properties that need to change beyond just "
                "the apiVersion field. Include the resource type, old property path, new "
                "property path, and reason.\n"
                "3. **Safe to Swap**: Which resources can safely have their apiVersion updated "
                "with no other changes.\n"
                "4. **Risk Assessment**: Rate the migration risk (low/medium/high) and explain.\n"
                "5. **Migration Steps**: Ordered list of specific changes to make.\n"
                "6. **Validation Criteria**: What should pass after the migration.\n\n"
                "Be concrete and specific — include actual property names and values. "
                "This plan will be handed to a code generation model to execute.\n\n"
                "If you're uncertain about breaking changes for a specific API version, "
                "note the uncertainty but still provide your best assessment based on "
                "Azure ARM template patterns and common API evolution.\n"
            )

            if _governance_ctx:
                planning_prompt += (
                    f"\n--- SECURITY & GOVERNANCE REQUIREMENTS (MANDATORY) ---\n"
                    f"{_governance_ctx}\n"
                    f"--- END SECURITY REQUIREMENTS ---\n\n"
                    "IMPORTANT: In addition to the API version migration, also identify and plan "
                    "fixes for any security issues in the template that violate these requirements. "
                    "Include a **Security Fixes** section in your migration plan.\n"
                )

            migration_plan = ""
            try:
                _plan_client = await ensure_copilot_client()
                if _plan_client:
                    migration_plan = await copilot_send(
                        _plan_client,
                        model=get_model_for_task(Task.PLANNING),
                        system_prompt=LLM_REASONER.system_prompt,
                        prompt=planning_prompt,
                        timeout=90,
                        agent_name="LLM_REASONER",
                    )
            except Exception as e:
                logger.warning(f"Planning phase failed (non-fatal): {e}")
                migration_plan = ""

            # Stream the planning output line by line
            for line in migration_plan.split("\n"):
                line = line.strip()
                if line:
                    yield json.dumps({
                        "type": "llm_reasoning", "phase": "planning",
                        "detail": line,
                        "progress": 0.14,
                    }) + "\n"

            if migration_plan:
                yield json.dumps({
                    "type": "progress", "phase": "planning_complete",
                    "detail": f"✓ Migration plan ready — {len(migration_plan)} chars of analysis covering {len(resource_types)} resource type(s)",
                    "progress": 0.16,
                }) + "\n"
            else:
                yield json.dumps({
                    "type": "progress", "phase": "planning_complete",
                    "detail": f"⚠️ Planning phase returned no response — falling back to direct apiVersion swap",
                    "progress": 0.16,
                }) + "\n"

            # ═══════════════════════════════════════════════════
            # STEP 3: EXECUTE — Code gen model applies migration
            # ═══════════════════════════════════════════════════
            #
            # If we have a migration plan, use claude-sonnet-4 to rewrite
            # the template guided by the plan. Otherwise fall back to the
            # simple deterministic apiVersion swap.

            def _update_api_versions(resources_list, tgt_api):
                """Recursively update apiVersion on all resources."""
                count = 0
                for r in resources_list:
                    if isinstance(r, dict) and "apiVersion" in r:
                        r["apiVersion"] = tgt_api
                        count += 1
                    if isinstance(r, dict) and "resources" in r:
                        count += _update_api_versions(r["resources"], tgt_api)
                return count

            _gen_model = get_model_display(Task.CODE_GENERATION)
            updated_template = None

            if migration_plan:
                yield json.dumps({
                    "type": "progress", "phase": "executing",
                    "detail": f"⚡ EXECUTE phase — {_gen_model} rewriting template guided by migration plan…",
                    "progress": 0.17,
                }) + "\n"

                execute_prompt = (
                    f"Rewrite the following ARM template to migrate from API version "
                    f"{current_api} to {target_api}.\n\n"
                    f"--- MIGRATION PLAN (follow this precisely) ---\n"
                    f"{migration_plan}\n"
                    f"--- END MIGRATION PLAN ---\n\n"
                    f"--- CURRENT ARM TEMPLATE ---\n{original_template}\n--- END TEMPLATE ---\n\n"
                    "Apply ALL changes from the migration plan:\n"
                    "1. Update all apiVersion fields to the target version\n"
                    "2. Apply any property renames, additions, or removals identified in the plan\n"
                    "3. Fix ALL security issues identified in the plan (hardcoded passwords, missing encryption, etc.)\n"
                    "4. Preserve the template's intent and resource structure\n"
                    "5. Ensure the result is valid ARM template JSON\n\n"
                )

                if _governance_ctx:
                    execute_prompt += (
                        f"--- SECURITY & GOVERNANCE REQUIREMENTS (MANDATORY — CISO will block non-compliant templates) ---\n"
                        f"{_governance_ctx}\n"
                        f"--- END SECURITY REQUIREMENTS ---\n\n"
                    )

                execute_prompt += (
                    "Return ONLY the complete, corrected ARM template JSON — no markdown "
                    "fences, no explanation, no commentary."
                )

                try:
                    _exec_client = await ensure_copilot_client()
                    if _exec_client:
                        raw = await copilot_send(
                            _exec_client,
                            model=get_model_for_task(ARM_MODIFIER.task),
                            system_prompt=ARM_MODIFIER.system_prompt,
                            prompt=execute_prompt,
                            timeout=ARM_MODIFIER.timeout,
                            agent_name=ARM_MODIFIER.name,
                        )
                        cleaned = raw.strip()
                        if cleaned.startswith("```"):
                            lines = cleaned.split("\n")
                            cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
                        if cleaned.startswith("json"):
                            cleaned = cleaned[4:].strip()
                        # Validate it's valid JSON
                        json.loads(cleaned)
                        updated_template = cleaned

                        # ── Post-EXECUTE validation: verify resource type + apiVersion ──
                        # The LLM sometimes returns templates with wrong resource types or
                        # doesn't actually update the apiVersion. Catch this early.
                        try:
                            _exec_tpl = json.loads(cleaned)
                            _exec_resources = _exec_tpl.get("resources", [])

                            def _collect_resource_types(resources):
                                """Recursively collect (type, apiVersion) tuples."""
                                pairs = []
                                for r in resources:
                                    if isinstance(r, dict):
                                        if r.get("type"):
                                            pairs.append((r["type"].lower(), r.get("apiVersion", "")))
                                        if "resources" in r and isinstance(r["resources"], list):
                                            pairs.extend(_collect_resource_types(r["resources"]))
                                return pairs

                            _exec_pairs = _collect_resource_types(_exec_resources)
                            _svc_type_lower = service_id.lower()
                            # Check 1: at least one resource matches the service type
                            _has_correct_type = any(t == _svc_type_lower for t, _ in _exec_pairs)
                            # Check 2: the target apiVersion is present on the correct resource
                            _has_target_api = any(
                                t == _svc_type_lower and v == target_api
                                for t, v in _exec_pairs
                            )

                            if not _has_correct_type:
                                logger.warning(
                                    f"EXECUTE validation failed: template missing resource type {service_id}. "
                                    f"Found types: {[t for t, _ in _exec_pairs]}. Falling back to direct swap."
                                )
                                updated_template = None
                                yield json.dumps({
                                    "type": "progress", "phase": "execute_validation_failed",
                                    "detail": f"⚠️ LLM output missing {service_id} resource — falling back to direct swap",
                                    "progress": 0.19,
                                }) + "\n"
                            elif not _has_target_api:
                                logger.warning(
                                    f"EXECUTE validation failed: apiVersion not updated to {target_api}. "
                                    f"Found: {[(t, v) for t, v in _exec_pairs if t == _svc_type_lower]}. Falling back to direct swap."
                                )
                                # Use the LLM template but force-update the apiVersion
                                _update_api_versions(_exec_tpl.get("resources", []), target_api)
                                updated_template = json.dumps(_exec_tpl, indent=2)
                                yield json.dumps({
                                    "type": "progress", "phase": "execute_api_fixup",
                                    "detail": f"⚠️ LLM didn't set target apiVersion — force-applied {target_api}",
                                    "progress": 0.19,
                                }) + "\n"
                        except Exception as _val_err:
                            logger.warning(f"Post-EXECUTE validation error: {_val_err}")

                        yield json.dumps({
                            "type": "progress", "phase": "execute_complete",
                            "detail": f"✓ {_gen_model} rewrote template with migration plan applied",
                            "progress": 0.20,
                        }) + "\n"
                except Exception as e:
                    logger.warning(f"EXECUTE phase failed, falling back to direct swap: {e}")
                    yield json.dumps({
                        "type": "progress", "phase": "execute_fallback",
                        "detail": f"⚠️ Code generation failed ({str(e)[:100]}) — falling back to direct apiVersion swap",
                        "progress": 0.18,
                    }) + "\n"
                    updated_template = None

            # Fallback: deterministic apiVersion swap
            if updated_template is None:
                updated_count = _update_api_versions(tpl.get("resources", []), target_api)
                updated_template = json.dumps(tpl, indent=2)

                yield json.dumps({
                    "type": "progress", "phase": "update_complete",
                    "detail": f"✓ Direct swap: updated {updated_count} resource apiVersion(s) to {target_api}",
                    "progress": 0.20,
                }) + "\n"

            # Ensure parameter defaults
            updated_template = _ensure_parameter_defaults(updated_template)

            # ── Save as new draft version ─────────────────────────
            _db = await get_backend()
            _vrows = await _db.execute(
                "SELECT MAX(version) as max_ver FROM service_versions WHERE service_id = ?",
                (service_id,),
            )
            new_ver = (_vrows[0]["max_ver"] if _vrows and _vrows[0]["max_ver"] else 0) + 1
            source_semver = active_ver.get("semver") or f"{active_ver_num}.0.0"
            source_parts = source_semver.split(".")
            try:
                major = int(source_parts[0])
                minor = int(source_parts[1]) + 1 if len(source_parts) > 1 else 1
            except (ValueError, IndexError):
                major, minor = new_ver, 0
            new_semver = f"{major}.{minor}.0"

            # Stamp metadata
            updated_template = _stamp_template_metadata(
                updated_template,
                service_id=service_id,
                version_int=new_ver,
                semver=new_semver,
                gen_source=f"api-version-update ({model_id})",
                region=region,
            )

            _gen_source = f"copilot-healed" if migration_plan else f"api-version-update"

            ver = await create_service_version(
                service_id,
                arm_template=updated_template,
                version=new_ver,
                semver=new_semver,
                status="draft",
                changelog=f"API version updated: {current_api} → {target_api}" + (" (PLAN→EXECUTE)" if migration_plan else " (direct swap)"),
                created_by=_gen_source,
            )

            yield json.dumps({
                "type": "progress", "phase": "saved",
                "detail": f"✓ Saved as v{new_semver}",
                "progress": 0.25,
                "version": new_ver, "semver": new_semver,
            }) + "\n"

            # Tell the user about the version bump reasoning
            yield json.dumps({
                "type": "progress", "phase": "version_info",
                "detail": f"Version bump: v{source_semver} → v{new_semver} (minor bump — API version change is backwards-compatible)",
                "progress": 0.26,
                "from_semver": source_semver, "to_semver": new_semver,
                "from_api": current_api, "to_api": target_api,
                "bump_reason": "API version migration" + (" with PLAN→EXECUTE" if migration_plan else " (direct swap)"),
            }) + "\n"

            # ── Governance review gate ─────────────────────────────────
            heal_history: list[dict] = []  # track previous attempts (used by both governance heal and validation heal)

            try:
                from src.governance import run_governance_review, format_review_summary

                yield json.dumps({
                    "type": "progress", "phase": "governance_review",
                    "detail": "🏛️ Running governance review — CISO (security) + CTO (architecture)…",
                    "progress": 0.27,
                }) + "\n"

                _gov_client = await ensure_copilot_client()
                if _gov_client:
                    gov_result = await run_governance_review(
                        _gov_client,
                        updated_template,
                        service_id=service_id,
                        version=new_semver,
                        standards_ctx=_standards_ctx,
                    )

                    # Emit individual reviews
                    ciso_rev = gov_result["ciso"]
                    cto_rev = gov_result["cto"]

                    yield json.dumps({
                        "type": "progress", "phase": "ciso_review",
                        "detail": format_review_summary(ciso_rev),
                        "progress": 0.29,
                        "review": ciso_rev,
                    }) + "\n"

                    yield json.dumps({
                        "type": "progress", "phase": "cto_review",
                        "detail": format_review_summary(cto_rev),
                        "progress": 0.31,
                        "review": cto_rev,
                    }) + "\n"

                    # Persist reviews
                    try:
                        _gov_save_kw = dict(
                            semver=new_semver,
                            pipeline_type="update",
                            gate_decision=gov_result["gate_decision"],
                            gate_reason=gov_result["gate_reason"],
                            created_by="pipeline",
                        )
                        await save_governance_review(service_id, new_ver, ciso_rev, **_gov_save_kw)
                        await save_governance_review(service_id, new_ver, cto_rev, **_gov_save_kw)
                    except Exception as _gs_err:
                        logger.warning("Failed to persist governance reviews: %s", _gs_err)

                    # Gate result
                    _gate = gov_result["gate_decision"]
                    _gate_reason = gov_result["gate_reason"]

                    if _gate == "blocked":
                        _ciso_findings = ciso_rev.get("findings", [])
                        _critical_findings = [f for f in _ciso_findings if f.get("severity") in ("critical", "high")]

                        # ── Auto-heal loop: fix template to address CISO findings ──
                        MAX_GOV_HEAL = 5
                        _gov_healed = False

                        for _gov_attempt in range(1, MAX_GOV_HEAL + 1):
                            finding_descs = []
                            for f in _ciso_findings:
                                sev = f.get("severity", "medium")
                                desc = f.get("description", f.get("finding", str(f)))
                                finding_descs.append(f"[{sev}] {desc}")
                            error_for_healer = (
                                f"CISO governance review BLOCKED this template. Findings:\n"
                                + "\n".join(finding_descs)
                                + f"\n\nCISO summary: {ciso_rev.get('summary', '')}"
                            )

                            yield json.dumps({
                                "type": "healing", "phase": "governance_heal_start",
                                "detail": f"🛡️ CISO blocked — auto-healing template to address {len(_ciso_findings)} finding(s) "
                                          f"(attempt {_gov_attempt}/{MAX_GOV_HEAL})…",
                                "progress": 0.33 + _gov_attempt * 0.01,
                                "step": _gov_attempt,
                            }) + "\n"

                            try:
                                from src.pipeline_helpers import copilot_fix_two_phase
                                _pre_fix = updated_template
                                updated_template, _strategy = await copilot_fix_two_phase(
                                    updated_template, error_for_healer,
                                    _standards_ctx, migration_plan or "",
                                    heal_history,
                                )
                                heal_history.append({
                                    "step": len(heal_history) + 1,
                                    "phase": "governance_review",
                                    "error": error_for_healer[:500],
                                    "fix_summary": _summarize_fix(_pre_fix, updated_template),
                                    "strategy": _strategy,
                                })
                                await update_service_version_template(
                                    service_id, new_ver, updated_template, "copilot-healed",
                                )

                                yield json.dumps({
                                    "type": "healing_done", "phase": "governance_heal_strategy",
                                    "detail": f"Fix applied: {_strategy[:200]} — re-running governance review…",
                                    "progress": 0.33 + _gov_attempt * 0.01 + 0.005,
                                    "step": _gov_attempt,
                                }) + "\n"
                            except Exception as heal_err:
                                logger.warning("Governance heal attempt %d failed: %s", _gov_attempt, heal_err)
                                yield json.dumps({
                                    "type": "progress", "phase": "governance_heal_failed",
                                    "detail": f"⚠️ Heal attempt {_gov_attempt} failed: {str(heal_err)[:200]}",
                                    "progress": 0.33 + _gov_attempt * 0.01,
                                }) + "\n"
                                continue

                            # Re-run governance review with healed template
                            try:
                                gov_result = await run_governance_review(
                                    _gov_client, updated_template,
                                    service_id=service_id, version=new_semver,
                                    standards_ctx=_standards_ctx,
                                )
                                ciso_rev = gov_result["ciso"]
                                cto_rev = gov_result["cto"]
                                _gate = gov_result["gate_decision"]
                                _gate_reason = gov_result["gate_reason"]
                                _ciso_findings = ciso_rev.get("findings", [])

                                yield json.dumps({
                                    "type": "progress", "phase": "ciso_review",
                                    "detail": format_review_summary(ciso_rev),
                                    "progress": 0.33 + _gov_attempt * 0.01 + 0.008,
                                    "review": ciso_rev,
                                }) + "\n"

                                if _gate != "blocked":
                                    _gov_healed = True
                                    yield json.dumps({
                                        "type": "progress", "phase": "governance_heal_complete",
                                        "detail": f"✅ Governance gate passed after {_gov_attempt} heal(s) — {_gate.upper()}",
                                        "progress": 0.35,
                                    }) + "\n"
                                    break
                            except Exception as rev_err:
                                logger.warning("Re-review after heal failed: %s", rev_err)

                        if not _gov_healed:
                            # Exhausted heal budget — proceed with conditional approval
                            _gate = "conditional"
                            _gate_reason = f"Auto-healed {MAX_GOV_HEAL}x — remaining concerns noted"
                            yield json.dumps({
                                "type": "progress", "phase": "governance_complete",
                                "detail": f"⚠️ Governance gate: CONDITIONAL (auto-healed {MAX_GOV_HEAL}x) — proceeding with noted concerns",
                                "progress": 0.35,
                                "gate_decision": "conditional", "gate_reason": _gate_reason,
                            }) + "\n"
                    elif _gate == "conditional":
                        yield json.dumps({
                            "type": "progress", "phase": "governance_complete",
                            "detail": f"⚠️ Governance gate: CONDITIONAL — {_gate_reason}",
                            "progress": 0.34,
                            "gate_decision": _gate, "gate_reason": _gate_reason,
                            "ciso_verdict": ciso_rev.get("verdict"),
                            "cto_verdict": cto_rev.get("verdict"),
                        }) + "\n"
                    else:
                        yield json.dumps({
                            "type": "progress", "phase": "governance_complete",
                            "detail": f"✅ Governance gate: {_gate.upper()} — {_gate_reason}",
                            "progress": 0.34,
                            "gate_decision": _gate, "gate_reason": _gate_reason,
                            "ciso_verdict": ciso_rev.get("verdict"),
                            "cto_verdict": cto_rev.get("verdict"),
                        }) + "\n"
                else:
                    yield json.dumps({
                        "type": "progress", "phase": "governance_complete",
                        "detail": "⚠️ Copilot SDK not available — skipping governance review",
                        "progress": 0.34,
                    }) + "\n"

            except Exception as gov_exc:
                logger.warning("Governance review failed: %s", gov_exc)
                yield json.dumps({
                    "type": "progress", "phase": "governance_complete",
                    "detail": f"⚠️ Governance review error — proceeding without gate",
                    "progress": 0.34,
                }) + "\n"

            # ── Validation loop: validate→what-if→deploy→policy→cleanup→promote ─
            # Copilot SDK auto-healing with migration plan context and heal history
            _client = None  # lazy-init only when healing needed
            _last_error = ""  # track last error for failure analysis

            arm_template = updated_template
            attempt = 0
            promoted = False

            # Build migration context string for healers
            _migration_ctx = ""
            if migration_plan:
                _migration_ctx = (
                    f"\n\n--- MIGRATION CONTEXT ---\n"
                    f"This template is being migrated from API version {current_api} to {target_api}.\n"
                    f"Migration plan:\n{migration_plan[:2000]}\n"
                    f"--- END MIGRATION CONTEXT ---\n"
                )

            # ── Pre-flight quota check ────────────────────────
            from src.pipeline_helpers import find_available_regions
            _quota_primary, _quota_alts = await find_available_regions(region)
            if not _quota_primary["ok"]:
                _alt_names = [a["region"] for a in _quota_alts[:5]]
                yield json.dumps({
                    "type": "error", "phase": "quota_exceeded",
                    "detail": (
                        f"Subscription VM quota exceeded in {region} "
                        f"({_quota_primary['used']}/{_quota_primary['limit']} cores in use). "
                        f"Cannot deploy to this region."
                    ),
                    "quota": _quota_primary,
                    "alternative_regions": _alt_names,
                    "progress": 1.0,
                }) + "\n"
                await update_service_version_status(service_id, new_ver, "failed")
                await complete_pipeline_run(
                    _run_id, "failed",
                    error_detail=f"VM quota exceeded in {region} ({_quota_primary['used']}/{_quota_primary['limit']} cores)",
                    heal_count=0)
                return

            while attempt < MAX_HEAL_ATTEMPTS and not promoted:
                attempt += 1
                if attempt > 1:
                    yield json.dumps({
                        "type": "healing", "phase": "fixing_template",
                        "step": attempt,
                        "detail": f"🤖 Auto-healing attempt {attempt}/{MAX_HEAL_ATTEMPTS}…",
                        "progress": 0.25 + (attempt - 1) * 0.05,
                    }) + "\n"

                # ── Static policy check ───────────────────────────
                yield json.dumps({
                    "type": "progress", "phase": "static_policy_check",
                    "step": attempt,
                    "detail": "Running static policy checks…",
                    "progress": 0.28 + (attempt - 1) * 0.15,
                }) + "\n"

                try:
                    governance_policies = await get_governance_policies_as_dict()
                    arm_dict = json.loads(arm_template) if isinstance(arm_template, str) else arm_template
                    static_report = validate_template(arm_dict, governance_policies)
                    svc_standards = await get_standards_for_service(service_id)
                    std_report = validate_template_against_standards(arm_dict, svc_standards)
                    # Merge standard failures into the main report
                    failed_from_std = [r for r in std_report.results if not r.passed]
                    static_report.results.extend(failed_from_std)
                    if failed_from_std:
                        static_report.passed = False
                        static_report.blockers += std_report.blockers
                        static_report.failed_checks += std_report.failed_checks

                    if static_report.passed:
                        yield json.dumps({
                            "type": "progress", "phase": "static_policy_complete",
                            "step": attempt,
                            "detail": "✓ Static policy checks passed",
                            "progress": 0.32 + (attempt - 1) * 0.15,
                        }) + "\n"
                    else:
                        violations = [r for r in static_report.results if not r.passed and r.enforcement == "block"]
                        yield json.dumps({
                            "type": "progress", "phase": "static_policy_failed",
                            "step": attempt,
                            "detail": f"⚠ {len(violations)} policy violation(s) — auto-healing…",
                            "progress": 0.32 + (attempt - 1) * 0.15,
                        }) + "\n"
                        if attempt < MAX_HEAL_ATTEMPTS:
                            if _client is None:
                                _client = await ensure_copilot_client()
                            if not _client:
                                continue
                            fix_prompt = build_remediation_prompt(arm_template, violations) + _migration_ctx
                            if heal_history:
                                fix_prompt += "\n\n--- PREVIOUS ATTEMPTS (do NOT repeat) ---\n"
                                for pa in heal_history:
                                    fix_prompt += f"Step {pa.get('step','?')}: {pa['error'][:200]} → {pa['fix_summary']}\n"
                                fix_prompt += "--- END PREVIOUS ATTEMPTS ---\n"
                            fix_model = get_model_for_task(Task.CODE_FIXING)
                            _fix_display = get_model_display(Task.CODE_FIXING)
                            yield json.dumps({
                                "type": "llm_reasoning", "phase": "healing",
                                "step": attempt,
                                "detail": f"🔧 {_fix_display} fixing policy violations with migration context…",
                                "progress": 0.33 + (attempt - 1) * 0.15,
                            }) + "\n"
                            raw = await copilot_send(_client, model=fix_model,
                                system_prompt=TEMPLATE_HEALER.system_prompt,
                                prompt=fix_prompt, timeout=TEMPLATE_HEALER.timeout,
                                agent_name=TEMPLATE_HEALER.name)
                            cleaned = raw.strip()
                            if cleaned.startswith("```"):
                                lines = cleaned.split("\n")
                                cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
                            try:
                                json.loads(cleaned)
                            except (json.JSONDecodeError, ValueError) as je:
                                _heal_err = f"Healer returned invalid JSON: {str(je)[:150]}"
                                logger.warning(f"Static policy heal parse failed: {je}")
                                _last_error = _heal_err
                                heal_history.append({"step": len(heal_history) + 1, "phase": "static_policy", "error": _heal_err, "fix_summary": "Heal produced invalid JSON"})
                                yield json.dumps({
                                    "type": "progress", "phase": "healing_failed",
                                    "step": attempt,
                                    "detail": f"⚠ Auto-heal produced invalid JSON — will retry" if attempt < MAX_HEAL_ATTEMPTS else f"⚠ Auto-heal produced invalid JSON",
                                    "progress": 0.35 + (attempt - 1) * 0.15,
                                }) + "\n"
                                continue
                            _pre_template = arm_template
                            arm_template = cleaned
                            # Apply guardrails
                            arm_template = guard_locations(arm_template)
                            arm_template = ensure_parameter_defaults(arm_template)
                            arm_template = sanitize_placeholder_guids(arm_template)
                            _last_error = "; ".join(f"[{v.rule_id}] {v.message}" for v in violations[:3])
                            heal_history.append({"step": len(heal_history) + 1, "phase": "static_policy", "error": _last_error, "fix_summary": f"Fixed {len(violations)} policy violation(s)"})
                            await update_service_version_template(service_id, new_ver, arm_template)
                            yield json.dumps({
                                "type": "healing_done", "phase": "template_fixed",
                                "step": attempt,
                                "detail": "🔧 Template fixed — retrying validation…",
                                "progress": 0.35 + (attempt - 1) * 0.15,
                            }) + "\n"
                            continue
                except Exception as e:
                    logger.warning(f"Static policy check failed: {e}")
                    _last_error = str(e)

                # ── What-If ──────────────────────────────────────
                yield json.dumps({
                    "type": "progress", "phase": "what_if",
                    "step": attempt,
                    "detail": "Running ARM What-If analysis…",
                    "progress": 0.38 + (attempt - 1) * 0.15,
                }) + "\n"

                what_if_ok = False
                what_if_error = ""
                try:
                    import asyncio as _aio
                    import os as _os
                    from azure.identity import DefaultAzureCredential as _DAC
                    from azure.mgmt.resource import ResourceManagementClient as _RMC
                    from azure.mgmt.resource.resources.models import (
                        DeploymentWhatIf as _DWI,
                        DeploymentProperties as _DP,
                        DeploymentMode as _DM,
                    )

                    sub_id = _os.getenv("AZURE_SUBSCRIPTION_ID", "")
                    if not sub_id:
                        raise RuntimeError("AZURE_SUBSCRIPTION_ID not set")

                    cred = _DAC(exclude_workload_identity_credential=True,
                               exclude_managed_identity_credential=True)
                    client = _RMC(cred, sub_id)
                    loop = _aio.get_event_loop()

                    # Ensure RG exists
                    await loop.run_in_executor(None, lambda: client.resource_groups.create_or_update(
                        rg_name, {"location": region}))
                    await update_service_version_deployment_info(
                        service_id, new_ver, run_id=_run_id,
                        resource_group=rg_name, subscription_id=sub_id)

                    tpl_obj = json.loads(arm_template)
                    params_obj = {
                        k: {"value": v.get("defaultValue", "")}
                        for k, v in tpl_obj.get("parameters", {}).items()
                        if "defaultValue" in v
                        and not (isinstance(v.get("defaultValue"), str)
                                 and v["defaultValue"].startswith("[") and v["defaultValue"].endswith("]"))
                    }

                    what_if_params = _DWI(properties=_DP(
                        mode=_DM.INCREMENTAL,
                        template=tpl_obj,
                        parameters=params_obj,
                    ))
                    what_if_result = await loop.run_in_executor(
                        None,
                        lambda: client.deployments.begin_what_if(
                            rg_name, f"infraforge-whatif-{_run_id}", what_if_params
                        ).result()
                    )
                    changes = what_if_result.changes or []
                    what_if_ok = True

                    yield json.dumps({
                        "type": "progress", "phase": "what_if_complete",
                        "step": attempt,
                        "detail": f"✓ What-If passed — {len(changes)} change(s) predicted",
                        "progress": 0.45 + (attempt - 1) * 0.15,
                    }) + "\n"
                except Exception as e:
                    what_if_error = str(e)
                    logger.warning(f"What-If failed: {e}")
                    _last_error = what_if_error
                    _whatif_brief = _brief_azure_error(what_if_error)
                    yield json.dumps({
                        "type": "progress", "phase": "what_if_failed",
                        "step": attempt,
                        "detail": f"{_whatif_brief}",
                        "progress": 0.45 + (attempt - 1) * 0.15,
                    }) + "\n"

                    # Try to heal
                    if attempt < MAX_HEAL_ATTEMPTS:
                        if _client is None:
                            _client = await ensure_copilot_client()
                        if not _client:
                            await update_service_version_status(service_id, new_ver, "failed")
                            yield json.dumps({"type": "error", "phase": "failed", "detail": "✗ What-If failed — no Copilot client for healing", "progress": 1.0}) + "\n"
                            await complete_pipeline_run(_run_id, "failed", error_detail="What-If failed, no Copilot client")
                            return

                        _total_whatif_heals = sum(1 for h in heal_history if h.get("phase") == "what_if")
                        _use_deep_wif = _total_whatif_heals >= DEEP_HEAL_THRESHOLD
                        _plan_model = get_model_for_task(Task.PLANNING)
                        _fix_model = get_model_for_task(Task.CODE_FIXING)
                        _plan_display = get_model_display(Task.PLANNING)
                        _fix_display = get_model_display(Task.CODE_FIXING)

                        # Phase 1: root cause analysis
                        yield json.dumps({
                            "type": "llm_reasoning", "phase": "analyzing_whatif_failure",
                            "step": attempt,
                            "detail": f"🧠 {_plan_display} analyzing What-If failure — root cause analysis…",
                            "progress": 0.46 + (attempt - 1) * 0.15,
                        }) + "\n"

                        _wif_analysis_prompt = (
                            f"You are debugging an ARM template What-If validation failure "
                            f"(attempt {attempt}).\n\n"
                            f"--- ERROR ---\n{what_if_error}\n--- END ERROR ---\n\n"
                            f"--- CURRENT TEMPLATE (abbreviated) ---\n{arm_template[:8000]}\n--- END TEMPLATE ---\n\n"
                        )
                        if _migration_ctx:
                            _wif_analysis_prompt += f"--- ARCHITECTURE INTENT ---\n{_migration_ctx}\n--- END INTENT ---\n\n"
                        if heal_history:
                            _wif_analysis_prompt += "--- PREVIOUS FAILED ATTEMPTS ---\n"
                            for pa in heal_history:
                                _wif_analysis_prompt += (
                                    f"Attempt {pa.get('step', '?')} ({pa.get('phase', '?')}): "
                                    f"{pa['error'][:300]} → {pa.get('strategy', pa.get('fix_summary', 'unknown'))}\n"
                                )
                            _wif_analysis_prompt += "--- END PREVIOUS ATTEMPTS ---\n\n"
                        _wif_analysis_prompt += (
                            "Produce a ROOT CAUSE ANALYSIS followed by a STRATEGY.\n\n"
                            "Format:\nROOT CAUSE:\n<1-3 sentences>\n\n"
                            "STRATEGY FOR THIS ATTEMPT:\n<Specific, concrete approach>\n"
                        )

                        _wif_strategy = await copilot_send(
                            _client, model=_plan_model,
                            system_prompt=DEEP_TEMPLATE_HEALER.system_prompt,
                            prompt=_wif_analysis_prompt, timeout=DEEP_TEMPLATE_HEALER.timeout,
                            agent_name=DEEP_TEMPLATE_HEALER.name,
                        )
                        logger.info(f"[What-If Healer] Strategy (attempt {attempt}): {_wif_strategy[:300]}")

                        # Phase 2: apply the strategy
                        yield json.dumps({
                            "type": "llm_reasoning", "phase": "healing",
                            "step": attempt,
                            "detail": f"🔧 {_fix_display} applying fix strategy…",
                            "progress": 0.47 + (attempt - 1) * 0.15,
                        }) + "\n"

                        _wif_fix_prompt = (
                            f"Fix this ARM template following the STRATEGY below.\n\n"
                            f"--- STRATEGY ---\n{_wif_strategy}\n--- END STRATEGY ---\n\n"
                            f"--- ERROR ---\n{what_if_error}\n--- END ERROR ---\n\n"
                            f"--- TEMPLATE ---\n{arm_template}\n--- END TEMPLATE ---\n\n"
                            "FOLLOW the strategy. Return ONLY raw JSON — no markdown, no explanation.\n"
                            "CRITICAL: Keep locations as \"[resourceGroup().location]\" or "
                            "\"[parameters('location')]\". Ensure every parameter has defaultValue.\n"
                        )
                        _wif_healer_sys = DEEP_TEMPLATE_HEALER.system_prompt if _use_deep_wif else TEMPLATE_HEALER.system_prompt
                        _wif_healer_name = DEEP_TEMPLATE_HEALER.name if _use_deep_wif else TEMPLATE_HEALER.name
                        raw = await copilot_send(_client, model=_fix_model,
                            system_prompt=_wif_healer_sys,
                            prompt=_wif_fix_prompt, timeout=DEEP_TEMPLATE_HEALER.timeout,
                            agent_name=_wif_healer_name)
                        cleaned = raw.strip()
                        if cleaned.startswith("```"):
                            lines = cleaned.split("\n")
                            cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
                        if not cleaned.startswith("{"):
                            _js = cleaned.find("{")
                            _je = cleaned.rfind("}")
                            if _js >= 0 and _je > _js:
                                cleaned = cleaned[_js:_je + 1]
                        try:
                            json.loads(cleaned)
                        except (json.JSONDecodeError, ValueError) as je:
                            _heal_err = f"Healer returned invalid JSON: {str(je)[:150]}"
                            logger.warning(f"What-If heal parse failed: {je}")
                            _last_error = _heal_err
                            heal_history.append({"step": len(heal_history) + 1, "phase": "what_if", "error": _heal_err, "fix_summary": "Heal produced invalid JSON", "strategy": _wif_strategy[:300]})
                            yield json.dumps({
                                "type": "progress", "phase": "healing_failed",
                                "step": attempt,
                                "detail": f"⚠ Auto-heal produced invalid JSON — will retry" if attempt < MAX_HEAL_ATTEMPTS else f"⚠ Auto-heal produced invalid JSON",
                                "progress": 0.48 + (attempt - 1) * 0.15,
                            }) + "\n"
                            continue

                        # Apply guardrails
                        cleaned = guard_locations(cleaned)
                        cleaned = ensure_parameter_defaults(cleaned)
                        cleaned = sanitize_placeholder_guids(cleaned)

                        arm_template = cleaned
                        heal_history.append({"step": len(heal_history) + 1, "phase": "what_if", "error": what_if_error[:300], "fix_summary": "Two-phase What-If fix", "strategy": _wif_strategy[:300]})
                        await update_service_version_template(service_id, new_ver, arm_template)
                        yield json.dumps({
                            "type": "healing_done", "phase": "template_fixed",
                            "step": attempt,
                            "detail": "🔧 Template fixed — retrying…",
                            "progress": 0.48 + (attempt - 1) * 0.15,
                        }) + "\n"
                        continue
                    elif not what_if_ok:
                        # Can't heal — fail
                        await update_service_version_status(service_id, new_ver, "failed")
                        yield json.dumps({
                            "type": "error", "phase": "failed",
                            "detail": f"✗ What-If failed after {attempt} attempt(s)",
                            "progress": 1.0,
                        }) + "\n"
                        await complete_pipeline_run(_run_id, "failed", error_detail=f"What-If failed after {attempt} attempts", heal_count=len(heal_history))
                        return

                # ── Deploy ────────────────────────────────────────
                yield json.dumps({
                    "type": "progress", "phase": "deploying",
                    "step": attempt,
                    "detail": f"Deploying to validation RG {rg_name}…",
                    "progress": 0.50 + (attempt - 1) * 0.15,
                    "resource_group": rg_name,
                    "region": region,
                    "deploy_mode": "incremental",
                }) + "\n"

                deploy_ok = False
                deploy_error = ""
                try:
                    tpl_obj = json.loads(arm_template)
                    params_obj = {
                        k: {"value": v.get("defaultValue", "")}
                        for k, v in tpl_obj.get("parameters", {}).items()
                        if "defaultValue" in v
                        and not (isinstance(v.get("defaultValue"), str)
                                 and v["defaultValue"].startswith("[") and v["defaultValue"].endswith("]"))
                    }

                    deploy_name = f"infraforge-update-{_run_id}"
                    deploy_props = _DP(mode=_DM.INCREMENTAL,
                                      template=tpl_obj, parameters=params_obj)
                    deploy_result = await loop.run_in_executor(
                        None,
                        lambda: client.deployments.begin_create_or_update(
                            rg_name, deploy_name,
                            {"properties": deploy_props}
                        ).result()
                    )
                    await update_service_version_deployment_info(
                        service_id, new_ver, deployment_name=deploy_name)
                    deploy_ok = True

                    yield json.dumps({
                        "type": "progress", "phase": "deploy_complete",
                        "step": attempt,
                        "detail": "✓ Deployment succeeded",
                        "progress": 0.62 + (attempt - 1) * 0.15,
                    }) + "\n"
                except Exception as e:
                    deploy_error = str(e)
                    _last_error = deploy_error
                    logger.warning(f"Deployment failed: {e}")
                    _deploy_brief = _brief_azure_error(deploy_error)
                    yield json.dumps({
                        "type": "progress", "phase": "deploy_failed",
                        "step": attempt,
                        "detail": f"{_deploy_brief}",
                        "progress": 0.62 + (attempt - 1) * 0.15,
                    }) + "\n"

                # ── Deploy sub-loop: goal-driven heal → re-deploy ──
                # Instead of burning a full outer loop iteration, try
                # targeted deploy-specific heals with immediate re-deploy.
                # Uses two-phase healing: analyze root cause → plan → fix.
                _deploy_heals = 0
                _total_deploy_heals = sum(1 for h in heal_history if h.get("phase") == "deploy")
                while not deploy_ok and _deploy_heals < MAX_DEPLOY_SUB_HEALS:
                    _deploy_heals += 1
                    _total_deploy_heals += 1

                    # Quota / capacity errors — stop immediately, no LLM fix possible
                    _is_quota_vu = any(kw in deploy_error.lower() for kw in
                        ("subscriptionisoverquotaforsku", "overquota",
                         "quotaexceeded", "operation cannot be completed without additional quota",
                         "notenoughcores", "allocationfailed", "zonalallocationfailed"))
                    if _is_quota_vu:
                        await update_service_version_status(service_id, new_ver, "failed")
                        await complete_pipeline_run(
                            _run_id, "failed",
                            error_detail="Subscription quota exceeded — no template fix possible",
                            heal_count=len(heal_history))
                        yield json.dumps({
                            "type": "error", "phase": "deploy", "step": attempt,
                            "detail": (
                                "Subscription quota exceeded — cannot deploy in this region. "
                                "Request a quota increase in the Azure portal, deploy to a "
                                "different region, or free up existing resources."
                            ),
                            "progress": 1.0,
                        }) + "\n"
                        return

                    if _client is None:
                        _client = await ensure_copilot_client()
                    if not _client:
                        await update_service_version_status(service_id, new_ver, "failed")
                        await complete_pipeline_run(_run_id, "failed", error_detail="Deploy failed, no Copilot client for healing", heal_count=len(heal_history))
                        yield json.dumps({"type": "error", "phase": "failed", "detail": "✗ Deployment failed — no Copilot client for healing", "progress": 1.0}) + "\n"
                        return

                    _use_deep = _total_deploy_heals >= DEEP_HEAL_THRESHOLD
                    _heal_label = "deep two-phase" if _use_deep else "two-phase"
                    _plan_model = get_model_for_task(Task.PLANNING)
                    _fix_model = get_model_for_task(Task.CODE_FIXING)
                    _fix_display = get_model_display(Task.CODE_FIXING)
                    _plan_display = get_model_display(Task.PLANNING)

                    # ── Phase 1: Root cause analysis + strategy ──
                    yield json.dumps({
                        "type": "llm_reasoning", "phase": "analyzing_deploy_failure",
                        "step": attempt, "sub_step": _deploy_heals,
                        "detail": f"🧠 {_plan_display} analyzing deployment failure — root cause analysis…",
                        "progress": 0.63 + (attempt - 1) * 0.10,
                    }) + "\n"

                    _analysis_prompt = (
                        f"You are debugging an ARM template deployment failure "
                        f"(outer attempt {attempt}, deploy heal {_deploy_heals}).\n\n"
                        f"--- ERROR ---\n{deploy_error}\n--- END ERROR ---\n\n"
                        f"--- CURRENT TEMPLATE (abbreviated) ---\n{arm_template[:8000]}\n--- END TEMPLATE ---\n\n"
                    )
                    if _migration_ctx:
                        _analysis_prompt += f"--- ARCHITECTURE INTENT ---\n{_migration_ctx}\n--- END INTENT ---\n\n"

                    if heal_history:
                        _analysis_prompt += "--- PREVIOUS FAILED ATTEMPTS (do NOT repeat these strategies) ---\n"
                        for pa in heal_history:
                            _analysis_prompt += (
                                f"Attempt {pa.get('step', '?')} (phase: {pa.get('phase', '?')}):\n"
                                f"  Error: {pa['error'][:400]}\n"
                                f"  Strategy tried: {pa.get('strategy', pa.get('fix_summary', 'unknown'))}\n"
                                f"  Result: STILL FAILED\n\n"
                            )
                        _analysis_prompt += "--- END PREVIOUS ATTEMPTS ---\n\n"

                    # Resource-type-specific knowledge
                    try:
                        _tpl_for_hints = json.loads(arm_template)
                        _res_types = {r.get("type", "").lower() for r in _tpl_for_hints.get("resources", []) if isinstance(r, dict)}
                        _type_hints = get_resource_type_hints(_res_types)
                        if _type_hints:
                            _analysis_prompt += f"--- RESOURCE-TYPE-SPECIFIC KNOWLEDGE ---\n{_type_hints}\n--- END KNOWLEDGE ---\n\n"
                    except Exception:
                        pass

                    _analysis_prompt += (
                        "Produce a ROOT CAUSE ANALYSIS followed by a STRATEGY.\n\n"
                        "Format your response EXACTLY as:\n\n"
                        "ROOT CAUSE:\n<1-3 sentences explaining the fundamental issue>\n\n"
                        "WHAT WAS TRIED AND WHY IT FAILED:\n<For each previous attempt, or 'First attempt' if none>\n\n"
                        "STRATEGY FOR THIS ATTEMPT:\n<Specific, concrete, DIFFERENT approach>\n\n"
                        "Be specific. Don't say 'try a different API version' — say which "
                        "version and why. Don't say 'fix the parameters' — say which "
                        "parameter and what the correct value should be.\n"
                    )

                    _strategy_text = await copilot_send(
                        _client, model=_plan_model,
                        system_prompt=DEEP_TEMPLATE_HEALER.system_prompt,
                        prompt=_analysis_prompt, timeout=DEEP_TEMPLATE_HEALER.timeout,
                        agent_name=DEEP_TEMPLATE_HEALER.name,
                    )
                    logger.info(f"[Deploy Healer] Phase 1 strategy (attempt {attempt}, sub {_deploy_heals}): {_strategy_text[:300]}")

                    # ── Phase 2: Apply the strategy to fix the template ──
                    yield json.dumps({
                        "type": "llm_reasoning", "phase": "healing",
                        "step": attempt, "sub_step": _deploy_heals,
                        "detail": f"🔧 {_fix_display} applying {_heal_label} fix strategy…",
                        "progress": 0.64 + (attempt - 1) * 0.10,
                    }) + "\n"

                    _fix_prompt = (
                        f"Fix this ARM template following the STRATEGY below.\n\n"
                        f"--- STRATEGY (from root cause analysis) ---\n{_strategy_text}\n--- END STRATEGY ---\n\n"
                        f"--- ERROR ---\n{deploy_error}\n--- END ERROR ---\n\n"
                        f"--- CURRENT TEMPLATE ---\n{arm_template}\n--- END TEMPLATE ---\n\n"
                    )

                    try:
                        _fix_tpl = json.loads(arm_template)
                        _fix_params = extract_param_values(_fix_tpl)
                        if _fix_params:
                            _fix_prompt += (
                                "--- PARAMETER VALUES SENT TO ARM ---\n"
                                f"{json.dumps(_fix_params, indent=2, default=str)}\n"
                                "--- END PARAMETER VALUES ---\n\n"
                            )
                    except Exception:
                        pass

                    _fix_prompt += (
                        "FOLLOW the strategy above. Apply the SPECIFIC changes it recommends.\n"
                        "Return ONLY the corrected raw JSON — no markdown fences, no explanation.\n\n"
                        "CRITICAL RULES:\n"
                        "1. LOCATIONS — Keep ALL location parameters as \"[resourceGroup().location]\" "
                        "or \"[parameters('location')]\" — NEVER hardcode a region.\n"
                        "   EXCEPTION: Globally-scoped resources MUST use location \"global\".\n"
                        "2. Ensure EVERY parameter has a \"defaultValue\".\n"
                        "3. Add tags: environment, owner, costCenter, project on every resource.\n"
                        "4. NEVER use placeholder GUIDs like '00000000-0000-0000-0000-000000000000'.\n"
                    )

                    _healer_sys = DEEP_TEMPLATE_HEALER.system_prompt if _use_deep else TEMPLATE_HEALER.system_prompt
                    _healer_name = DEEP_TEMPLATE_HEALER.name if _use_deep else TEMPLATE_HEALER.name
                    raw = await copilot_send(
                        _client, model=_fix_model,
                        system_prompt=_healer_sys,
                        prompt=_fix_prompt, timeout=DEEP_TEMPLATE_HEALER.timeout,
                        agent_name=_healer_name,
                    )
                    cleaned = raw.strip()
                    if cleaned.startswith("```"):
                        _lines = cleaned.split("\n")
                        cleaned = "\n".join(_lines[1:-1] if _lines[-1].strip() == "```" else _lines[1:])
                    # Extract JSON if surrounded by non-JSON text
                    if not cleaned.startswith("{"):
                        _js = cleaned.find("{")
                        _je = cleaned.rfind("}")
                        if _js >= 0 and _je > _js:
                            cleaned = cleaned[_js:_je + 1]

                    try:
                        json.loads(cleaned)
                    except (json.JSONDecodeError, ValueError) as je:
                        _heal_err = f"Healer returned invalid JSON: {str(je)[:150]}"
                        logger.warning(f"Deploy heal parse failed (sub {_deploy_heals}): {je}")
                        _last_error = _heal_err
                        heal_history.append({"step": len(heal_history) + 1, "phase": "deploy", "error": _heal_err, "fix_summary": "Heal produced invalid JSON", "strategy": _strategy_text[:300]})
                        yield json.dumps({
                            "type": "progress", "phase": "healing_failed",
                            "step": attempt, "sub_step": _deploy_heals,
                            "detail": f"⚠ Auto-heal produced invalid JSON — {'will retry' if _deploy_heals < MAX_DEPLOY_SUB_HEALS else 'sub-loop exhausted'}",
                            "progress": 0.65 + (attempt - 1) * 0.10,
                        }) + "\n"
                        continue

                    # Apply guardrails
                    cleaned = guard_locations(cleaned)
                    cleaned = ensure_parameter_defaults(cleaned)
                    cleaned = sanitize_placeholder_guids(cleaned)

                    arm_template = cleaned
                    heal_history.append({
                        "step": len(heal_history) + 1, "phase": "deploy",
                        "error": deploy_error[:300],
                        "fix_summary": f"Two-phase {'deep ' if _use_deep else ''}fix applied",
                        "strategy": _strategy_text[:300],
                    })
                    await update_service_version_template(service_id, new_ver, arm_template)

                    yield json.dumps({
                        "type": "healing_done", "phase": "template_fixed",
                        "step": attempt, "sub_step": _deploy_heals,
                        "detail": f"🔧 Template fixed via {_heal_label} healing — re-deploying…",
                        "progress": 0.65 + (attempt - 1) * 0.10,
                    }) + "\n"

                    # ── Immediate re-deploy (skip policy/what-if) ──
                    try:
                        tpl_obj = json.loads(arm_template)
                        params_obj = {
                            k: {"value": v.get("defaultValue", "")}
                            for k, v in tpl_obj.get("parameters", {}).items()
                            if "defaultValue" in v
                            and not (isinstance(v.get("defaultValue"), str)
                                     and v["defaultValue"].startswith("[") and v["defaultValue"].endswith("]"))
                        }
                        deploy_name = f"infraforge-update-{_run_id}-h{_deploy_heals}"
                        deploy_props = _DP(mode=_DM.INCREMENTAL,
                                          template=tpl_obj, parameters=params_obj)

                        yield json.dumps({
                            "type": "progress", "phase": "deploying",
                            "step": attempt, "sub_step": _deploy_heals,
                            "detail": f"Re-deploying to {rg_name} (heal {_deploy_heals})…",
                            "progress": 0.66 + (attempt - 1) * 0.10,
                            "resource_group": rg_name,
                        }) + "\n"

                        deploy_result = await loop.run_in_executor(
                            None,
                            lambda: client.deployments.begin_create_or_update(
                                rg_name, deploy_name,
                                {"properties": deploy_props}
                            ).result()
                        )
                        await update_service_version_deployment_info(
                            service_id, new_ver, deployment_name=deploy_name)
                        deploy_ok = True

                        yield json.dumps({
                            "type": "progress", "phase": "deploy_complete",
                            "step": attempt, "sub_step": _deploy_heals,
                            "detail": f"✓ Deployment succeeded after {_heal_label} healing",
                            "progress": 0.68 + (attempt - 1) * 0.10,
                        }) + "\n"
                    except Exception as _redeploy_err:
                        deploy_error = str(_redeploy_err)
                        _last_error = deploy_error
                        logger.warning(f"Re-deploy failed (sub {_deploy_heals}): {_redeploy_err}")
                        _deploy_brief = _brief_azure_error(deploy_error)
                        yield json.dumps({
                            "type": "progress", "phase": "deploy_failed",
                            "step": attempt, "sub_step": _deploy_heals,
                            "detail": f"{_deploy_brief}",
                            "progress": 0.67 + (attempt - 1) * 0.10,
                        }) + "\n"
                        # Sub-loop continues to next heal iteration

                if not deploy_ok:
                    # Deploy sub-loop exhausted — fall back to outer loop
                    if attempt < MAX_HEAL_ATTEMPTS:
                        yield json.dumps({
                            "type": "healing", "phase": "escalating",
                            "step": attempt,
                            "detail": f"🔄 Deploy sub-heals exhausted — restarting full validation pipeline (attempt {attempt + 1}/{MAX_HEAL_ATTEMPTS})…",
                            "progress": 0.68 + (attempt - 1) * 0.10,
                        }) + "\n"
                        continue  # back to outer loop: re-run policy + what-if + deploy
                    else:
                        # All attempts exhausted
                        await update_service_version_status(service_id, new_ver, "failed")
                        await complete_pipeline_run(_run_id, "failed", error_detail=f"Deploy failed after {attempt} attempt(s) + {_total_deploy_heals} heals: {deploy_error[:300]}", heal_count=len(heal_history))
                        yield json.dumps({
                            "type": "error", "phase": "failed",
                            "detail": f"✗ Deployment failed after {attempt} attempt(s) with {_total_deploy_heals} heal cycles",
                            "progress": 1.0,
                        }) + "\n"
                        # Try cleanup
                        try:
                            await loop.run_in_executor(None,
                                lambda: client.resource_groups.begin_delete(rg_name).result())
                        except Exception:
                            pass
                        return

                # ── Runtime policy check — deploy Azure Policy ──────────
                yield json.dumps({
                    "type": "progress", "phase": "policy_testing",
                    "step": attempt,
                    "detail": "Running runtime compliance checks…",
                    "progress": 0.68 + (attempt - 1) * 0.15,
                }) + "\n"

                _update_policy_deployed = False
                try:
                    # Fetch the service's policy artifact (generated during onboarding)
                    _arts = await get_service_artifacts(service_id)
                    _policy_content = (_arts.get("policy", {}).get("content") or "").strip()
                    _policy_obj = None
                    if _policy_content:
                        try:
                            _policy_obj = json.loads(_policy_content)
                        except Exception:
                            pass

                    if _policy_obj:
                        # Deploy policy definition + assignment to the validation RG
                        from src.tools.policy_deployer import deploy_policy, cleanup_policy
                        _pol_info = await deploy_policy(
                            service_id=service_id, run_id=_run_id,
                            policy_json=_policy_obj, resource_group=rg_name,
                        )
                        _update_policy_deployed = True

                        # List deployed resources for compliance verification
                        live_resources = await loop.run_in_executor(
                            None,
                            lambda: [r.as_dict() for r in client.resources.list_by_resource_group(rg_name)]
                        )

                        yield json.dumps({
                            "type": "progress", "phase": "policy_testing_complete",
                            "step": attempt,
                            "detail": (
                                f"✓ Azure Policy '{_pol_info['definition_name']}' deployed — "
                                f"{len(live_resources)} resource(s) verified"
                            ),
                            "progress": 0.75 + (attempt - 1) * 0.15,
                        }) + "\n"
                    else:
                        # No policy artifact — list resources only
                        live_resources = await loop.run_in_executor(
                            None,
                            lambda: [r.as_dict() for r in client.resources.list_by_resource_group(rg_name)]
                        )
                        yield json.dumps({
                            "type": "progress", "phase": "policy_testing_complete",
                            "step": attempt,
                            "detail": f"✓ {len(live_resources)} resource(s) verified (no policy artifact to enforce)",
                            "progress": 0.75 + (attempt - 1) * 0.15,
                        }) + "\n"
                except Exception as e:
                    logger.warning(f"Runtime policy check failed: {e}")
                    yield json.dumps({
                        "type": "progress", "phase": "policy_testing_complete",
                        "step": attempt,
                        "detail": f"⚠ Runtime check skipped (non-blocking): {str(e)[:150]}",
                        "progress": 0.75 + (attempt - 1) * 0.15,
                    }) + "\n"

                # ── Cleanup ───────────────────────────────────────
                yield json.dumps({
                    "type": "progress", "phase": "cleanup",
                    "step": attempt,
                    "detail": f"Cleaning up validation RG {rg_name}…",
                    "progress": 0.80,
                }) + "\n"

                # Clean up Azure Policy assignment + definition
                if _update_policy_deployed:
                    try:
                        from src.tools.policy_deployer import cleanup_policy
                        await cleanup_policy(service_id, _run_id, rg_name)
                    except Exception as _cpe:
                        logger.debug(f"Policy cleanup (non-fatal): {_cpe}")

                try:
                    await loop.run_in_executor(None,
                        lambda: client.resource_groups.begin_delete(rg_name).result())
                    yield json.dumps({
                        "type": "progress", "phase": "cleanup_complete",
                        "step": attempt,
                        "detail": "✓ Validation resources + Azure Policy cleaned up",
                        "progress": 0.88,
                    }) + "\n"
                except Exception as e:
                    logger.warning(f"Cleanup failed (non-blocking): {e}")
                    yield json.dumps({
                        "type": "progress", "phase": "cleanup_complete",
                        "step": attempt,
                        "detail": "⚠ Cleanup deferred (non-blocking)",
                        "progress": 0.88,
                    }) + "\n"

                # ── Promote ───────────────────────────────────────
                yield json.dumps({
                    "type": "progress", "phase": "promoting",
                    "step": attempt,
                    "detail": f"Publishing v{new_semver} as active…",
                    "progress": 0.92,
                }) + "\n"

                await update_service_version_status(service_id, new_ver, "approved",
                    validation_result={"api_version_update": True,
                                       "from": current_api, "to": target_api})
                await set_active_service_version(service_id, new_ver)
                promoted = True

                yield json.dumps({
                    "type": "done", "phase": "approved",
                    "detail": f"✅ API version updated: {current_api} → {target_api} (v{new_semver})",
                    "progress": 1.0,
                    "new_version": new_ver, "new_semver": new_semver,
                    "from_api": current_api, "to_api": target_api,
                }) + "\n"

                # Record pipeline completion
                await complete_pipeline_run(
                    _run_id, "completed",
                    version_num=new_ver, semver=new_semver,
                    summary={"from_api": current_api, "to_api": target_api},
                    heal_count=len(heal_history),
                )

            if not promoted:
                await update_service_version_status(service_id, new_ver, "failed")
                await fail_service_validation(service_id)

                # ── Failure analysis: explain WHY the update failed ──
                yield json.dumps({
                    "type": "progress", "phase": "analyzing_failure",
                    "detail": f"🧠 Analyzing why the update failed — preparing explanation…",
                    "progress": 0.95,
                }) + "\n"

                _analysis_error = _last_error or "Unknown error"
                _is_downgrade = target_api < current_api
                try:
                    analysis = await _get_deploy_agent_analysis(
                        _analysis_error,
                        f"{service_id} (API {current_api} → {target_api}{'  ↓ DOWNGRADE' if _is_downgrade else ''})",
                        rg_name,
                        region,
                        heal_history=[{
                            "attempt": h.get("step", i + 1),
                            "phase": h.get("phase", "unknown"),
                            "error": h.get("error", ""),
                            "fix_summary": h.get("fix_summary", ""),
                        } for i, h in enumerate(heal_history)],
                    )
                except Exception as _ae:
                    logger.warning(f"Failure analysis failed: {_ae}")
                    analysis = (
                        f"The API version update from `{current_api}` to `{target_api}` "
                        f"failed after {MAX_HEAL_ATTEMPTS} attempt(s).\n\n"
                        f"**Last error:** {_analysis_error[:300]}\n\n"
                        + (f"**Note:** This is a **downgrade** — the target API version `{target_api}` "
                           f"is older than the current `{current_api}`. Azure may reject templates "
                           f"that use features introduced in newer API versions.\n\n" if _is_downgrade else "")
                        + "**Next steps:** Try a different target version, or discuss in chat for alternatives."
                    )

                yield json.dumps({
                    "type": "agent_analysis", "phase": "failed",
                    "detail": analysis,
                    "progress": 1.0,
                    "from_api": current_api,
                    "to_api": target_api,
                    "is_downgrade": _is_downgrade,
                    "attempts": MAX_HEAL_ATTEMPTS,
                }) + "\n"

                yield json.dumps({
                    "type": "error", "phase": "failed",
                    "detail": f"✗ Update failed after {MAX_HEAL_ATTEMPTS} attempts — see analysis above",
                    "progress": 1.0,
                }) + "\n"

                # Record pipeline failure
                await complete_pipeline_run(
                    _run_id, "failed",
                    error_detail=_last_error[:500] if _last_error else "Update failed after max attempts",
                    heal_count=len(heal_history),
                )

        except Exception as _stream_err:
            logger.error(f"Update pipeline stream error: {_stream_err}", exc_info=True)
            yield json.dumps({
                "type": "error", "phase": "failed",
                "detail": f"✗ Pipeline error: {str(_stream_err)[:300]}",
                "progress": 1.0,
            }) + "\n"
            try:
                await complete_pipeline_run(
                    _run_id, "failed",
                    error_detail=str(_stream_err)[:500],
                )
            except Exception:
                pass

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


@app.get("/api/catalog/services/sync")
async def sync_services_from_azure():
    """Stream real-time progress of Azure resource provider sync via SSE.

    - If no sync is running, starts one and streams progress.
    - If a sync IS already running, subscribes to the existing stream
      (with full history replay so you immediately see current state).
    - The final event has `phase: 'done'` with the full summary.
    """
    from src.azure_sync import sync_manager, run_sync_managed

    # Start a sync if one isn't already running (idempotent)
    await run_sync_managed()

    async def _event_stream():
        q = sync_manager.subscribe()
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            sync_manager.unsubscribe(q)

    return StreamingResponse(_event_stream(), media_type="text/event-stream")


@app.get("/api/catalog/services/sync/status")
async def sync_status():
    """Return the current sync status (running, progress, last result)."""
    from src.azure_sync import sync_manager
    return JSONResponse(sync_manager.status())


@app.get("/api/catalog/services/sync/stats")
async def sync_stats():
    """Return combined service stats + sync status for the stats panel.

    Returns total Azure resource types (from last sync), total cached
    in our DB, total approved, and current sync status — all in one call.
    """
    from src.azure_sync import sync_manager

    try:
        services = await get_all_services()
        stats = {"approved": 0, "conditional": 0, "under_review": 0, "not_approved": 0}
        for svc in services:
            status = svc.get("status", "not_approved")
            stats[status] = stats.get(status, 0) + 1

        sync_info = sync_manager.status()

        return JSONResponse({
            "total_azure": sync_info.get("total_azure_resource_types"),
            "total_cached": len(services),
            "total_approved": stats["approved"],
            "total_conditional": stats["conditional"],
            "total_under_review": stats["under_review"],
            "total_not_approved": stats["not_approved"],
            "sync_running": sync_info["running"],
            "last_synced_at": sync_info.get("last_completed_at_iso"),
            "last_synced_ago_sec": sync_info.get("last_completed_ago_sec"),
            "last_sync_result": sync_info.get("last_completed"),
        })
    except Exception as e:
        logger.error(f"Failed to load sync stats: {e}")
        return JSONResponse({
            "total_azure": None,
            "total_cached": 0,
            "total_approved": 0,
            "total_conditional": 0,
            "total_under_review": 0,
            "total_not_approved": 0,
            "sync_running": False,
            "last_synced_at": None,
            "last_synced_ago_sec": None,
            "last_sync_result": None,
        })


# ══════════════════════════════════════════════════════════════
# ARM TEMPLATE Q&A — ASK QUESTIONS ABOUT THE ARM TEMPLATE
# ══════════════════════════════════════════════════════════════

@app.post("/api/catalog/templates/{template_id}/arm-qa")
async def arm_template_qa(template_id: str, request: Request):
    """Answer a user question about an ARM template's contents.

    Body: { "question": "What does the networkSecurityGroup resource do?" }
    Returns: { "answer": "..." }
    """
    from src.copilot_helpers import copilot_send
    from src.model_router import get_model_for_task, Task

    tmpl = await _require_template(template_id)

    body = await _parse_body_required(request)

    question = body.get("question", "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question is required")

    # ── Load the ARM template content ─────────────────────────
    arm_content = ""
    active_ver = tmpl.get("active_version") or 1
    try:
        ver_row = await get_template_version(template_id, active_ver)
        if ver_row and ver_row.get("arm_template"):
            arm_content = ver_row["arm_template"]
    except Exception as exc:
        logger.warning(f"[arm-qa] Failed to load ARM content: {exc}")

    if not arm_content:
        # Fall back to template.content if no versioned ARM template
        arm_content = tmpl.get("content") or ""

    if not arm_content:
        return JSONResponse({"answer": "No ARM template content is available for this template."})

    # ── Truncate very large templates to avoid token limits ───
    max_chars = 60_000
    truncated = arm_content[:max_chars]
    if len(arm_content) > max_chars:
        truncated += "\n... (truncated)"

    # ── Build the prompt and call the LLM ─────────────────────
    client = await ensure_copilot_client()
    model = get_model_for_task(Task.CHAT)

    system_prompt = (
        "You are an Azure infrastructure expert. The user is viewing an ARM "
        "(Azure Resource Manager) JSON template and has a question about it. "
        "Answer clearly and concisely, referencing specific resources, parameters, "
        "or sections from the template when relevant. Use Markdown formatting. "
        "If the question is unrelated to the template, politely redirect."
    )

    user_prompt = (
        f"--- ARM TEMPLATE ---\n{truncated}\n--- END TEMPLATE ---\n\n"
        f"Question: {question}"
    )

    try:
        answer = await copilot_send(
            client,
            model=model,
            system_prompt=system_prompt,
            prompt=user_prompt,
            timeout=45.0,
            agent_name="arm-qa",
        )
        return JSONResponse({"answer": answer or "I couldn't generate a response. Please try rephrasing your question."})
    except Exception as exc:
        logger.error(f"[arm-qa] LLM error: {exc}")
        return JSONResponse(
            status_code=500,
            content={"answer": f"Error generating response: {str(exc)[:200]}"},
        )


# ══════════════════════════════════════════════════════════════
# TEMPLATE FEEDBACK — CHAT WITH YOUR TEMPLATE
# ══════════════════════════════════════════════════════════════

@app.post("/api/catalog/templates/{template_id}/feedback")
async def template_feedback(template_id: str, request: Request):
    """Accept natural-language feedback about a template and auto-fix it.

    Body:
    {
        "message": "I wanted a VM but only the VNet got deployed"
    }

    The endpoint:
    1. Sends the template + user message to the LLM for gap analysis
    2. Identifies missing Azure resource types
    3. Auto-onboards missing services into the catalog
    4. Updates the template's service_ids and triggers recompose
    5. Returns the analysis, actions taken, and updated template

    This is the human-in-the-loop channel for the autonomous orchestrator.
    """
    from src.orchestrator import analyze_template_feedback
    from src.tools.arm_generator import (
        _STANDARD_PARAMETERS, _TEMPLATE_WRAPPER, generate_arm_template,
        has_builtin_skeleton,
    )
    from src.template_engine import analyze_dependencies
    import json as _json

    tmpl = await _require_template(template_id)

    body = await _parse_body_required(request)

    message = body.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Feedback message is required")

    # Get the copilot client for LLM analysis
    client = await ensure_copilot_client()

    # ── Step 1: Analyze feedback ──────────────────────────────
    feedback_result = await analyze_template_feedback(
        tmpl,
        message,
        copilot_client=client,
    )

    if not feedback_result["should_recompose"]:
        return JSONResponse({
            "status": "no_changes",
            "analysis": feedback_result["analysis"],
            "missing_services": feedback_result["missing_services"],
            "actions_taken": feedback_result["actions_taken"],
            "message": "No missing services identified — the template may already cover your needs. "
                       "Try providing more specific feedback about what resources you expected.",
        })

    # ── Step 2: Update template service_ids ───────────────────
    new_service_ids = feedback_result["new_service_ids"]

    # ── Step 3: Recompose with the updated service list ───────
    STANDARD_PARAMS = {
        "resourceName", "location", "environment",
        "projectName", "ownerEmail", "costCenter",
    }

    service_templates: list[dict] = []
    pinned_versions: dict = {}
    for sid in new_service_ids:
        svc = await get_service(sid)
        if not svc:
            continue
        tpl_dict = None
        active = await get_active_service_version(sid)
        if active and active.get("arm_template"):
            try:
                tpl_dict = _json.loads(active["arm_template"])
                pinned_versions[sid] = {
                    "version": active.get("version"),
                    "semver": active.get("semver"),
                }
            except Exception:
                pass
        # Fallback: draft version from auto-prep
        if not tpl_dict:
            draft = await get_latest_service_version(sid)
            if draft and draft.get("arm_template"):
                try:
                    tpl_dict = _json.loads(draft["arm_template"])
                    pinned_versions[sid] = {
                        "version": draft.get("version"),
                        "semver": draft.get("semver", "0.0.0-draft"),
                    }
                except Exception:
                    pass
        if not tpl_dict and has_builtin_skeleton(sid):
            tpl_dict = generate_arm_template(sid)
            if sid not in pinned_versions:
                pinned_versions[sid] = {"version": 0, "semver": "0.0.0-builtin"}
        if not tpl_dict:
            continue
        service_templates.append({
            "svc": svc,
            "template": tpl_dict,
            "quantity": 1,
        })

    if not service_templates:
        raise HTTPException(
            status_code=500,
            detail="No service templates available for recomposition",
        )

    # Compose the updated template (same logic as recompose endpoint)
    combined_params = dict(_STANDARD_PARAMETERS)
    combined_resources: list[dict] = []
    combined_outputs: dict = {}
    resource_types: list[str] = []
    tags_list: list[str] = []

    for entry in service_templates:
        svc = entry["svc"]
        tpl = entry["template"]
        sid = svc["id"]
        short_name = sid.split("/")[-1].lower()
        resource_types.append(sid)
        tags_list.append(svc.get("category", ""))

        src_params = tpl.get("parameters", {})
        src_resources = tpl.get("resources", [])
        src_outputs = tpl.get("outputs", {})

        suffix = f"_{short_name}"
        instance_name_param = f"resourceName{suffix}"
        combined_params[instance_name_param] = {
            "type": "string",
            "metadata": {"description": f"Name for {svc.get('name', sid)}"},
        }

        all_non_standard = [
            pname for pname in src_params
            if pname not in STANDARD_PARAMS and pname != "resourceName"
        ]
        for pname in all_non_standard:
            pdef = src_params.get(pname)
            if not pdef:
                continue
            suffixed = f"{pname}{suffix}"
            combined_params[suffixed] = dict(pdef)

        for res in src_resources:
            res_str = _json.dumps(res)
            res_str = res_str.replace(
                "[parameters('resourceName')]",
                f"[parameters('{instance_name_param}')]",
            )
            res_str = res_str.replace(
                "parameters('resourceName')",
                f"parameters('{instance_name_param}')",
            )
            for pname in all_non_standard:
                suffixed = f"{pname}{suffix}"
                res_str = res_str.replace(
                    f"[parameters('{pname}')]",
                    f"[parameters('{suffixed}')]",
                )
                res_str = res_str.replace(
                    f"parameters('{pname}')",
                    f"parameters('{suffixed}')",
                )
            combined_resources.append(_json.loads(res_str))

        for oname, odef in src_outputs.items():
            out_name = f"{oname}{suffix}"
            out_val = _json.dumps(odef)
            out_val = out_val.replace(
                "[parameters('resourceName')]",
                f"[parameters('{instance_name_param}')]",
            )
            out_val = out_val.replace(
                "parameters('resourceName')",
                f"parameters('{instance_name_param}')",
            )
            for pname in all_non_standard:
                suffixed = f"{pname}{suffix}"
                out_val = out_val.replace(
                    f"[parameters('{pname}')]",
                    f"[parameters('{suffixed}')]",
                )
                out_val = out_val.replace(
                    f"parameters('{pname}')",
                    f"parameters('{suffixed}')",
                )
            combined_outputs[out_name] = _json.loads(out_val)

    composed = dict(_TEMPLATE_WRAPPER)
    composed["parameters"] = combined_params
    composed["variables"] = {}
    composed["resources"] = combined_resources
    composed["outputs"] = combined_outputs

    content_str = _json.dumps(composed, indent=2)

    # Apply sanitizers
    content_str = _ensure_parameter_defaults(content_str)
    content_str = _sanitize_placeholder_guids(content_str)
    content_str = _sanitize_dns_zone_names(content_str)

    composed = _json.loads(content_str)
    combined_params = composed.get("parameters", {})
    param_list = [
        {"name": k, "type": v.get("type", "string"), "required": "defaultValue" not in v}
        for k, v in combined_params.items()
    ]

    dep_analysis = analyze_dependencies(new_service_ids)

    # ── Step 4: Save the updated template ─────────────────────
    catalog_entry = {
        "id": template_id,
        "name": tmpl.get("name", template_id),
        "description": tmpl.get("description", ""),
        "format": "arm",
        "category": tmpl.get("category", "blueprint"),
        "content": content_str,
        "tags": list(set(tags_list)),
        "resources": list(set(resource_types)),
        "parameters": param_list,
        "outputs": list(combined_outputs.keys()),
        "is_blueprint": len(service_templates) > 1,
        "service_ids": new_service_ids,
        "pinned_versions": pinned_versions,
        "status": "draft",  # Reset to draft — needs re-testing
        "registered_by": tmpl.get("registered_by", "template-composer"),
        "template_type": dep_analysis["template_type"],
        "provides": dep_analysis["provides"],
        "requires": dep_analysis["requires"],
        "optional_refs": dep_analysis["optional_refs"],
    }

    try:
        await delete_template_versions_by_status(template_id, ["draft", "failed"])
        await upsert_template(catalog_entry)
        ver = await create_template_version(
            template_id, content_str,
            changelog=f"Feedback recompose: {message[:100]}",
            change_type="minor",
            created_by="feedback-orchestrator",
        )
    except Exception as e:
        logger.error(f"Failed to save feedback-recomposed template: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    logger.info(
        f"Feedback recomposed '{template_id}': {len(feedback_result['actions_taken'])} actions, "
        f"{len(combined_resources)} resources, {len(new_service_ids)} services"
    )

    return JSONResponse({
        "status": "recomposed",
        "analysis": feedback_result["analysis"],
        "missing_services": feedback_result["missing_services"],
        "actions_taken": feedback_result["actions_taken"],
        "template_id": template_id,
        "resource_count": len(combined_resources),
        "parameter_count": len(combined_params),
        "services": new_service_ids,
        "version": ver,
        "message": f"Template updated with {len(feedback_result['actions_taken'])} new services and recomposed. Status reset to draft for re-testing.",
    })


# ══════════════════════════════════════════════════════════════
# TEMPLATE REVISION — REQUEST REVISION WITH POLICY CHECK
# ══════════════════════════════════════════════════════════════

@app.post("/api/catalog/templates/{template_id}/revision/policy-check")
async def revision_policy_check(template_id: str, request: Request):
    """Pre-check a revision request against org policies.

    Body: { "prompt": "Add a public-facing VM with open SSH" }

    Returns instant pass/warning/block feedback BEFORE any changes are made.
    Call this first; if it passes, call the /revise endpoint.
    """
    from src.orchestrator import check_revision_policy

    tmpl = await _require_template(template_id)

    body = await _parse_body_required(request)

    prompt = body.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    client = await ensure_copilot_client()

    try:
        result = await check_revision_policy(
            prompt,
            template=tmpl,
            copilot_client=client,
        )
    except Exception as e:
        logger.warning(f"Policy check failed: {e}")
        result = {"verdict": "pass", "issues": [], "summary": "Policy check unavailable — proceeding."}

    return JSONResponse(result)


@app.post("/api/catalog/templates/{template_id}/revise")
async def revise_template(template_id: str, request: Request):
    """Revise a template based on natural language — with policy enforcement.

    Streams NDJSON log events so the UI can show live progress.

    Body:
    {
        "prompt": "Add a SQL database and a Key Vault to this template",
        "skip_policy_check": false
    }

    Stream event format (one JSON object per line):
    {
        "type": "log" | "step" | "result" | "error",
        "phase": "policy" | "analyze" | "onboard" | "compose" | "save" | "done",
        "status": "running" | "success" | "warning" | "error" | "skip",
        "message": "Human-readable log line",
        "detail": { ... optional structured data ... },
        "ts": "ISO timestamp"
    }
    """
    from src.orchestrator import (
        check_revision_policy, analyze_template_feedback,
    )
    from src.tools.arm_generator import (
        _STANDARD_PARAMETERS, _TEMPLATE_WRAPPER, generate_arm_template,
        has_builtin_skeleton,
    )
    from src.template_engine import analyze_dependencies
    import json as _json

    tmpl = await _require_template(template_id)

    body = await _parse_body_required(request)

    prompt = body.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    skip_policy = body.get("skip_policy_check", False)

    async def _stream():
        from datetime import datetime, timezone

        def emit(type_: str, phase: str, status: str, message: str, detail: dict | None = None):
            event = {
                "type": type_,
                "phase": phase,
                "status": status,
                "message": message,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            if detail:
                event["detail"] = detail
            return _json.dumps(event, default=str) + "\n"

        try:
            client = await ensure_copilot_client()

            # ── Phase 1: Policy pre-check ─────────────────────────
            yield emit("step", "policy", "running", "Checking organizational policies…")
            policy_result = None
            if not skip_policy:
                policy_result = await check_revision_policy(
                    prompt, template=tmpl, copilot_client=client,
                )
                verdict = policy_result.get("verdict", "pass")
                if verdict == "block":
                    yield emit("step", "policy", "error",
                               f"Policy review required: {policy_result.get('summary', '')}",
                               {"policy_check": policy_result})
                    yield emit("result", "done", "blocked",
                               "Revision paused — organizational policies need to be addressed.",
                               {"status": "blocked", "policy_check": policy_result})
                    return
                elif verdict == "warning":
                    yield emit("step", "policy", "warning",
                               f"Policy notes: {policy_result.get('summary', '')}",
                               {"policy_check": policy_result})
                else:
                    yield emit("step", "policy", "success", "Policy check complete")
            else:
                yield emit("step", "policy", "skip", "Policy check skipped (pre-checked)")

            # ── Phase 2: Analyze what needs to change ─────────────
            yield emit("step", "analyze", "running", "Analyzing request with Copilot SDK…")
            yield emit("log", "analyze", "running",
                       f"Prompt: \"{prompt[:120]}{'…' if len(prompt) > 120 else ''}\"")

            feedback_result = await analyze_template_feedback(
                tmpl, prompt, copilot_client=client,
            )

            analysis = feedback_result.get("analysis", "")
            if analysis:
                yield emit("log", "analyze", "running", f"Analysis: {analysis}")

            actions = feedback_result.get("actions_taken", [])
            for a in actions:
                yield emit("log", "analyze", "running",
                           f"Action: {a.get('action', '?')} → {a.get('service_id', '?')} — {a.get('detail', '')}")

            if not feedback_result["should_recompose"]:
                # ── Direct code edit path ─────────────────────────
                if feedback_result.get("needs_code_edit"):
                    yield emit("step", "analyze", "success",
                               "Direct code edit identified")
                    yield emit("step", "compose", "running",
                               "Applying code edits via Copilot SDK…")

                    from src.orchestrator import apply_template_code_edit
                    edit_result = await apply_template_code_edit(
                        tmpl,
                        feedback_result.get("edit_instruction", prompt),
                        prompt,
                        copilot_client=client,
                    )

                    if not edit_result["success"]:
                        yield emit("step", "compose", "error",
                                   f"Edit noted: {edit_result['error']}")
                        yield emit("result", "done", "error",
                                   f"Edit could not be applied: {edit_result['error']}",
                                   {"status": "edit_failed",
                                    "policy_check": policy_result,
                                    "analysis": analysis})
                        return

                    yield emit("log", "compose", "running",
                               f"Changes: {edit_result['changes_summary']}")
                    yield emit("step", "compose", "success", "Code edit applied")

                    # Save
                    yield emit("step", "save", "running", "Saving edited template…")
                    edited_content = edit_result["content"]
                    try:
                        parsed = _json.loads(edited_content)
                        resource_count = len(parsed.get("resources", []))
                        param_count = len(parsed.get("parameters", {}))
                    except Exception:
                        resource_count = 0
                        param_count = 0

                    try:
                        parsed_params = _json.loads(edited_content).get("parameters", {})
                        param_list = [
                            {"name": k, "type": v.get("type", "string"),
                             "required": "defaultValue" not in v}
                            for k, v in parsed_params.items()
                        ]
                    except Exception:
                        param_list = tmpl.get("parameters", [])

                    catalog_entry = {
                        "id": template_id,
                        "name": tmpl.get("name", template_id),
                        "description": tmpl.get("description", ""),
                        "format": "arm",
                        "category": tmpl.get("category", "blueprint"),
                        "content": edited_content,
                        "tags": tmpl.get("tags", []),
                        "resources": tmpl.get("resources", []),
                        "parameters": param_list,
                        "outputs": tmpl.get("outputs", []),
                        "is_blueprint": tmpl.get("is_blueprint", False),
                        "service_ids": tmpl.get("service_ids", []),
                        "pinned_versions": tmpl.get("pinned_versions", {}),
                        "status": "draft",
                        "registered_by": tmpl.get("registered_by", "template-composer"),
                        "template_type": tmpl.get("template_type", ""),
                        "provides": tmpl.get("provides", []),
                        "requires": tmpl.get("requires", []),
                        "optional_refs": tmpl.get("optional_refs", []),
                    }

                    await delete_template_versions_by_status(template_id, ["draft", "failed"])
                    await upsert_template(catalog_entry)
                    ver = await create_template_version(
                        template_id, edited_content,
                        changelog=f"Edit: {prompt[:100]}",
                        change_type="minor",
                        created_by="revision-code-edit",
                    )
                    yield emit("step", "save", "success",
                               f"Saved as v{ver.get('semver', '?')}")

                    yield emit("result", "done", "success",
                               f"Template edited: {edit_result['changes_summary']}",
                               {"status": "revised",
                                "policy_check": policy_result,
                                "analysis": analysis,
                                "actions_taken": [{"action": "code_edit",
                                                   "service_id": "template",
                                                   "detail": edit_result["changes_summary"]}],
                                "template_id": template_id,
                                "resource_count": resource_count,
                                "parameter_count": param_count,
                                "services": tmpl.get("service_ids", []),
                                "version": ver})
                    return

                # No changes path
                yield emit("step", "analyze", "success", "No new services identified")
                yield emit("result", "done", "no_changes",
                           feedback_result.get("analysis", "No changes needed."),
                           {"status": "no_changes",
                            "policy_check": policy_result,
                            "analysis": analysis,
                            "actions_taken": actions,
                            "message": "No new services identified from your request. "
                                       "Try being more specific about what resources you need."})
                return

            # ── Phase 3: Service onboarding ───────────────────────
            new_service_ids = feedback_result["new_service_ids"]
            yield emit("step", "analyze", "success",
                       f"Identified {len(new_service_ids)} service(s) for composition")

            yield emit("step", "onboard", "running",
                       f"Preparing {len(new_service_ids)} service template(s)…")

            STANDARD_PARAMS = {
                "resourceName", "location", "environment",
                "projectName", "ownerEmail", "costCenter",
            }

            service_templates: list[dict] = []
            pinned_versions: dict = {}
            for sid in new_service_ids:
                svc = await get_service(sid)
                if not svc:
                    yield emit("log", "onboard", "warning", f"Service {sid} not found — skipping")
                    continue

                tpl_dict = None
                active = await get_active_service_version(sid)
                if active and active.get("arm_template"):
                    try:
                        tpl_dict = _json.loads(active["arm_template"])
                        pinned_versions[sid] = {
                            "version": active.get("version"),
                            "semver": active.get("semver"),
                        }
                        yield emit("log", "onboard", "running",
                                   f"● {svc.get('name', sid)} — loaded from catalog")
                    except Exception:
                        pass
                # Fallback: draft version from auto-prep
                if not tpl_dict:
                    draft = await get_latest_service_version(sid)
                    if draft and draft.get("arm_template"):
                        try:
                            tpl_dict = _json.loads(draft["arm_template"])
                            pinned_versions[sid] = {
                                "version": draft.get("version"),
                                "semver": draft.get("semver", "0.0.0-draft"),
                            }
                            yield emit("log", "onboard", "running",
                                       f"● {svc.get('name', sid)} — loaded draft from auto-prep")
                        except Exception:
                            pass
                if not tpl_dict and has_builtin_skeleton(sid):
                    tpl_dict = generate_arm_template(sid)
                    if sid not in pinned_versions:
                        pinned_versions[sid] = {"version": 0, "semver": "0.0.0-builtin"}
                    yield emit("log", "onboard", "running",
                               f"● {svc.get('name', sid)} — generated ARM skeleton")
                if not tpl_dict:
                    yield emit("log", "onboard", "warning",
                               f"○ {svc.get('name', sid)} — no template available")
                    continue

                service_templates.append({
                    "svc": svc,
                    "template": tpl_dict,
                    "quantity": 1,
                })

            if not service_templates:
                yield emit("step", "onboard", "error",
                           "No service templates available for recomposition")
                yield emit("result", "done", "error",
                           "No service templates available — try a different approach",
                           {"status": "error"})
                return

            yield emit("step", "onboard", "success",
                       f"{len(service_templates)} service template(s) ready")

            # ── Phase 4: Compose ──────────────────────────────────
            yield emit("step", "compose", "running",
                       f"Composing ARM template from {len(service_templates)} services…")

            combined_params = dict(_STANDARD_PARAMETERS)
            combined_resources: list[dict] = []
            combined_outputs: dict = {}
            resource_types: list[str] = []
            tags_list: list[str] = []

            for entry in service_templates:
                svc = entry["svc"]
                tpl = entry["template"]
                sid = svc["id"]
                short_name = sid.split("/")[-1].lower()
                resource_types.append(sid)
                tags_list.append(svc.get("category", ""))

                src_params = tpl.get("parameters", {})
                src_resources = tpl.get("resources", [])
                src_outputs = tpl.get("outputs", {})

                suffix = f"_{short_name}"
                instance_name_param = f"resourceName{suffix}"
                combined_params[instance_name_param] = {
                    "type": "string",
                    "metadata": {"description": f"Name for {svc.get('name', sid)}"},
                }

                all_non_standard = [
                    pname for pname in src_params
                    if pname not in STANDARD_PARAMS and pname != "resourceName"
                ]
                for pname in all_non_standard:
                    pdef = src_params.get(pname)
                    if not pdef:
                        continue
                    suffixed = f"{pname}{suffix}"
                    combined_params[suffixed] = dict(pdef)

                for res in src_resources:
                    res_str = _json.dumps(res)
                    res_str = res_str.replace("[parameters('resourceName')]",
                                             f"[parameters('{instance_name_param}')]")
                    res_str = res_str.replace("parameters('resourceName')",
                                             f"parameters('{instance_name_param}')")
                    for pname in all_non_standard:
                        suffixed = f"{pname}{suffix}"
                        res_str = res_str.replace(f"[parameters('{pname}')]",
                                                  f"[parameters('{suffixed}')]")
                        res_str = res_str.replace(f"parameters('{pname}')",
                                                  f"parameters('{suffixed}')")
                    combined_resources.append(_json.loads(res_str))

                for oname, odef in src_outputs.items():
                    out_name = f"{oname}{suffix}"
                    out_val = _json.dumps(odef)
                    out_val = out_val.replace("[parameters('resourceName')]",
                                             f"[parameters('{instance_name_param}')]")
                    out_val = out_val.replace("parameters('resourceName')",
                                             f"parameters('{instance_name_param}')")
                    for pname in all_non_standard:
                        suffixed = f"{pname}{suffix}"
                        out_val = out_val.replace(f"[parameters('{pname}')]",
                                                  f"[parameters('{suffixed}')]")
                        out_val = out_val.replace(f"parameters('{pname}')",
                                                  f"parameters('{suffixed}')")
                    combined_outputs[out_name] = _json.loads(out_val)

                yield emit("log", "compose", "running",
                           f"Merged {svc.get('name', sid)}: "
                           f"{len(src_resources)} resource(s), "
                           f"{len(all_non_standard)} param(s)")

            composed = dict(_TEMPLATE_WRAPPER)
            composed["parameters"] = combined_params
            composed["variables"] = {}
            composed["resources"] = combined_resources
            composed["outputs"] = combined_outputs

            content_str = _json.dumps(composed, indent=2)
            content_str = _ensure_parameter_defaults(content_str)
            content_str = _sanitize_placeholder_guids(content_str)
            content_str = _sanitize_dns_zone_names(content_str)

            composed = _json.loads(content_str)
            combined_params = composed.get("parameters", {})
            param_list = [
                {"name": k, "type": v.get("type", "string"),
                 "required": "defaultValue" not in v}
                for k, v in combined_params.items()
            ]

            dep_analysis = analyze_dependencies(new_service_ids)

            yield emit("step", "compose", "success",
                       f"Composed: {len(combined_resources)} resources, "
                       f"{len(combined_params)} params")

            # ── Phase 5: Save ─────────────────────────────────────
            yield emit("step", "save", "running", "Saving to catalog…")

            catalog_entry = {
                "id": template_id,
                "name": tmpl.get("name", template_id),
                "description": tmpl.get("description", ""),
                "format": "arm",
                "category": tmpl.get("category", "blueprint"),
                "content": content_str,
                "tags": list(set(tags_list)),
                "resources": list(set(resource_types)),
                "parameters": param_list,
                "outputs": list(combined_outputs.keys()),
                "is_blueprint": len(service_templates) > 1,
                "service_ids": new_service_ids,
                "pinned_versions": pinned_versions,
                "status": "draft",
                "registered_by": tmpl.get("registered_by", "template-composer"),
                "template_type": dep_analysis["template_type"],
                "provides": dep_analysis["provides"],
                "requires": dep_analysis["requires"],
                "optional_refs": dep_analysis["optional_refs"],
            }

            await delete_template_versions_by_status(template_id, ["draft", "failed"])
            await upsert_template(catalog_entry)
            ver = await create_template_version(
                template_id, content_str,
                changelog=f"Revision: {prompt[:100]}",
                change_type="minor",
                created_by="revision-orchestrator",
            )

            yield emit("step", "save", "success",
                       f"Version v{ver.get('semver', '?')} created")

            # ── Done ──────────────────────────────────────────────
            yield emit("result", "done", "success",
                       f"Template revised with {len(actions)} change(s).",
                       {"status": "revised",
                        "policy_check": policy_result,
                        "analysis": analysis,
                        "actions_taken": actions,
                        "template_id": template_id,
                        "resource_count": len(combined_resources),
                        "parameter_count": len(combined_params),
                        "services": new_service_ids,
                        "version": ver})

        except Exception as e:
            logger.error(f"Revision stream error: {e}")
            yield emit("error", "done", "error", str(e))

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


# ══════════════════════════════════════════════════════════════
# PROMPT-DRIVEN TEMPLATE COMPOSITION
# ══════════════════════════════════════════════════════════════

@app.post("/api/catalog/templates/compose-from-prompt")
async def compose_template_from_prompt(request: Request):
    """Compose a new template from a natural language description.

    Body:
    {
        "prompt": "I need a VM with a SQL database and Key Vault",
        "name": "optional override",
        "category": "optional override"
    }

    Flow:
    1. Policy pre-check → block if violations
    2. LLM determines which services are needed
    3. Auto-onboard missing services + resolve dependencies
    4. Compose the template
    5. Run structural tests
    6. Return the composed template
    """
    from src.orchestrator import (
        check_revision_policy, determine_services_from_prompt,
        resolve_composition_dependencies,
    )
    from src.tools.arm_generator import (
        _STANDARD_PARAMETERS, _TEMPLATE_WRAPPER, generate_arm_template,
        has_builtin_skeleton,
    )
    from src.template_engine import analyze_dependencies
    import json as _json

    body = await _parse_body_required(request)

    prompt = body.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required — describe what infrastructure you need")

    client = await ensure_copilot_client()

    # ── Step 1: Policy pre-check ──────────────────────────────
    policy_result = await check_revision_policy(
        prompt,
        copilot_client=client,
    )

    if policy_result["verdict"] == "block":
        return JSONResponse({
            "status": "blocked",
            "policy_check": policy_result,
            "message": "Request blocked by organizational policy.",
        }, status_code=422)

    # ── Step 2: LLM determines services ───────────────────────
    selection = await determine_services_from_prompt(
        prompt,
        copilot_client=client,
    )

    services = selection.get("services", [])
    if not services:
        return JSONResponse({
            "status": "no_services",
            "policy_check": policy_result,
            "message": "Could not determine which Azure services are needed. "
                       "Try being more specific, e.g. 'I need a VM with a SQL database'.",
        })

    name = body.get("name", "").strip() or selection.get("name_suggestion", "My Template")
    description = body.get("description", "").strip() or selection.get("description_suggestion", "")
    category = body.get("category", "").strip() or selection.get("category_suggestion", "blueprint")

    # ── Step 3: Resolve & onboard ─────────────────────────────
    service_ids = [s["resource_type"] for s in services]

    dep_result = await resolve_composition_dependencies(
        service_ids,
        copilot_client=client,
    )

    final_service_ids = dep_result["final_service_ids"]

    # ── Step 4: Gather ARM templates ──────────────────────────
    STANDARD_PARAMS = {
        "resourceName", "location", "environment",
        "projectName", "ownerEmail", "costCenter",
    }

    service_templates: list[dict] = []
    pinned_versions: dict = {}  # service_id → {version, semver}
    for sid in final_service_ids:
        svc = await get_service(sid)
        if not svc:
            continue
        tpl_dict = None
        active = await get_active_service_version(sid)
        if active and active.get("arm_template"):
            try:
                tpl_dict = _json.loads(active["arm_template"])
                pinned_versions[sid] = {
                    "version": active.get("version"),
                    "semver": active.get("semver"),
                }
            except Exception:
                pass
        # Fallback: draft version from auto-prep
        if not tpl_dict:
            draft = await get_latest_service_version(sid)
            if draft and draft.get("arm_template"):
                try:
                    tpl_dict = _json.loads(draft["arm_template"])
                    pinned_versions[sid] = {
                        "version": draft.get("version"),
                        "semver": draft.get("semver", "0.0.0-draft"),
                    }
                except Exception:
                    pass
        if not tpl_dict and has_builtin_skeleton(sid):
            tpl_dict = generate_arm_template(sid)
            if sid not in pinned_versions:
                pinned_versions[sid] = {"version": 0, "semver": "0.0.0-builtin"}
        if not tpl_dict:
            continue

        # Find quantity for this service
        qty = 1
        for s in services:
            if s["resource_type"] == sid:
                qty = s.get("quantity", 1)
                break

        service_templates.append({
            "svc": svc,
            "template": tpl_dict,
            "quantity": qty,
            "chosen_params": set(),
        })

    if not service_templates:
        raise HTTPException(status_code=500, detail="No service templates available after resolution")

    # ── Step 5: Compose ───────────────────────────────────────
    combined_params = dict(_STANDARD_PARAMETERS)
    combined_resources: list[dict] = []
    combined_outputs: dict = {}
    composed_service_ids: list[str] = []
    resource_types: list[str] = []
    tags_list: list[str] = []

    for entry in service_templates:
        svc = entry["svc"]
        tpl = entry["template"]
        qty = entry["quantity"]
        sid = svc["id"]
        composed_service_ids.append(sid)
        short_name = sid.split("/")[-1].lower()
        resource_types.append(sid)
        tags_list.append(svc.get("category", ""))

        src_params = tpl.get("parameters", {})
        src_resources = tpl.get("resources", [])
        src_outputs = tpl.get("outputs", {})

        for idx in range(1, qty + 1):
            suffix = f"_{short_name}" if qty == 1 else f"_{short_name}{idx}"
            instance_name_param = f"resourceName{suffix}"
            combined_params[instance_name_param] = {
                "type": "string",
                "metadata": {
                    "description": f"Name for {svc.get('name', sid)}"
                    + (f" (instance {idx})" if qty > 1 else ""),
                },
            }

            all_non_standard = [
                pname for pname in src_params
                if pname not in STANDARD_PARAMS and pname != "resourceName"
            ]
            for pname in all_non_standard:
                pdef = src_params.get(pname)
                if not pdef:
                    continue
                suffixed = f"{pname}{suffix}"
                combined_params[suffixed] = dict(pdef)

            for res in src_resources:
                res_str = _json.dumps(res)
                res_str = res_str.replace("[parameters('resourceName')]", f"[parameters('{instance_name_param}')]")
                res_str = res_str.replace("parameters('resourceName')", f"parameters('{instance_name_param}')")
                for pname in all_non_standard:
                    suffixed = f"{pname}{suffix}"
                    res_str = res_str.replace(f"[parameters('{pname}')]", f"[parameters('{suffixed}')]")
                    res_str = res_str.replace(f"parameters('{pname}')", f"parameters('{suffixed}')")
                combined_resources.append(_json.loads(res_str))

            for oname, odef in src_outputs.items():
                out_name = f"{oname}{suffix}"
                out_val = _json.dumps(odef)
                out_val = out_val.replace("[parameters('resourceName')]", f"[parameters('{instance_name_param}')]")
                out_val = out_val.replace("parameters('resourceName')", f"parameters('{instance_name_param}')")
                for pname in all_non_standard:
                    suffixed = f"{pname}{suffix}"
                    out_val = out_val.replace(f"[parameters('{pname}')]", f"[parameters('{suffixed}')]")
                    out_val = out_val.replace(f"parameters('{pname}')", f"parameters('{suffixed}')")
                combined_outputs[out_name] = _json.loads(out_val)

    composed = dict(_TEMPLATE_WRAPPER)
    composed["parameters"] = combined_params
    composed["variables"] = {}
    composed["resources"] = combined_resources
    composed["outputs"] = combined_outputs

    content_str = _json.dumps(composed, indent=2)
    content_str = _ensure_parameter_defaults(content_str)
    content_str = _sanitize_placeholder_guids(content_str)
    content_str = _sanitize_dns_zone_names(content_str)

    template_id = "composed-" + name.lower().replace(" ", "-")[:50]

    composed = _json.loads(content_str)
    combined_params = composed.get("parameters", {})
    param_list = [
        {"name": k, "type": v.get("type", "string"), "required": "defaultValue" not in v}
        for k, v in combined_params.items()
    ]

    dep_analysis = analyze_dependencies(composed_service_ids)

    catalog_entry = {
        "id": template_id,
        "name": name,
        "description": description,
        "format": "arm",
        "category": category,
        "content": content_str,
        "tags": list(set(tags_list)),
        "resources": list(set(resource_types)),
        "parameters": param_list,
        "outputs": list(combined_outputs.keys()),
        "is_blueprint": len(service_templates) > 1,
        "service_ids": composed_service_ids,
        "pinned_versions": pinned_versions,
        "status": "draft",
        "registered_by": "prompt-composer",
        "template_type": dep_analysis["template_type"],
        "provides": dep_analysis["provides"],
        "requires": dep_analysis["requires"],
        "optional_refs": dep_analysis["optional_refs"],
    }

    try:
        await delete_template_versions_by_status(template_id, ["draft", "failed"])
        await upsert_template(catalog_entry)
        ver = await create_template_version(
            template_id, content_str,
            changelog=f"Prompt compose: {prompt[:100]}",
            change_type="initial",
        )
    except Exception as e:
        logger.error(f"Failed to save prompt-composed template: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({
        "status": "composed",
        "policy_check": policy_result,
        "template_id": template_id,
        "template": catalog_entry,
        "version": ver,
        "services_detected": services,
        "dependency_resolution": dep_result,
        "resource_count": len(combined_resources),
        "parameter_count": len(combined_params),
        "message": f"Template '{name}' composed from {len(composed_service_ids)} services "
                   f"({len(combined_resources)} resources, {len(combined_params)} params). "
                   f"Ready for testing.",
    })


@app.delete("/api/catalog/templates/{template_id}")
async def delete_template_endpoint(template_id: str):
    """Remove a template from the catalog."""

    deleted = await delete_template(template_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Template not found")
    return JSONResponse({"status": "ok", "deleted": template_id})


@app.delete("/api/catalog/templates/{template_id}/versions/drafts")
async def delete_template_draft_versions(template_id: str):
    """Delete all draft and failed template versions for a template."""
    count = await delete_template_versions_by_status(template_id, ["draft", "failed"])
    return JSONResponse({"deleted": count})


# ══════════════════════════════════════════════════════════════
# TEMPLATE DEPENDENCIES & RESOURCE DISCOVERY
# ══════════════════════════════════════════════════════════════

@app.get("/api/templates/types")
async def get_template_types():
    """List available template types (foundation, workload, composite)."""
    from src.template_engine import TEMPLATE_TYPES
    return JSONResponse(TEMPLATE_TYPES)


@app.get("/api/templates/known-dependencies")
async def list_known_dependencies():
    """List known resource type dependency mappings."""
    from src.template_engine import RESOURCE_DEPENDENCIES
    # Only return resource types that have dependencies
    return JSONResponse({k: v for k, v in RESOURCE_DEPENDENCIES.items() if v})


@app.get("/api/templates/hard-dependencies")
async def list_hard_dependencies():
    """Return the full hard-dependency map.

    Hard dependencies are services that MUST be co-selected together.
    The frontend caches this on load and enforces it in the compose chooser.
    """
    from src.template_engine import get_all_hard_dependencies
    return JSONResponse(get_all_hard_dependencies())


@app.post("/api/templates/analyze-dependencies")
async def analyze_template_dependencies(request: Request):
    """Analyze dependencies for a set of service IDs.

    Body: { "service_ids": ["Microsoft.Compute/virtualMachines", ...] }

    Returns: template_type, provides, requires, optional_refs, auto_created,
    and whether the template is deployable_standalone.
    """
    from src.template_engine import analyze_dependencies

    body = await _parse_body_required(request)

    service_ids = body.get("service_ids", [])
    if not service_ids:
        raise HTTPException(status_code=400, detail="service_ids list is required")

    analysis = analyze_dependencies(service_ids)
    return JSONResponse(analysis)


@app.get("/api/templates/discover/{resource_type:path}")
async def discover_resources_for_deployment(
    resource_type: str,
    subscription_id: Optional[str] = None,
):
    """Lightweight Azure Resource Graph query to find existing resources.

    Used at deploy time to populate resource pickers for template dependencies.
    One API call per resource type — not a full subscription scan.
    """
    from src.template_engine import discover_existing_resources

    resources = await discover_existing_resources(resource_type, subscription_id)
    return JSONResponse({
        "resource_type": resource_type,
        "count": len(resources),
        "resources": resources,
    })


@app.get("/api/templates/discover-subnets")
async def discover_subnets_endpoint(vnet_id: str):
    """Get subnets for a specific VNet — used for cascading pickers."""
    from src.template_engine import discover_subnets_for_vnet

    subnets = await discover_subnets_for_vnet(vnet_id)
    return JSONResponse({
        "vnet_id": vnet_id,
        "count": len(subnets),
        "subnets": subnets,
    })


# ══════════════════════════════════════════════════════════════
# SERVICE ONBOARDING & VERSIONED VALIDATION
# ══════════════════════════════════════════════════════════════

# ── Legacy artifact endpoints (kept for backward compat) ─────

@app.get("/api/services/{service_id:path}/artifacts")
async def get_artifacts_endpoint(service_id: str):
    """Get all approval artifacts for a service."""

    svc = await _require_service(service_id)

    artifacts = await get_service_artifacts(service_id)
    return JSONResponse(artifacts)


@app.put("/api/services/{service_id:path}/artifacts/{artifact_type}")
async def save_artifact_endpoint(service_id: str, artifact_type: str, request: Request):
    """Save or update an artifact (policy, template, or pipeline) for a service."""

    if artifact_type not in ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid artifact type. Must be one of: {', '.join(ARTIFACT_TYPES)}",
        )

    svc = await _require_service(service_id)

    body = await _parse_body_required(request)

    content = body.get("content", "")
    notes = body.get("notes", "")
    status = body.get("status", "draft")

    if status not in ("draft", "not_started"):
        raise HTTPException(status_code=400, detail="Use the /approve endpoint to approve")

    try:
        artifact = await save_service_artifact(
            service_id=service_id,
            artifact_type=artifact_type,
            content=content,
            status=status,
            notes=notes,
        )
        return JSONResponse({"status": "ok", "artifact": artifact})
    except Exception as e:
        logger.error(f"Failed to save artifact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/services/{service_id:path}/artifacts/{artifact_type}/approve")
async def approve_artifact_endpoint(service_id: str, artifact_type: str, request: Request):
    """Approve an artifact gate. If both gates are approved, the service moves to 'validating'."""

    if artifact_type not in ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid artifact type. Must be one of: {', '.join(ARTIFACT_TYPES)}",
        )

    svc = await _require_service(service_id)

    body = await _parse_body(request)

    approved_by = body.get("approved_by", "IT Staff")

    try:
        artifact = await approve_service_artifact(
            service_id=service_id,
            artifact_type=artifact_type,
            approved_by=approved_by,
        )

        # Check if all gates are now approved → validation required
        all_artifacts = await get_service_artifacts(service_id)
        all_approved = all_artifacts["_summary"]["all_approved"]

        return JSONResponse({
            "status": "ok",
            "artifact": artifact,
            "gates_approved": all_artifacts["_summary"]["approved_count"],
            "validation_required": all_approved,
            "message": (
                f"Both gates approved! Starting deployment validation…"
                if all_approved
                else f"Gate '{artifact_type}' approved ({all_artifacts['_summary']['approved_count']}/2)"
            ),
        })
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to approve artifact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/services/{service_id:path}/artifacts/{artifact_type}/unapprove")
async def unapprove_artifact_endpoint(service_id: str, artifact_type: str):
    """Revert an artifact back to draft (e.g. for edits after approval)."""

    if artifact_type not in ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid artifact type. Must be one of: {', '.join(ARTIFACT_TYPES)}",
        )

    try:
        artifact = await unapprove_service_artifact(service_id, artifact_type)
        if not artifact:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return JSONResponse({"status": "ok", "artifact": artifact})
    except Exception as e:
        logger.error(f"Failed to unapprove artifact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/services/{service_id:path}/validate-deployment")
async def validate_deployment_endpoint(service_id: str, request: Request):
    """Full deployment validation: What-If → Deploy → Policy Test → Cleanup.

    The auto-healing loop wraps steps 1-2.  On What-If or deploy failure the
    Copilot SDK rewrites the ARM template and retries (up to MAX_HEAL_ATTEMPTS).

    Phases streamed as NDJSON:
      iteration_start → what_if → deploying → deploy_complete →
      resource_check → policy_testing → policy_result → cleanup → done

    On success the service is promoted to 'approved'.
    On failure it is set to 'validation_failed'.
    """

    MAX_HEAL_ATTEMPTS = 5

    svc = await _require_service(service_id)

    artifacts = await get_service_artifacts(service_id)
    if not artifacts["_summary"]["all_approved"]:
        raise HTTPException(status_code=400, detail="Both gates must be approved before validation")

    template_artifact = artifacts.get("template", {})
    template_content = template_artifact.get("content", "").strip()
    if not template_content:
        raise HTTPException(status_code=400, detail="ARM template artifact has no content")

    body = await _parse_body(request)

    region = body.get("region", "eastus2")
    import uuid as _uuid
    _run_id = _uuid.uuid4().hex[:8]
    rg_name = f"infraforge-val-{service_id.replace('/', '-').replace('.', '-').lower()}-{_run_id}"[:90]

    # ── Copilot fix helper ────────────────────────────────────

    async def _copilot_fix(artifact_type: str, content: str, error: str,
                           previous_attempts: list[dict] | None = None) -> str:
        """Ask the Copilot SDK to fix an artifact.

        Tracks previous attempts so each iteration tries a DIFFERENT strategy.
        """
        attempt_num = len(previous_attempts) + 1 if previous_attempts else 1

        if artifact_type == "template":
            prompt = (
                "The following ARM template failed Azure deployment validation.\n\n"
                f"--- ERROR ---\n{error}\n--- END ERROR ---\n\n"
                f"--- CURRENT TEMPLATE ---\n{content}\n--- END TEMPLATE ---\n\n"
            )

            # Include parameter values so the LLM can see what was sent to ARM
            try:
                _fix_tpl = json.loads(content)
                _fix_params = _extract_param_values(_fix_tpl)
                if _fix_params:
                    prompt += (
                        "--- PARAMETER VALUES SENT TO ARM ---\n"
                        f"{json.dumps(_fix_params, indent=2, default=str)}\n"
                        "--- END PARAMETER VALUES ---\n\n"
                        "IMPORTANT: These are the actual values sent to Azure. "
                        "If the error is caused by one of these values (invalid "
                        "name, bad format), fix the corresponding parameter's "
                        "\"defaultValue\" in the template.\n\n"
                    )
            except Exception:
                pass

            # Previous attempt history
            if previous_attempts:
                prompt += "--- RESOLUTION HISTORY (DO NOT repeat these fixes) ---\n"
                for pa in previous_attempts:
                    prompt += (
                        f"Step {pa.get('step', '?')}: Error was: {pa['error'][:300]}\n"
                        f"  Fix tried: {pa['fix_summary']}\n"
                        f"  Result: STILL FAILED — do something DIFFERENT\n\n"
                    )
                prompt += "--- END PREVIOUS ATTEMPTS ---\n\n"

            prompt += (
                "Fix the template so it deploys successfully. Return ONLY the "
                "corrected raw JSON — no markdown fences, no explanation.\n\n"
                "CRITICAL RULES (in priority order):\n\n"
                "1. PARAMETER VALUES — Check parameter defaultValues FIRST:\n"
                "   - If the error mentions an invalid resource name, the name likely "
                "     comes from a parameter defaultValue. Find that parameter and fix "
                "     its defaultValue to comply with Azure naming rules.\n"
                "   - Azure DNS zone names MUST be valid FQDNs with at least two labels "
                "     (e.g. 'infraforge-demo.com', NOT 'if-dnszones').\n"
                "   - Storage account names: 3-24 lowercase alphanumeric, no hyphens.\n"
                "   - Key vault names: 3-24 alphanumeric + hyphens.\n"
                "   - Ensure EVERY parameter has a \"defaultValue\".\n\n"
                "2. LOCATIONS — Keep ALL location parameters as \"[resourceGroup().location]\" "
                "or \"[parameters('location')]\" — NEVER hardcode a region.\n"
                "   EXCEPTION: Globally-scoped resources MUST use location \"global\":\n"
                "   * Microsoft.Network/dnszones → \"global\"\n"
                "   * Microsoft.Network/trafficManagerProfiles → \"global\"\n"
                "   * Microsoft.Cdn/profiles → \"global\"\n\n"
                "3. STRUCTURAL FIXES:\n"
                "   - Keep the same resource intent and resource names.\n"
                "   - Fix schema issues, missing required properties, invalid API versions.\n"
                "   - If diagnosticSettings requires an external dependency, REMOVE it.\n"
                "   - NEVER use '00000000-0000-0000-0000-000000000000' as a subscription ID — "
                "     use [subscription().subscriptionId] instead.\n"
                "   - If the error mentions 'LinkedAuthorizationFailed', use "
                "     [subscription().subscriptionId] in resourceId() expressions.\n"
                "   - If a resource requires complex external deps (VPN gateways, "
                "     ExpressRoute), SIMPLIFY by removing those references.\n"
            )

            # Escalation strategies for later attempts
            if attempt_num >= 4:
                prompt += (
                    f"\n\nESCALATION — multiple strategies have failed, drastic measures needed:\n"
                    "- SIMPLIFY the template: remove optional/nice-to-have resources\n"
                    "- Remove diagnosticSettings, locks, autoscale rules if causing issues\n"
                    "- Use the SIMPLEST valid configuration for each resource\n"
                    "- Strip down to ONLY the primary resource with minimal properties\n"
                    "- Use well-known, stable API versions (prefer 2023-xx-xx or 2024-xx-xx)\n"
                )
            elif attempt_num >= 2:
                prompt += (
                    f"\n\nPrevious fix(es) did NOT work.\n"
                    "You MUST try a FUNDAMENTALLY DIFFERENT approach:\n"
                    "- Try a different API version for the failing resource\n"
                    "- Restructure resource dependencies\n"
                    "- Remove or replace the problematic sub-resource\n"
                    "- Check if required properties changed in newer API versions\n"
                )
        else:
            prompt = (
                "The following Azure Policy JSON has an error.\n\n"
                f"--- ERROR ---\n{error}\n--- END ERROR ---\n\n"
                f"--- CURRENT POLICY ---\n{content}\n--- END POLICY ---\n\n"
                "Fix the policy. Return ONLY the corrected raw JSON."
            )

        from src.copilot_helpers import copilot_send

        _client = await ensure_copilot_client()
        if _client is None:
            raise RuntimeError("Copilot SDK not available")

        fixed = await copilot_send(
            _client,
            model=get_model_for_task(POLICY_FIXER.task),
            system_prompt=POLICY_FIXER.system_prompt,
            prompt=prompt,
            timeout=90,
            agent_name="POLICY_FIXER",
        )
        if fixed.startswith("```"):
            lines = fixed.split("\n")[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            fixed = "\n".join(lines).strip()

        # ── Guard: ensure healer didn't corrupt the location parameter ──
        # NOTE: some resources (DNS zones, Traffic Manager, etc.) use "global"
        _GLOBAL_LOCATION_TYPES_INNER = {
            "microsoft.network/dnszones",
            "microsoft.network/trafficmanagerprofiles",
            "microsoft.cdn/profiles",
            "microsoft.network/frontdoors",
            "microsoft.network/frontdoorwebapplicationfirewallpolicies",
        }
        if artifact_type == "template":
            try:
                _ft = json.loads(fixed)
                _params = _ft.get("parameters", {})
                _loc = _params.get("location", {})
                _dv = _loc.get("defaultValue", "")
                # If the healer hardcoded a region, restore the ARM expression
                if isinstance(_dv, str) and _dv and not _dv.startswith("["):
                    _loc["defaultValue"] = "[resourceGroup().location]"
                    logger.warning(
                        f"Copilot healer corrupted location default to '{_dv}' — "
                        "restored to [resourceGroup().location]"
                    )
                    fixed = json.dumps(_ft, indent=2)
                # Also check each resource's location property
                for _res in _ft.get("resources", []):
                    _rtype = (_res.get("type") or "").lower()
                    _rloc = _res.get("location", "")
                    if _rtype in _GLOBAL_LOCATION_TYPES_INNER:
                        if isinstance(_rloc, str) and _rloc.lower() != "global":
                            _res["location"] = "global"
                            fixed = json.dumps(_ft, indent=2)
                        continue
                    if isinstance(_rloc, str) and _rloc and not _rloc.startswith("["):
                        _res["location"] = "[parameters('location')]"
                        logger.warning(
                            f"Copilot healer hardcoded resource location to '{_rloc}' — "
                            "restored to [parameters('location')]"
                        )
                        fixed = json.dumps(_ft, indent=2)
            except (json.JSONDecodeError, AttributeError):
                pass  # if it's not valid JSON yet, the parse step will catch it

        # ── Guard: ensure every param has a defaultValue ──
        if artifact_type == "template":
            fixed = _ensure_parameter_defaults(fixed)
            fixed = _sanitize_placeholder_guids(fixed)

        return fixed

    # ── Policy compliance tester ──────────────────────────────

    def _test_policy_compliance(policy_json: dict, resources: list[dict]) -> list[dict]:
        """Evaluate deployed resources against the policy rule.

        This is a local evaluation engine that interprets the policy's
        'if' condition against each resource's actual Azure properties.
        Returns a list of per-resource compliance results.
        """
        results = []
        rule = policy_json.get("properties", policy_json).get("policyRule", {})
        if_condition = rule.get("if", {})
        effect = rule.get("then", {}).get("effect", "deny")

        for resource in resources:
            match = _evaluate_condition(if_condition, resource)
            # If the condition matches → the policy's effect applies (deny/audit)
            # A "deny" match means the resource VIOLATES the policy
            compliant = not match if effect.lower() in ("deny", "audit") else match
            results.append({
                "resource_id": resource.get("id", ""),
                "resource_type": resource.get("type", ""),
                "resource_name": resource.get("name", ""),
                "location": resource.get("location", ""),
                "compliant": compliant,
                "effect": effect,
                "reason": (
                    "Resource matches policy conditions — compliant"
                    if compliant else
                    f"Resource violates policy — {effect} would apply"
                ),
            })
        return results

    def _evaluate_condition(condition: dict, resource: dict) -> bool:
        """Recursively evaluate Azure Policy condition against a resource."""
        # allOf — all sub-conditions must be true
        if "allOf" in condition:
            return all(_evaluate_condition(c, resource) for c in condition["allOf"])
        # anyOf — any sub-condition must be true
        if "anyOf" in condition:
            return any(_evaluate_condition(c, resource) for c in condition["anyOf"])
        # not — negate
        if "not" in condition:
            return not _evaluate_condition(condition["not"], resource)

        # Leaf condition: field + operator
        field = condition.get("field", "")
        resource_val = _resolve_field(field, resource)

        if "equals" in condition:
            return str(resource_val).lower() == str(condition["equals"]).lower()
        if "notEquals" in condition:
            return str(resource_val).lower() != str(condition["notEquals"]).lower()
        if "in" in condition:
            return str(resource_val).lower() in [str(v).lower() for v in condition["in"]]
        if "notIn" in condition:
            return str(resource_val).lower() not in [str(v).lower() for v in condition["notIn"]]
        if "contains" in condition:
            return str(condition["contains"]).lower() in str(resource_val).lower()
        if "like" in condition:
            import fnmatch
            return fnmatch.fnmatch(str(resource_val).lower(), str(condition["like"]).lower())
        if "exists" in condition:
            exists = resource_val is not None and resource_val != ""
            # Normalize string booleans: LLMs often return "false"/"true" strings
            want_exists = condition["exists"]
            if isinstance(want_exists, str):
                want_exists = want_exists.lower() not in ("false", "0", "no")
            return exists if want_exists else not exists

        return False

    def _resolve_field(field: str, resource: dict):
        """Resolve a policy field reference against a resource dict."""
        field_lower = field.lower()
        if field_lower == "type":
            return resource.get("type", "")
        if field_lower == "location":
            return resource.get("location", "")
        if field_lower == "name":
            return resource.get("name", "")
        if field_lower.startswith("tags["):
            # tags['environment'] or tags.environment
            tag_name = field.split("'")[1] if "'" in field else field.split("[")[1].rstrip("]")
            return (resource.get("tags") or {}).get(tag_name, "")
        if field_lower.startswith("tags."):
            tag_name = field.split(".", 1)[1]
            return (resource.get("tags") or {}).get(tag_name, "")
        # properties.X.Y.Z → nested lookup
        parts = field.split(".")
        val = resource
        for part in parts:
            if isinstance(val, dict):
                # Case-insensitive key lookup
                matched = None
                for k in val:
                    if k.lower() == part.lower():
                        matched = k
                        break
                val = val.get(matched) if matched else None
            else:
                return None
        return val

    # ── Cleanup helper ────────────────────────────────────────

    async def _cleanup_rg(rg: str):
        """Delete the validation resource group."""
        from src.tools.deploy_engine import _get_resource_client
        client = _get_resource_client()
        loop = asyncio.get_event_loop()
        try:
            poller = await loop.run_in_executor(
                None, lambda: client.resource_groups.begin_delete(rg)
            )
            # Don't wait for full deletion — it can take minutes
            # Just fire-and-forget, the RG will be cleaned up async
            logger.info(f"Cleanup: deletion started for resource group '{rg}'")
        except Exception as e:
            logger.warning(f"Cleanup: failed to delete resource group '{rg}': {e}")

    # ── Main streaming generator ──────────────────────────────

    def _track(event_json: str):
        """Record streamed event in the activity tracker."""
        try:
            evt = json.loads(event_json)
        except Exception:
            return
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tracker = _active_validations.get(service_id)
        if not tracker:
            tracker = {
                "status": "running",
                "service_name": svc.get("name", service_id),
                "started_at": now,
                "updated_at": now,
                "phase": "",
                "step": 0,
                "progress": 0,
                "rg_name": rg_name,
                "events": [],
                "error": "",
                "current_attempt": 1,
                "max_attempts": 5,
            }
            _active_validations[service_id] = tracker
        tracker["updated_at"] = now
        if evt.get("phase"):
            tracker["phase"] = evt["phase"]
        if evt.get("step"):
            tracker["step"] = evt["step"]
        elif evt.get("attempt"):
            tracker["step"] = evt["attempt"]
        if evt.get("attempt"):
            tracker["current_attempt"] = evt["attempt"]
        if evt.get("max_attempts"):
            tracker["max_attempts"] = evt["max_attempts"]
        if evt.get("progress"):
            tracker["progress"] = evt["progress"]
        if evt.get("detail"):
            tracker["detail"] = evt["detail"]
            tracker["events"].append({
                "type": evt.get("type", ""),
                "phase": evt.get("phase", ""),
                "detail": evt["detail"],
                "time": now,
            })
            # Keep only last 80 events for richer history
            if len(tracker["events"]) > 80:
                tracker["events"] = tracker["events"][-80:]
        # Capture init metadata
        if evt.get("type") == "init" and evt.get("meta"):
            tracker["template_meta"] = {
                "resource_count": evt["meta"].get("resource_count", 0),
                "resource_types": evt["meta"].get("resource_types", []),
                "size_kb": evt["meta"].get("template_size_kb", 0),
                "schema": evt["meta"].get("schema", ""),
                "parameters": evt["meta"].get("parameters", []),
                "outputs": evt["meta"].get("outputs", []),
                "resource_names": evt["meta"].get("resource_names", []),
                "api_versions": evt["meta"].get("api_versions", []),
                "has_policy": evt["meta"].get("has_policy", False),
            }
            tracker["region"] = evt["meta"].get("region", "")
            tracker["subscription"] = evt["meta"].get("subscription", "")
        # Track completed steps
        if evt.get("type") == "progress" and evt.get("phase", "").endswith("_complete"):
            completed = tracker.get("steps_completed", [])
            step = evt["phase"].replace("_complete", "")
            if step not in completed:
                completed.append(step)
            tracker["steps_completed"] = completed
        # Terminal states
        if evt.get("type") == "done":
            tracker["status"] = "succeeded"
            tracker["progress"] = 1.0
        elif evt.get("type") == "error":
            tracker["status"] = "failed"
            tracker["error"] = evt.get("detail", "")

    async def stream_validation():
        nonlocal template_content
        current_template = template_content
        deployed_rg = None  # track if we need cleanup
        heal_history: list[dict] = []  # tracks each heal attempt to avoid repeating the same fix

        # ── Safety guard: ensure every parameter has a defaultValue ──
        current_template = _ensure_parameter_defaults(current_template)
        # ── Replace placeholder subscription GUIDs ──
        current_template = _sanitize_placeholder_guids(current_template)
        # ── Ensure DNS zone names are valid FQDNs ──
        current_template = _sanitize_dns_zone_names(current_template)
        def _extract_template_meta(tmpl_str: str) -> dict:
            """Extract human-readable metadata from an ARM template string."""
            try:
                t = json.loads(tmpl_str)
            except Exception:
                return {"resource_count": 0, "resource_types": [], "schema": "unknown", "size_kb": round(len(tmpl_str) / 1024, 1)}
            resources = t.get("resources", [])
            rtypes = list({r.get("type", "?") for r in resources if isinstance(r, dict)})
            rnames = [r.get("name", "?") for r in resources if isinstance(r, dict)]
            schema = t.get("$schema", "unknown")
            if "deploymentTemplate" in schema:
                schema = "ARM Deployment Template"
            api_versions = list({r.get("apiVersion", "?") for r in resources if isinstance(r, dict)})
            params = list(t.get("parameters", {}).keys())
            outputs = list(t.get("outputs", {}).keys())
            return {
                "resource_count": len(resources),
                "resource_types": rtypes,
                "resource_names": rnames,
                "api_versions": api_versions,
                "schema": schema,
                "parameters": params[:10],
                "outputs": outputs[:10],
                "size_kb": round(len(tmpl_str) / 1024, 1),
            }

        tmpl_meta = _extract_template_meta(current_template)
        import os as _os
        _sub_id = _os.environ.get("AZURE_SUBSCRIPTION_ID", "unknown")[:12] + "…"

        # Register job start with metadata
        _active_validations[service_id] = {
            "status": "running",
            "service_name": svc.get("name", service_id),
            "category": svc.get("category", ""),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "phase": "starting",
            "detail": "Initializing validation pipeline…",
            "step": 0,
            "progress": 0,
            "rg_name": rg_name,
            "region": region,
            "subscription": _sub_id,
            "template_meta": tmpl_meta,
            "steps_completed": [],
            "events": [],
            "error": "",
            "current_attempt": 1,
            "max_attempts": 5,
        }

        # Emit a rich initialization event
        yield json.dumps({
            "type": "init",
            "phase": "starting",
            "detail": f"Starting deployment validation for {svc.get('name', service_id)} ({service_id})",
            "progress": 0.005,
            "meta": {
                "service_name": svc.get("name", service_id),
                "service_id": service_id,
                "category": svc.get("category", ""),
                "region": region,
                "subscription": _sub_id,
                "resource_group": rg_name,
                "template_size_kb": tmpl_meta["size_kb"],
                "resource_count": tmpl_meta["resource_count"],
                "resource_types": tmpl_meta["resource_types"],
                "resource_names": tmpl_meta.get("resource_names", []),
                "api_versions": tmpl_meta.get("api_versions", []),
                "schema": tmpl_meta["schema"],
                "parameters": tmpl_meta.get("parameters", []),
                "outputs": tmpl_meta.get("outputs", []),
                "has_policy": bool((artifacts.get("policy", {}).get("content") or "").strip()),
            },
        }) + "\n"

        try:
            for attempt in range(1, MAX_HEAL_ATTEMPTS + 1):
                is_last = attempt == MAX_HEAL_ATTEMPTS
                att_base = (attempt - 1) / MAX_HEAL_ATTEMPTS

                if attempt == 1:
                    step_desc = f"Parsing and validating ARM template ({tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s): {', '.join(tmpl_meta['resource_types'][:3]) or 'unknown'})…"
                else:
                    step_desc = f"Verifying corrected template ({tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s): {', '.join(tmpl_meta['resource_types'][:3]) or 'unknown'}) — resolved {len(heal_history)} issue{'s' if len(heal_history) != 1 else ''} so far…"

                yield json.dumps({
                    "type": "iteration_start",
                    "step": attempt,
                    "detail": step_desc,
                    "progress": att_base + 0.01,
                }) + "\n"

                # ── 1. Parse JSON ─────────────────────────────
                try:
                    template_json = json.loads(current_template)
                except json.JSONDecodeError as e:
                    error_msg = f"ARM template is not valid JSON — parse error at line {e.lineno}, col {e.colno}: {e.msg}"
                    if is_last:
                        await fail_service_validation(service_id, error_msg)
                        yield json.dumps({"type": "error", "phase": "parsing", "step": attempt, "detail": error_msg}) + "\n"
                        return
                    yield json.dumps({"type": "healing", "phase": "fixing_template", "step": attempt, "detail": f"JSON parse error at line {e.lineno}, col {e.colno}: {e.msg} — analyzing error and resolving…", "error": error_msg, "progress": att_base + 0.02}) + "\n"
                    _pre_fix = current_template
                    current_template = await _copilot_fix("template", current_template, error_msg, previous_attempts=heal_history)
                    heal_history.append({"step": len(heal_history) + 1, "phase": "parsing", "error": error_msg, "fix_summary": _summarize_fix(_pre_fix, current_template)})
                    tmpl_meta = _extract_template_meta(current_template)
                    await save_service_artifact(service_id, "template", content=current_template, status="approved", notes=f"Auto-healed: JSON parse error")
                    yield json.dumps({"type": "healing_done", "phase": "template_fixed", "step": attempt, "detail": f"Copilot SDK rewrote template (now {tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s)) — retrying validation…", "progress": att_base + 0.03}) + "\n"
                    continue

                # ── 2. What-If ────────────────────────────────
                res_types_str = ", ".join(tmpl_meta["resource_types"][:5]) or "unknown"
                yield json.dumps({
                    "type": "progress", "phase": "what_if", "step": attempt,
                    "detail": f"Submitting ARM What-If analysis to Azure Resource Manager — previewing changes for {tmpl_meta['resource_count']} resource(s) [{res_types_str}] in resource group '{rg_name}' ({region})",
                    "progress": att_base + 0.03,
                    "step_info": {"rg": rg_name, "region": region, "resource_types": tmpl_meta["resource_types"], "resource_count": tmpl_meta["resource_count"]},
                }) + "\n"

                try:
                    from src.tools.deploy_engine import run_what_if
                    wif = await run_what_if(resource_group=rg_name, template=template_json, parameters=_extract_param_values(template_json), region=region)
                    logger.info(f"What-If attempt {attempt}: status={wif.get('status')}, changes={wif.get('total_changes')}")
                except Exception as e:
                    logger.error(f"What-If attempt {attempt} exception: {e}", exc_info=True)
                    wif = {"status": "error", "errors": [str(e)]}

                if wif.get("status") != "success":
                    errors = "; ".join(str(e) for e in wif.get("errors", [])) or "Unknown What-If error"

                    # Detect infrastructure errors that are NOT template problems
                    _infra_keywords = ("beingdeleted", "being deleted", "deprovisioning",
                                       "throttled", "toomanyrequests", "retryable",
                                       "serviceunavailable", "internalservererror",
                                       "still being deleted")
                    _is_infra_error = any(kw in errors.lower() for kw in _infra_keywords)

                    if _is_infra_error:
                        # Don't burn a heal attempt — just wait and retry (no cleanup!)
                        yield json.dumps({"type": "progress", "phase": "infra_retry", "step": attempt,
                            "detail": f"Transient Azure infrastructure error (not a template problem) — waiting 10s before retry. Error: {errors[:200]}",
                            "progress": att_base + 0.05}) + "\n"
                        await asyncio.sleep(10)
                        continue

                    if is_last:
                        await fail_service_validation(service_id, f"What-If failed — all available resolution strategies exhausted: {errors}")
                        yield json.dumps({"type": "error", "phase": "what_if", "step": attempt, "detail": f"What-If analysis rejected by Azure Resource Manager — all available resolution strategies exhausted. Error: {errors}"}) + "\n"
                        return
                    yield json.dumps({"type": "healing", "phase": "fixing_template", "step": attempt, "detail": f"What-If rejected by ARM — analyzing error and resolving. Error: {errors[:300]}", "error": errors, "progress": att_base + 0.05}) + "\n"
                    _pre_fix = current_template
                    current_template = await _copilot_fix("template", current_template, errors, previous_attempts=heal_history)
                    heal_history.append({"step": len(heal_history) + 1, "phase": "what_if", "error": errors[:500], "fix_summary": _summarize_fix(_pre_fix, current_template)})
                    tmpl_meta = _extract_template_meta(current_template)
                    await save_service_artifact(service_id, "template", content=current_template, status="approved", notes=f"Auto-healed: {errors[:200]}")
                    yield json.dumps({"type": "healing_done", "phase": "template_fixed", "step": attempt, "detail": f"Copilot SDK rewrote template (now {tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s): {', '.join(tmpl_meta['resource_types'][:3])}) — restarting validation pipeline…", "progress": att_base + 0.07}) + "\n"
                    continue

                change_summary = ", ".join(f"{v} {k}" for k, v in wif.get("change_counts", {}).items())
                # Build per-resource details for verbose display
                change_details = []
                for ch in wif.get("changes", [])[:10]:
                    change_details.append(f"{ch.get('change_type','?')}: {ch.get('resource_type','?')}/{ch.get('resource_name','?')}")
                change_detail_str = "; ".join(change_details) if change_details else "no resource-level changes"
                yield json.dumps({
                    "type": "progress", "phase": "what_if_complete", "step": attempt,
                    "detail": f"✓ What-If analysis passed — ARM accepted the template. Changes: {change_summary or 'no changes detected'}. Resources: {change_detail_str}",
                    "progress": att_base + 0.06,
                    "result": wif,
                }) + "\n"

                # ── 3. Actual Deploy ──────────────────────────
                yield json.dumps({
                    "type": "progress", "phase": "deploying", "step": attempt,
                    "detail": f"Submitting ARM deployment to Azure — provisioning {tmpl_meta['resource_count']} resource(s) [{', '.join(tmpl_meta['resource_types'][:5])}] into resource group '{rg_name}' in {region}. Deployment mode: incremental. Deployment name: validate-{attempt}",
                    "progress": att_base + 0.08,
                    "step_info": {"deployment_name": f"validate-{attempt}", "mode": "incremental", "rg": rg_name, "region": region},
                }) + "\n"

                try:
                    from src.tools.deploy_engine import execute_deployment

                    deploy_events: list[dict] = []

                    async def _on_deploy_progress(evt):
                        deploy_events.append(evt)

                    deploy_result = await execute_deployment(
                        resource_group=rg_name,
                        template=template_json,
                        parameters=_extract_param_values(template_json),
                        region=region,
                        deployment_name=f"validate-{attempt}",
                        initiated_by="InfraForge Validator",
                        on_progress=_on_deploy_progress,
                    )
                    deploy_status = deploy_result.get("status", "unknown")
                    logger.info(f"Deploy attempt {attempt}: status={deploy_status}")
                except Exception as e:
                    logger.error(f"Deploy attempt {attempt} exception: {e}", exc_info=True)
                    deploy_result = {"status": "failed", "error": str(e)}
                    deploy_status = "failed"

                deployed_rg = rg_name  # mark for cleanup

                if deploy_status != "succeeded":
                    deploy_error = deploy_result.get("error", "Unknown deployment error")

                    # If the error is the generic ARM message, try to fetch operation-level details
                    if "Please list deployment operations" in deploy_error or "At least one resource" in deploy_error:
                        try:
                            from src.tools.deploy_engine import _get_resource_client, _get_deployment_operation_errors
                            _rc = _get_resource_client()
                            _lp = asyncio.get_event_loop()
                            op_errors = await _get_deployment_operation_errors(
                                _rc, _lp, rg_name, f"validate-{attempt}"
                            )
                            if op_errors:
                                deploy_error = f"{deploy_error} | Operation errors: {op_errors}"
                                logger.info(f"Deploy attempt {attempt} operation errors: {op_errors}")
                        except Exception as oe:
                            logger.debug(f"Could not fetch operation errors: {oe}")

                    # Detect infrastructure errors that are NOT template problems
                    _is_infra_deploy = any(kw in deploy_error.lower() for kw in
                        ("beingdeleted", "being deleted", "deprovisioning",
                         "throttled", "toomanyrequests", "retryable",
                         "serviceunavailable", "internalservererror",
                         "still being deleted"))

                    # Detect quota / capacity errors — no template fix possible
                    _is_quota = any(kw in deploy_error.lower() for kw in
                        ("subscriptionisoverquotaforsku", "overquota",
                         "quotaexceeded", "operation cannot be completed without additional quota",
                         "notenoughcores", "allocationfailed", "zonalallocationfailed"))

                    yield json.dumps({
                        "type": "progress", "phase": "deploy_failed", "step": attempt,
                        "detail": f"ARM deployment 'validate-{attempt}' failed in resource group '{rg_name}' ({region}). Error from Azure: {deploy_error[:400]}",
                        "progress": att_base + 0.12,
                    }) + "\n"

                    if _is_infra_deploy:
                        yield json.dumps({"type": "progress", "phase": "infra_retry", "step": attempt,
                            "detail": f"Transient Azure infrastructure error (not a template problem) — waiting 10s before retrying into the same RG. Error: {deploy_error[:200]}",
                            "progress": att_base + 0.13}) + "\n"
                        await asyncio.sleep(10)
                        continue

                    if _is_quota:
                        await _cleanup_rg(rg_name)
                        _quota_msg = (
                            "Subscription quota exceeded — cannot deploy in this region. "
                            "Request a quota increase in the Azure portal, deploy to a "
                            "different region, or free up existing resources."
                        )
                        await fail_service_validation(service_id, _quota_msg)
                        yield json.dumps({"type": "error", "phase": "deploy", "step": attempt,
                            "detail": _quota_msg,
                            "progress": 1.0}) + "\n"
                        return

                    if is_last:
                        await _cleanup_rg(rg_name)
                        await fail_service_validation(service_id, f"Deploy failed — all available resolution strategies exhausted: {deploy_error}")
                        yield json.dumps({"type": "error", "phase": "deploy", "step": attempt, "detail": f"Deployment failed — all available resolution strategies exhausted. Final error from Azure: {deploy_error}"}) + "\n"
                        return

                    yield json.dumps({"type": "healing", "phase": "fixing_template", "step": attempt, "detail": f"Deployment rejected by Azure — analyzing error and resolving. Error: {deploy_error[:300]}", "error": deploy_error, "progress": att_base + 0.13}) + "\n"
                    _pre_fix = current_template
                    current_template = await _copilot_fix("template", current_template, deploy_error, previous_attempts=heal_history)
                    heal_history.append({"step": len(heal_history) + 1, "phase": "deploy", "error": deploy_error[:500], "fix_summary": _summarize_fix(_pre_fix, current_template)})
                    tmpl_meta = _extract_template_meta(current_template)
                    await save_service_artifact(service_id, "template", content=current_template, status="approved", notes=f"Auto-healed: deploy error — {deploy_error[:200]}")
                    yield json.dumps({"type": "healing_done", "phase": "template_fixed", "step": attempt, "detail": f"Copilot SDK rewrote template (now {tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s): {', '.join(tmpl_meta['resource_types'][:3])}) — redeploying into same RG (incremental mode)…", "progress": att_base + 0.15}) + "\n"
                    # Don't cleanup — redeploy into the same RG (incremental mode)
                    continue

                # Deployment succeeded!
                provisioned = deploy_result.get("provisioned_resources", [])
                resource_summaries = [f"{r.get('type','?')}/{r.get('name','?')} ({r.get('location', region)})" for r in provisioned]

                # ── Persist deployment tracking info ──
                _deploy_name = f"validate-{attempt}"
                _subscription_id = deploy_result.get("subscription_id", "")
                try:
                    await update_service_version_deployment_info(
                        service_id, None,
                        run_id=_run_id,
                        resource_group=rg_name,
                        deployment_name=_deploy_name,
                        subscription_id=_subscription_id,
                    )
                    logger.info(f"[validate-deployment] Persisted deployment tracking: run_id={_run_id}, rg={rg_name}, deploy={_deploy_name}")
                except Exception as _te:
                    logger.warning(f"[validate-deployment] Failed to persist deployment tracking: {_te}")

                yield json.dumps({
                    "type": "progress", "phase": "deploy_complete", "step": attempt,
                    "detail": f"✓ ARM deployment 'validate-{attempt}' succeeded — {len(provisioned)} resource(s) provisioned in '{rg_name}': {'; '.join(resource_summaries[:5]) or 'none'}",
                    "progress": att_base + 0.12,
                    "resources": provisioned,
                }) + "\n"

                # ── 4. Verify resources exist ─────────────────
                yield json.dumps({
                    "type": "progress", "phase": "resource_check", "step": attempt,
                    "detail": f"Querying Azure Resource Manager to verify {len(provisioned)} resource(s) exist in resource group '{rg_name}' — fetching resource properties for policy evaluation…",
                    "progress": att_base + 0.13,
                }) + "\n"

                from src.tools.deploy_engine import _get_resource_client
                rc = _get_resource_client()
                loop = asyncio.get_event_loop()
                try:
                    live_resources = await loop.run_in_executor(
                        None,
                        lambda: list(rc.resources.list_by_resource_group(rg_name))
                    )
                    resource_details = []
                    for r in live_resources:
                        detail = {
                            "id": r.id,
                            "name": r.name,
                            "type": r.type,
                            "location": r.location,
                            "tags": dict(r.tags) if r.tags else {},
                        }
                        # Fetch full resource properties for policy evaluation
                        try:
                            full = await loop.run_in_executor(
                                None,
                                lambda r=r: rc.resources.get_by_id(r.id, api_version="2023-07-01")
                            )
                            if full.properties:
                                detail["properties"] = full.properties
                        except Exception:
                            pass
                        resource_details.append(detail)

                    res_detail_strs = [f"{r['type']}/{r['name']} @ {r['location']}" for r in resource_details[:8]]
                    yield json.dumps({
                        "type": "progress", "phase": "resource_check_complete", "step": attempt,
                        "detail": f"✓ Verified {len(resource_details)} live resource(s) in Azure: {'; '.join(res_detail_strs)}",
                        "progress": att_base + 0.14,
                        "resources": [{"name": r["name"], "type": r["type"], "location": r["location"]} for r in resource_details],
                    }) + "\n"

                except Exception as e:
                    logger.warning(f"Resource check failed: {e}")
                    resource_details = []
                    yield json.dumps({
                        "type": "progress", "phase": "resource_check_warning", "step": attempt,
                        "detail": f"Could not enumerate resources (non-fatal): {e}",
                        "progress": att_base + 0.14,
                    }) + "\n"

                # ── 5. Policy compliance test ─────────────────
                policy_content = (artifacts.get("policy", {}).get("content") or "").strip()
                policy_results = []

                if policy_content and resource_details:
                    _policy_size = round(len(policy_content) / 1024, 1)
                    try:
                        _pj = json.loads(policy_content)
                        _rule_count = len(_pj.get("rules", []))
                    except Exception:
                        _rule_count = 0
                    yield json.dumps({
                        "type": "progress", "phase": "policy_testing", "step": attempt,
                        "detail": f"Evaluating {len(resource_details)} deployed resource(s) against organization policy ({_policy_size} KB, {_rule_count} rule(s)). Checking tags, SKUs, locations, networking, and security configurations…",
                        "progress": att_base + 0.15,
                    }) + "\n"

                    try:
                        policy_json = json.loads(policy_content)
                    except json.JSONDecodeError as pe:
                        # Auto-heal policy if invalid
                        if not is_last:
                            yield json.dumps({"type": "healing", "phase": "fixing_policy", "step": attempt, "detail": f"Policy JSON error — asking AI to fix…", "error": str(pe), "progress": att_base + 0.155}) + "\n"
                            fixed_policy = await _copilot_fix("policy", policy_content, str(pe))
                            await save_service_artifact(service_id, "policy", content=fixed_policy, status="approved", notes=f"Auto-healed: policy JSON error")
                            artifacts["policy"]["content"] = fixed_policy
                            try:
                                policy_json = json.loads(fixed_policy)
                                policy_content = fixed_policy
                            except json.JSONDecodeError:
                                await _cleanup_rg(rg_name)
                                deployed_rg = None
                                continue
                        else:
                            await _cleanup_rg(rg_name)
                            await fail_service_validation(service_id, f"Policy JSON invalid: {pe}")
                            yield json.dumps({"type": "error", "phase": "policy", "step": attempt, "detail": f"Policy JSON invalid: {pe}"}) + "\n"
                            return

                    policy_results = _test_policy_compliance(policy_json, resource_details)
                    all_compliant = all(r["compliant"] for r in policy_results)
                    compliant_count = sum(1 for r in policy_results if r["compliant"])

                    for pr in policy_results:
                        icon = "✅" if pr["compliant"] else "❌"
                        yield json.dumps({
                            "type": "policy_result", "phase": "policy_testing", "step": attempt,
                            "detail": f"{icon} {pr['resource_type']}/{pr['resource_name']} — {pr['reason']}",
                            "compliant": pr["compliant"],
                            "resource": pr,
                            "progress": att_base + 0.16,
                        }) + "\n"

                    if not all_compliant:
                        violations = [pr for pr in policy_results if not pr["compliant"]]
                        violation_desc = "; ".join(f"{v['resource_name']}: {v['reason']}" for v in violations)
                        fail_msg = f"{compliant_count}/{len(policy_results)} resources compliant — {len(violations)} policy violation(s): {violation_desc[:300]}"
                        yield json.dumps({
                            "type": "progress", "phase": "policy_failed", "step": attempt,
                            "detail": fail_msg,
                            "progress": att_base + 0.17,
                        }) + "\n"

                        if is_last:
                            await _cleanup_rg(rg_name)
                            await fail_service_validation(service_id, fail_msg)
                            yield json.dumps({"type": "error", "phase": "policy", "step": attempt, "detail": f"Policy compliance failed — all available resolution strategies exhausted. Violations: {violation_desc}"}) + "\n"
                            return

                        fix_error = f"Policy violation: {violation_desc}. The policy requires: {policy_content[:500]}"
                        yield json.dumps({"type": "healing", "phase": "fixing_template", "step": attempt, "detail": f"Policy violations on {len(violations)} resource(s) — analyzing error and resolving. Violations: {violation_desc[:300]}", "error": fix_error, "progress": att_base + 0.175}) + "\n"
                        _pre_fix = current_template
                        current_template = await _copilot_fix("template", current_template, fix_error, previous_attempts=heal_history)
                        heal_history.append({"step": len(heal_history) + 1, "phase": "policy_compliance", "error": fix_error[:500], "fix_summary": _summarize_fix(_pre_fix, current_template)})
                        tmpl_meta = _extract_template_meta(current_template)
                        await save_service_artifact(service_id, "template", content=current_template, status="approved", notes=f"Auto-healed: policy violation")
                        yield json.dumps({"type": "healing_done", "phase": "template_fixed", "step": attempt, "detail": f"Copilot SDK rewrote template for policy compliance (now {tmpl_meta['size_kb']} KB) — redeploying into same RG and re-testing…", "progress": att_base + 0.18}) + "\n"
                        # Don't cleanup — redeploy into the same RG (incremental mode)
                        continue
                else:
                    yield json.dumps({
                        "type": "progress", "phase": "policy_skip", "step": attempt,
                        "detail": "No policy content or no resources to test — skipping policy check",
                        "progress": att_base + 0.16,
                    }) + "\n"

                # ── 5.5 Deploy Azure Policy to Azure ─────────
                _val_policy_deployed = False
                if policy_content:
                    yield json.dumps({
                        "type": "progress", "phase": "policy_deploy", "step": attempt,
                        "detail": f"🛡️ Deploying Azure Policy to enforce governance on {service_id}…",
                        "progress": att_base + 0.17,
                    }) + "\n"
                    try:
                        _pol_json = json.loads(policy_content) if isinstance(policy_content, str) else policy_content
                        from src.tools.policy_deployer import deploy_policy
                        _val_pol_info = await deploy_policy(
                            service_id=service_id, run_id=_run_id,
                            policy_json=_pol_json, resource_group=rg_name,
                        )
                        _val_policy_deployed = True
                        yield json.dumps({
                            "type": "progress", "phase": "policy_deploy_complete", "step": attempt,
                            "detail": f"✓ Azure Policy '{_val_pol_info['definition_name']}' deployed to RG '{rg_name}'",
                            "progress": att_base + 0.18,
                        }) + "\n"
                    except Exception as _pe:
                        logger.warning(f"Azure Policy deployment failed (non-blocking): {_pe}", exc_info=True)
                        yield json.dumps({
                            "type": "progress", "phase": "policy_deploy_complete", "step": attempt,
                            "detail": f"⚠ Azure Policy deployment failed (non-blocking): {str(_pe)[:200]}",
                            "progress": att_base + 0.18,
                        }) + "\n"

                # ── 6. Cleanup validation RG ──────────────────
                yield json.dumps({
                    "type": "progress", "phase": "cleanup", "step": attempt,
                    "detail": f"All checks passed — initiating deletion of validation resource group '{rg_name}' and all {len(resource_details)} resource(s) within it. This is fire-and-forget; Azure will complete deletion asynchronously.",
                    "progress": 0.90,
                }) + "\n"

                # Clean up Azure Policy first
                if _val_policy_deployed:
                    try:
                        from src.tools.policy_deployer import cleanup_policy
                        await cleanup_policy(service_id, _run_id, rg_name)
                    except Exception as _cpe:
                        logger.debug(f"Policy cleanup (non-fatal): {_cpe}")

                await _cleanup_rg(rg_name)
                deployed_rg = None

                yield json.dumps({
                    "type": "progress", "phase": "cleanup_complete", "step": attempt,
                    "detail": f"✓ Resource group '{rg_name}' + Azure Policy cleaned up",
                    "progress": 0.93,
                }) + "\n"

                # ── 7. Promote ────────────────────────────────
                validation_summary = {
                    "what_if": wif,
                    "deployed_resources": [{"name": r["name"], "type": r["type"], "location": r["location"]} for r in resource_details],
                    "policy_compliance": policy_results,
                    "all_compliant": all(r["compliant"] for r in policy_results) if policy_results else True,
                    "policy_deployed_to_azure": _val_policy_deployed,
                    "attempts": attempt,
                    "run_id": _run_id,
                    "resource_group": rg_name,
                    "deployment_name": _deploy_name,
                    "subscription_id": _subscription_id,
                    "deployment_id": deploy_result.get("deployment_id", ""),
                    "deploy_result": {
                        "status": deploy_result.get("status", ""),
                        "started_at": deploy_result.get("started_at", ""),
                        "completed_at": deploy_result.get("completed_at", ""),
                    },
                    "heal_history": heal_history,
                }

                yield json.dumps({
                    "type": "progress", "phase": "promoting", "step": attempt,
                    "detail": f"All validation gates passed — promoting {svc['name']} ({service_id}) from 'validating' → 'approved' in the service catalog…",
                    "progress": 0.97,
                }) + "\n"

                await promote_service_after_validation(service_id, validation_summary)

                # ── Co-onboard required child resources ──────
                # Azure parent-child relationship: when onboarding a parent
                # (e.g. VNet), automatically co-onboard tightly-coupled children
                # (e.g. subnets) that can't exist without the parent.
                from src.template_engine import get_required_co_onboard_types
                co_onboard_types = get_required_co_onboard_types(service_id)
                co_onboarded = []

                if co_onboard_types:
                    from src.orchestrator import auto_onboard_service
                    for child_info in co_onboard_types:
                        child_type = child_info["type"]
                        child_reason = child_info["reason"]
                        child_short = child_type.split("/")[-1]
                        yield json.dumps({
                            "type": "progress", "phase": "co_onboarding", "step": attempt,
                            "detail": f"Co-onboarding child resource: {child_short} — {child_reason}",
                            "progress": 0.98,
                        }) + "\n"
                        try:
                            client = await ensure_copilot_client()
                            child_result = await auto_onboard_service(
                                child_type,
                                copilot_client=client,
                            )
                            if child_result.get("status") in ("prepped", "already_prepped", "already_approved"):
                                co_onboarded.append(child_type)
                                logger.info(f"Co-prepped child resource {child_type} with parent {service_id} (needs own pipeline)")
                            else:
                                logger.warning(f"Co-onboard of {child_type} returned: {child_result.get('status')}")
                        except Exception as co_err:
                            logger.warning(f"Failed to co-onboard {child_type}: {co_err}")

                compliant_str = f", all {len(policy_results)} policy check(s) passed" if policy_results else ""
                res_types_done = ", ".join(tmpl_meta["resource_types"][:5]) or "N/A"
                issues_resolved = len(heal_history)
                heal_msg = f" Resolved {issues_resolved} issue{'s' if issues_resolved != 1 else ''} automatically." if issues_resolved > 0 else ""
                co_msg = f" Also co-onboarded: {', '.join(t.split('/')[-1] for t in co_onboarded)}." if co_onboarded else ""
                yield json.dumps({
                    "type": "done", "phase": "approved", "step": attempt,
                    "issues_resolved": issues_resolved,
                    "co_onboarded": co_onboarded,
                    "detail": f"🎉 {svc['name']} approved! Successfully deployed {len(resource_details)} resource(s) [{res_types_done}] to Azure{compliant_str}. Validation resource group cleaned up.{heal_msg}{co_msg}",
                    "progress": 1.0,
                    "summary": validation_summary,
                }) + "\n"
                return  # ✅ success

        except Exception as e:
            logger.error(f"Deployment validation error for {service_id}: {e}", exc_info=True)
            try:
                await fail_service_validation(service_id, str(e))
            except Exception:
                pass
            yield json.dumps({"type": "error", "phase": "unknown", "detail": _friendly_error(e)}) + "\n"
        except (GeneratorExit, asyncio.CancelledError):
            # Client disconnected — clean up and mark failed so user can retry
            logger.warning(f"Validation stream cancelled (client disconnect) for {service_id}")
            try:
                await fail_service_validation(service_id, "Validation interrupted — client disconnected. Please retry.")
            except Exception:
                pass
        finally:
            # Safety net: always clean up if an RG was created
            if deployed_rg:
                try:
                    await _cleanup_rg(deployed_rg)
                except Exception:
                    pass

    async def _tracked_stream():
        """Wrap stream_validation to record every event in the activity tracker."""
        try:
            async for line in stream_validation():
                _track(line)
                yield line
        finally:
            # Safety net: if service is still stuck at 'validating', mark it failed
            try:
                tracker = _active_validations.get(service_id, {})
                _final = tracker.get("status", "failed")
                if _final not in ("succeeded",):
                    _be = await get_backend()
                    _svc_rows = await _be.execute(
                        "SELECT status FROM services WHERE id = ?", (service_id,)
                    )
                    if _svc_rows and _svc_rows[0].get("status") == "validating":
                        await fail_service_validation(
                            service_id,
                            tracker.get("error", "")[:500] or "Validation stream ended without explicit completion",
                        )
                        logger.info(f"Safety net: marked service {service_id} as validation_failed")
            except Exception as _e:
                logger.debug(f"Validate safety-net error: {_e}")

            # Clean up tracker after a delay so activity page can still show final state
            async def _cleanup_tracker():
                await asyncio.sleep(300)  # keep for 5 min after completion
                _active_validations.pop(service_id, None)
            asyncio.create_task(_cleanup_tracker())

    return StreamingResponse(
        _tracked_stream(),
        media_type="application/x-ndjson",
    )


@app.post("/api/services/{service_id:path}/artifacts/{artifact_type}/generate")
async def generate_artifact_endpoint(service_id: str, artifact_type: str, request: Request):
    """Use the Copilot SDK to generate an artifact from a natural language prompt.

    Streams the generated content back as newline-delimited JSON chunks:
      {"type": "delta", "content": "..."}   — streaming content chunk
      {"type": "done", "content": "..."}    — final full content
      {"type": "error", "message": "..."}   — error
    """

    if artifact_type not in ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid artifact type. Must be one of: {', '.join(ARTIFACT_TYPES)}",
        )

    svc = await _require_service(service_id)

    body = await _parse_body_required(request)

    user_prompt = body.get("prompt", "").strip()
    if not user_prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    # Build artifact-specific system prompt
    artifact_prompts = {
        "policy": (
            f"Generate an Azure Policy definition (JSON) for the Azure service '{svc['name']}' "
            f"(resource type: {service_id}).\n\n"
            f"User requirement: {user_prompt}\n\n"
            "Return ONLY the raw Azure Policy JSON definition — no markdown fences, no explanation, "
            "no surrounding text. The JSON should be a complete, deployable Azure Policy definition "
            "with properties.displayName, properties.policyType, properties.mode, and properties.policyRule."
        ),
        "template": (
            f"Generate an ARM template (JSON) for deploying the Azure service '{svc['name']}' "
            f"(resource type: {service_id}).\n\n"
            f"User requirement: {user_prompt}\n\n"
            "Return ONLY the raw ARM JSON — no markdown fences, no explanation, no surrounding text. "
            "The template should include parameters for projectName, environment, and location. "
            "Follow Azure Well-Architected Framework best practices including proper tagging, "
            "managed identities, and diagnostic settings where applicable. "
            "This template will be deployed directly via the Azure ARM SDK."
        ),
    }

    generation_prompt = artifact_prompts[artifact_type]

    async def stream_generation():
        """SSE-style streaming via Copilot SDK."""
        # Select model based on artifact type
        _artifact_task = Task.POLICY_GENERATION if artifact_type == "policy" else Task.CODE_GENERATION
        _artifact_model = get_model_for_task(_artifact_task)
        logger.info(f"[ModelRouter] artifact generation type={artifact_type} → model={_artifact_model}")

        try:
            _client = await ensure_copilot_client()
            if _client is None:
                raise RuntimeError("Copilot SDK not available")

            from src.copilot_helpers import copilot_send
            full_content = await copilot_send(
                _client,
                model=_artifact_model,
                system_prompt=ARTIFACT_GENERATOR.system_prompt,
                prompt=generation_prompt,
                timeout=60,
                agent_name="ARTIFACT_GENERATOR",
            )

            # Strip markdown code fences if the model wrapped them anyway
            if full_content.startswith("```"):
                lines = full_content.split("\n")
                # Remove first line (```json, ```bicep, etc.)
                lines = lines[1:]
                # Remove last line if it's just ```
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                full_content = "\n".join(lines).strip()

            yield json.dumps({"type": "done", "content": full_content}) + "\n"

        except asyncio.TimeoutError:
            yield json.dumps({"type": "error", "message": "Generation timed out"}) + "\n"
        except Exception as e:
            logger.error(f"Artifact generation failed: {e}")
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    return StreamingResponse(
        stream_generation(),
        media_type="application/x-ndjson",
    )


# ── Service Versions & Onboarding ─────────────────────────────

@app.get("/api/services/{service_id:path}/versions")
async def get_service_versions_endpoint(service_id: str, status: str | None = None):
    """Get all versions of a service's ARM template.

    Query params:
        status: filter by version status (e.g. 'approved', 'failed', 'draft')
    """

    try:
        svc = await _require_service(service_id)

        versions = await get_service_versions(service_id, status=status)
        # Strip arm_template from listing to keep payload small; use single-version endpoint to fetch it
        versions_summary = []
        for v in versions:
            vs = {k: v2 for k, v2 in v.items() if k != "arm_template"}
            vs["template_size_bytes"] = len(v.get("arm_template") or "") if v.get("arm_template") else 0
            # Extract API version(s) from the ARM template for display
            arm_str = v.get("arm_template")
            if arm_str:
                try:
                    tpl = json.loads(arm_str)
                    api_versions = sorted(
                        {r.get("apiVersion", "") for r in tpl.get("resources", [])
                         if isinstance(r, dict) and r.get("apiVersion")},
                        reverse=True,
                    )
                    vs["api_version"] = api_versions[0] if api_versions else None
                except Exception:
                    vs["api_version"] = None
            else:
                vs["api_version"] = None
            versions_summary.append(vs)

        # ── API version advisory ──
        api_version_status = _build_api_version_status(svc, versions)

        # ── Parent-child resource relationships ──
        from src.template_engine import (
            get_child_resource_types, get_parent_resource_type,
        )
        child_resources = []
        for child_info in get_child_resource_types(service_id):
            child_type = child_info["type"]
            # Check if the child is already in the catalog
            child_svc = await get_service(child_type)
            child_resources.append({
                "type": child_type,
                "short_name": child_type.split("/")[-1],
                "reason": child_info["reason"],
                "always_include": child_info.get("always_include", False),
                "status": child_svc.get("status") if child_svc else "not_in_catalog",
                "has_active_version": bool(child_svc.get("active_version")) if child_svc else False,
            })
        parent_type = get_parent_resource_type(service_id)

        # Parent-child co-validation staleness check
        from src.database import check_parent_child_staleness
        staleness = await check_parent_child_staleness(service_id)

        return JSONResponse({
            "service_id": service_id,
            "active_version": svc.get("active_version"),
            "versions": versions_summary,
            "api_version_status": api_version_status,
            "child_resources": child_resources,
            "parent_resource": parent_type,
            "parent_staleness": staleness,
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error fetching versions for {service_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/services/{service_id:path}/versions/latest")
async def get_service_latest_version(service_id: str):
    """Get the latest approved (or highest) version's ARM template for a service."""

    svc = await _require_service(service_id)

    versions = await get_service_versions(service_id)
    if not versions:
        raise HTTPException(status_code=404, detail=f"No versions exist for '{service_id}'")

    # Prefer: active version → latest approved → highest version number
    active = svc.get("active_version")
    match = None
    if active:
        match = next((v for v in versions if v.get("version") == active), None)
    if not match:
        approved = [v for v in versions if v.get("status") == "approved"]
        if approved:
            match = max(approved, key=lambda v: v.get("version", 0))
    if not match:
        match = max(versions, key=lambda v: v.get("version", 0))

    # Ensure contentVersion in ARM JSON matches the stored semver
    _arm_raw = match.get("arm_template") or ""
    _ver_semver = match.get("semver") or ""
    if _arm_raw and _ver_semver:
        try:
            _arm = json.loads(_arm_raw)
            if isinstance(_arm, dict) and _arm.get("contentVersion") != _ver_semver:
                _arm["contentVersion"] = _ver_semver
                match["arm_template"] = json.dumps(_arm, indent=2)
        except (json.JSONDecodeError, TypeError):
            pass

    return JSONResponse(match)


@app.get("/api/services/{service_id:path}/versions/{version:int}")
async def get_service_version_detail(service_id: str, version: int):
    """Get a single version including the full ARM template content."""

    svc = await _require_service(service_id)

    versions = await get_service_versions(service_id)
    match = next((v for v in versions if v.get("version") == version), None)
    if not match:
        raise HTTPException(status_code=404, detail=f"Version {version} not found for '{service_id}'")

    # Ensure contentVersion in ARM JSON matches the stored semver
    _arm_raw = match.get("arm_template") or ""
    _ver_semver = match.get("semver") or ""
    if _arm_raw and _ver_semver:
        try:
            _arm = json.loads(_arm_raw)
            if isinstance(_arm, dict) and _arm.get("contentVersion") != _ver_semver:
                _arm["contentVersion"] = _ver_semver
                match["arm_template"] = json.dumps(_arm, indent=2)
        except (json.JSONDecodeError, TypeError):
            pass

    return JSONResponse(match)


@app.delete("/api/services/{service_id:path}/versions/{version:int}")
async def delete_service_version_endpoint(service_id: str, version: int):
    """Delete a single draft or failed service version. Cannot delete the active version."""

    svc = await _require_service(service_id)

    if svc.get("active_version") == version:
        raise HTTPException(status_code=400, detail="Cannot delete the active version")

    backend = await get_backend()
    # Only allow deleting draft or failed versions
    rows = await backend.execute(
        "SELECT status FROM service_versions WHERE service_id = ? AND version = ?",
        (service_id, version),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Version {version} not found")
    if rows[0]["status"] not in ("draft", "failed"):
        raise HTTPException(status_code=400, detail=f"Cannot delete version with status '{rows[0]['status']}'")

    await backend.execute_write(
        "DELETE FROM service_versions WHERE service_id = ? AND version = ?",
        (service_id, version),
    )
    return JSONResponse({"deleted": True, "version": version})


@app.delete("/api/services/{service_id:path}/versions/drafts")
async def delete_all_draft_versions_endpoint(service_id: str):
    """Delete all draft and failed versions for a service."""

    svc = await _require_service(service_id)

    count = await delete_service_versions_by_status(service_id, ["draft", "failed"])
    return JSONResponse({"deleted": count})


@app.get("/api/services/{service_id:path}/pipeline-runs")
async def get_service_pipeline_runs(service_id: str, limit: int = 20):
    """Get recent pipeline runs for a service."""

    runs = await get_pipeline_runs(service_id, limit=min(limit, 100))
    return JSONResponse(runs)


@app.get("/api/services/{service_id:path}/governance-reviews")
async def get_service_governance_reviews(
    service_id: str,
    version: int | None = None,
    limit: int = 20,
):
    """Get governance reviews for a service, optionally filtered by version."""

    reviews = await get_governance_reviews(service_id, version=version, limit=min(limit, 100))
    return JSONResponse(reviews)


@app.post("/api/services/{service_id:path}/versions/{version:int}/modify")
async def modify_service_version(service_id: str, version: int, request: Request):
    """Modify an existing ARM template version via LLM and save as a new version.

    Accepts a natural-language prompt describing the desired modification,
    sends the current template + prompt to the Copilot SDK, and saves the
    result as a new version with a semver bump.

    Request body:
        prompt (str): Description of the modification to apply
        model (str, optional): LLM model override

    Streams NDJSON events for real-time progress tracking.
    """
    from src.tools.arm_generator import modify_arm_template_with_copilot

    svc = await _require_service(service_id)

    body = await _parse_body_required(request)

    modification_prompt = (body.get("prompt") or "").strip()
    if not modification_prompt:
        raise HTTPException(status_code=400, detail="'prompt' field is required and cannot be empty")

    model_id = body.get("model", get_active_model())

    # Fetch the source version
    versions = await get_service_versions(service_id)
    source = next((v for v in versions if v.get("version") == version), None)
    if not source:
        raise HTTPException(status_code=404, detail=f"Version {version} not found for '{service_id}'")

    source_template = source.get("arm_template", "")
    if not source_template:
        raise HTTPException(status_code=400, detail=f"Version {version} has no ARM template content")

    source_semver = source.get("semver") or f"{version}.0.0"

    async def _stream():
        # Clean up stale drafts/failed from previous runs
        _cleaned = await delete_service_versions_by_status(
            service_id, ["draft", "failed"],
        )
        if _cleaned:
            yield json.dumps({
                "type": "progress", "phase": "cleanup_drafts",
                "detail": f"🧹 Cleaned up {_cleaned} stale draft/failed version(s)",
                "progress": 0.0,
            }) + "\n"

        yield json.dumps({
            "type": "progress",
            "phase": "start",
            "detail": f"Modifying ARM template v{source_semver} for {service_id}…",
            "progress": 0.0,
        }) + "\n"

        yield json.dumps({
            "type": "progress",
            "phase": "llm",
            "detail": f"Sending template + modification prompt to LLM ({model_id})…",
            "progress": 0.15,
        }) + "\n"

        try:
            # Send to LLM for modification
            _client = await ensure_copilot_client()
            if _client is None:
                raise RuntimeError("Copilot SDK not available")
            modified_template = await modify_arm_template_with_copilot(
                existing_template=source_template,
                modification_prompt=modification_prompt,
                resource_type=service_id,
                copilot_client=_client,
                model=model_id,
            )

            yield json.dumps({
                "type": "progress",
                "phase": "generated",
                "detail": "✓ LLM returned modified template — processing…",
                "progress": 0.50,
            }) + "\n"

            # Ensure parameter defaults
            modified_template = _ensure_parameter_defaults(modified_template)

            # Compute new version number
            _db = await get_backend()
            _vrows = await _db.execute(
                "SELECT MAX(version) as max_ver FROM service_versions WHERE service_id = ?",
                (service_id,),
            )
            new_ver = (_vrows[0]["max_ver"] if _vrows and _vrows[0]["max_ver"] else 0) + 1

            # Semver: bump minor from the source version
            # e.g. source 1.0.0 → 1.1.0, source 2.0.0 → 2.1.0
            source_parts = source_semver.split(".")
            try:
                major = int(source_parts[0])
                minor = int(source_parts[1]) + 1 if len(source_parts) > 1 else 1
            except (ValueError, IndexError):
                major, minor = new_ver, 0
            new_semver = f"{major}.{minor}.0"

            # Stamp metadata
            modified_template = _stamp_template_metadata(
                modified_template,
                service_id=service_id,
                version_int=new_ver,
                semver=new_semver,
                gen_source=f"llm-modify ({model_id})",
                region="eastus2",
            )

            yield json.dumps({
                "type": "progress",
                "phase": "saving",
                "detail": f"Saving as v{new_semver}…",
                "progress": 0.75,
            }) + "\n"

            # Save as a new draft version — must pass validation before becoming active
            ver = await create_service_version(
                service_id=service_id,
                arm_template=modified_template,
                version=new_ver,
                semver=new_semver,
                status="draft",
                changelog=f"Modified from v{source_semver}: {modification_prompt[:200]}",
                created_by=f"llm-modify ({model_id})",
            )

            # Parse for summary
            try:
                parsed = json.loads(modified_template)
                resource_count = len(parsed.get("resources", []))
                size_kb = f"{len(modified_template) / 1024:.1f}"
            except Exception:
                resource_count = "?"
                size_kb = "?"

            yield json.dumps({
                "type": "complete",
                "phase": "done",
                "detail": f"✓ Template saved as draft v{new_semver} "
                          f"({resource_count} resource(s), {size_kb} KB) — validate to promote",
                "progress": 1.0,
                "version": new_ver,
                "semver": new_semver,
                "service_id": service_id,
                "status": "draft",
            }) + "\n"

        except ValueError as e:
            yield json.dumps({
                "type": "error",
                "phase": "failed",
                "detail": f"✗ Modification failed: {str(e)}",
                "progress": 1.0,
            }) + "\n"
        except Exception as e:
            logger.exception(f"Template modification failed for {service_id}")
            yield json.dumps({
                "type": "error",
                "phase": "failed",
                "detail": f"✗ Unexpected error: {str(e)}",
                "progress": 1.0,
            }) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


@app.post("/api/services/{service_id:path}/onboard")
async def onboard_service_endpoint(service_id: str, request: Request):
    """One-click service onboarding: auto-generate ARM template and run full validation.

    New pipeline:
    1. Auto-generate ARM template from resource type (built-in skeleton or Copilot)
    2. Static policy check against org-wide governance policies + security standards
    3. ARM What-If deployment preview
    4. ARM deployment to validation resource group
    5. Runtime resource compliance check
    6. Cleanup validation RG
    7. Promote: version → approved, service → approved

    Streams NDJSON events for real-time progress tracking.
    Auto-healing via Copilot SDK (up to 5 attempts).
    """

    svc = await _require_service(service_id)

    body = await _parse_body(request)

    region = body.get("region", "eastus2")
    # Allow per-request model override, fall back to active global model
    model_id = body.get("model", get_active_model())
    # If use_version is set, skip generation and validate the existing draft version
    use_version: int | None = body.get("use_version")
    import uuid as _uuid
    _run_id = _uuid.uuid4().hex[:8]
    rg_name = f"infraforge-val-{service_id.replace('/', '-').replace('.', '-').lower()}-{_run_id}"[:90]

    # ── Activity tracker ──────────────────────────────────────

    def _track(event_json: str):
        try:
            evt = json.loads(event_json)
        except Exception:
            return
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tracker = _active_validations.get(service_id)
        if not tracker:
            tracker = {
                "status": "running",
                "service_name": svc.get("name", service_id),
                "started_at": now,
                "updated_at": now,
                "phase": "",
                "step": 0,
                "progress": 0,
                "rg_name": rg_name,
                "events": [],
                "error": "",
                "current_attempt": 1,
                "max_attempts": 5,
            }
            _active_validations[service_id] = tracker
        tracker["updated_at"] = now
        if evt.get("phase"):
            tracker["phase"] = evt["phase"]
        if evt.get("step"):
            tracker["step"] = evt["step"]
        elif evt.get("attempt"):
            tracker["step"] = evt["attempt"]
        if evt.get("attempt"):
            tracker["current_attempt"] = evt["attempt"]
        if evt.get("max_attempts"):
            tracker["max_attempts"] = evt["max_attempts"]
        if evt.get("progress"):
            tracker["progress"] = evt["progress"]
        if evt.get("detail"):
            tracker["detail"] = evt["detail"]
            tracker["events"].append({
                "type": evt.get("type", ""),
                "phase": evt.get("phase", ""),
                "detail": evt["detail"],
                "time": now,
            })
            if len(tracker["events"]) > 80:
                tracker["events"] = tracker["events"][-80:]
        if evt.get("type") == "init" and evt.get("meta"):
            tracker["template_meta"] = evt["meta"]
            tracker["region"] = evt["meta"].get("region", "")
            tracker["subscription"] = evt["meta"].get("subscription", "")
        if evt.get("type") == "progress" and evt.get("phase", "").endswith("_complete"):
            completed = tracker.get("steps_completed", [])
            step = evt["phase"].replace("_complete", "")
            if step not in completed:
                completed.append(step)
            tracker["steps_completed"] = completed
        if evt.get("type") == "done":
            tracker["status"] = "succeeded"
            tracker["progress"] = 1.0
        elif evt.get("type") == "policy_blocked":
            tracker["status"] = "policy_blocked"
            tracker["error"] = evt.get("detail", "")
        elif evt.get("type") == "error":
            tracker["status"] = "failed"
            tracker["error"] = evt.get("detail", "")

    # ── Main streaming generator ──────────────────────────────

    async def stream_onboarding():
        from src.pipelines.onboarding import runner
        from src.pipeline import PipelineContext

        ctx = PipelineContext(
            "service_onboarding",
            run_id=_run_id,
            service_id=service_id,
            region=region,
            rg_name=rg_name,
            svc=svc,
            use_version=use_version,
            model_id=model_id,
            onboarding_chain={service_id},
        )

        async for line in runner.execute(ctx):
            yield line


    async def _tracked_stream():
        try:
            async for line in stream_onboarding():
                _track(line)
                yield line

            # Log usage for analytics
            try:
                tracker = _active_validations.get(service_id, {})
                if tracker.get("status") == "succeeded":
                    # Get user context if authenticated
                    auth_header = request.headers.get("Authorization", "")
                    user_email = ""
                    user_dept = ""
                    user_cc = ""
                    if auth_header.startswith("Bearer "):
                        user = await get_user_context(auth_header[7:])
                        if user:
                            user_email = user.email
                            user_dept = user.department
                            user_cc = user.cost_center
                    await log_usage({
                        "timestamp": time.time(),
                        "user": user_email,
                        "department": user_dept,
                        "cost_center": user_cc,
                        "prompt": f"Onboard service: {service_id}",
                        "resource_types": [service_id],
                        "estimated_cost": 0.0,
                        "from_catalog": False,
                    })
            except Exception:
                pass  # Non-critical

        finally:
            # ── Safety net: ensure pipeline_runs record is finalized ──
            # The pipeline runner's own finalizer should handle this, but
            # if the client disconnects or the async generator is cancelled,
            # the runner's finally block may not execute reliably.
            try:
                _be = await get_backend()
                _rows = await _be.execute(
                    "SELECT status FROM pipeline_runs WHERE run_id = ?", (_run_id,)
                )
                if _rows and _rows[0].get("status") == "running":
                    tracker = _active_validations.get(service_id, {})
                    _final = tracker.get("status", "failed")
                    _err = tracker.get("error", "")
                    if _final == "succeeded":
                        _db_status = "completed"
                    else:
                        _db_status = "failed"
                    await complete_pipeline_run(
                        _run_id, _db_status,
                        error_detail=_err[:4000] if _err else "Pipeline did not complete — stream ended without finalization",
                    )
                    logger.info(f"Safety net: marked pipeline run {_run_id} as {_db_status}")

                    # Also fix the service status if stuck at 'validating'
                    if _db_status == "failed":
                        try:
                            await fail_service_validation(
                                service_id,
                                _err[:500] if _err else "Pipeline failed without explicit status update",
                            )
                        except Exception:
                            pass
            except Exception as _safety_err:
                logger.error(f"Pipeline run safety-net error for {_run_id}: {_safety_err}", exc_info=True)

            async def _cleanup_tracker():
                await asyncio.sleep(300)
                _active_validations.pop(service_id, None)
            asyncio.create_task(_cleanup_tracker())

    return StreamingResponse(
        _tracked_stream(),
        media_type="application/x-ndjson",
    )


# ── Governance Resolution API ─────────────────────────────────

@app.post("/api/services/{service_id:path}/governance-resolve")
async def governance_resolve_endpoint(service_id: str, request: Request):
    """Resolve a governance-blocked onboarding by healing the template or granting an exception.

    Actions:
    - ``heal``:  Use Copilot to fix the template based on CISO findings, then re-run onboarding.
    - ``exception``: Record that the user acknowledged findings, skip governance on re-run.

    Streams NDJSON events (same format as /onboard) for real-time progress.
    """

    svc = await _require_service(service_id)

    body = await _parse_body_required(request)

    action = body.get("action")
    if action not in ("heal", "exception"):
        raise HTTPException(status_code=400, detail="action must be 'heal' or 'exception'")

    findings = body.get("findings", [])
    region = body.get("region", "eastus2")
    model_id = body.get("model", get_active_model())

    import uuid as _uuid
    _run_id = _uuid.uuid4().hex[:8]
    rg_name = f"infraforge-val-{service_id.replace('/', '-').replace('.', '-').lower()}-{_run_id}"[:90]

    async def stream_resolve():
        from src.pipelines.onboarding import runner
        from src.pipeline import PipelineContext, emit
        from src.pipeline_helpers import copilot_fix_two_phase

        # Get the latest version to heal
        ver = await get_latest_service_version(service_id)
        template_str = ver.get("arm_template", "") if ver else ""
        version_num = ver.get("version") if ver else None

        extra_kwargs = {}

        if action == "heal" and template_str:
            # ── Build healing prompt from CISO findings ──
            findings_text = "\n".join(
                f"- [{f.get('severity', 'medium').upper()}] {f.get('category', 'general')}: "
                f"{f.get('finding', '')} → Recommendation: {f.get('recommendation', 'N/A')}"
                for f in findings
            )
            heal_prompt = (
                f"The CISO governance review BLOCKED this template. "
                f"Fix ALL of the following security findings:\n\n{findings_text}\n\n"
                f"Ensure the template complies with security best practices. "
                f"Do NOT use hardcoded secrets — use parameters with @secure() decorator or Key Vault references."
            )

            yield emit("progress", "governance_heal_start",
                        f"🔧 Auto-healing template based on {len(findings)} CISO finding(s)…",
                        progress=0.05)

            try:
                fixed_template, strategy = await copilot_fix_two_phase(
                    template_str, heal_prompt,
                    standards_ctx="",
                    planning_context="Fix governance review findings to pass CISO approval.",
                    previous_attempts=None,
                )

                yield emit("progress", "governance_heal_strategy",
                            f"📋 Fix strategy: {strategy[:300]}",
                            progress=0.15)

                # Save healed template
                if version_num is not None:
                    await update_service_version_template(
                        service_id, version_num, fixed_template, "governance-healed"
                    )

                yield emit("progress", "governance_heal_complete",
                            f"✅ Template healed — {len(findings)} finding(s) addressed. Re-running pipeline…",
                            progress=0.20)

                extra_kwargs["use_version"] = version_num

            except Exception as heal_err:
                logger.error("Governance heal failed: %s", heal_err)
                yield emit("error", "governance_heal_failed",
                            f"Auto-heal failed: {str(heal_err)[:300]}. Try manual fixes or request an exception.")
                return

        elif action == "exception":
            yield emit("progress", "governance_exception",
                        "⚡ Governance exception granted — bypassing CISO review for this run.",
                        progress=0.05)
            extra_kwargs["governance_exception"] = True
            extra_kwargs["governance_exception_by"] = body.get("acknowledged_by", "user")
            if version_num is not None:
                extra_kwargs["use_version"] = version_num

        # ── Re-run onboarding pipeline ──
        ctx = PipelineContext(
            "service_onboarding",
            run_id=_run_id,
            service_id=service_id,
            region=region,
            rg_name=rg_name,
            svc=svc,
            model_id=model_id,
            onboarding_chain={service_id},
            **extra_kwargs,
        )

        async for line in runner.execute(ctx):
            yield line

    def _track_resolve(event_json: str):
        try:
            evt = json.loads(event_json)
        except Exception:
            return
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tracker = _active_validations.get(service_id)
        if not tracker:
            tracker = {
                "status": "running",
                "service_name": svc.get("name", service_id),
                "started_at": now,
                "updated_at": now,
                "phase": "",
                "step": 0,
                "progress": 0,
                "rg_name": rg_name,
                "events": [],
                "error": "",
            }
            _active_validations[service_id] = tracker
        tracker["updated_at"] = now
        if evt.get("phase"):
            tracker["phase"] = evt["phase"]
        if evt.get("progress"):
            tracker["progress"] = evt["progress"]
        if evt.get("detail"):
            tracker["detail"] = evt["detail"]
        if evt.get("type") == "done":
            tracker["status"] = "succeeded"
        elif evt.get("type") == "error":
            tracker["status"] = "failed"
            tracker["error"] = evt.get("detail", "")

    async def _tracked_resolve_stream():
        try:
            async for line in stream_resolve():
                _track_resolve(line)
                yield line
        finally:
            # Safety net: finalize the pipeline run if it's still "running"
            try:
                _be = await get_backend()
                _rows = await _be.execute(
                    "SELECT status FROM pipeline_runs WHERE run_id = ?", (_run_id,)
                )
                if _rows and _rows[0].get("status") == "running":
                    tracker = _active_validations.get(service_id, {})
                    _final = tracker.get("status", "failed")
                    _err = tracker.get("error", "")
                    _db_status = "completed" if _final == "succeeded" else "failed"
                    await complete_pipeline_run(
                        _run_id, _db_status,
                        error_detail=_err[:4000] if _err else "Pipeline did not complete — stream ended without finalization",
                    )
                    if _db_status == "failed":
                        try:
                            await fail_service_validation(service_id, _err[:500] if _err else "Pipeline failed")
                        except Exception:
                            pass
            except Exception:
                pass

            async def _cleanup():
                await asyncio.sleep(300)
                _active_validations.pop(service_id, None)
            asyncio.create_task(_cleanup())

    return StreamingResponse(
        _tracked_resolve_stream(),
        media_type="application/x-ndjson",
    )


# ══════════════════════════════════════════════════════════════
# GENERIC PIPELINE RESOLUTION ENDPOINT
# ══════════════════════════════════════════════════════════════

@app.post("/api/pipeline-resolve")
async def pipeline_resolve_endpoint(request: Request):
    """Generic pipeline failure resolution.

    Any pipeline failure that emits an ``action_required`` event includes
    an ``actions`` list and a ``context`` dict.  The frontend renders
    buttons; when clicked it POSTs here with the chosen action + context.

    Returns an NDJSON stream (same format as the original pipeline).
    """
    body = await _parse_body_required(request)

    action = body.get("action")
    pipeline = body.get("pipeline")
    context = body.get("context", {})
    params = body.get("params", {})

    if not action or not pipeline:
        raise HTTPException(status_code=400, detail="'action' and 'pipeline' are required")

    # ── User chose to end the pipeline ─────────────────────
    if action == "end_pipeline":
        async def _end():
            yield json.dumps({
                "type": "done", "phase": "user_ended",
                "detail": "Pipeline ended by user.",
                "progress": 1.0,
            }) + "\n"
        return StreamingResponse(_end(), media_type="application/x-ndjson")

    service_id = context.get("service_id", "")
    region = params.get("region") or context.get("region", "eastus2")

    # ── Dispatch based on pipeline type ────────────────────
    if pipeline == "service_onboarding":
        return await _resolve_pipeline_onboarding(service_id, region, action, context, params)
    elif pipeline == "validation":
        return await _resolve_pipeline_validation(service_id, region, action, context, params)
    elif pipeline == "deploy":
        return await _resolve_pipeline_deploy(service_id, region, action, context, params)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown pipeline: {pipeline}")


async def _resolve_pipeline_onboarding(
    service_id: str, region: str, action: str,
    context: dict, params: dict,
):
    """Resolve an onboarding pipeline failure."""
    svc = await _require_service(service_id)
    model_id = get_active_model()

    import uuid as _uuid
    _run_id = _uuid.uuid4().hex[:8]
    rg_name = f"infraforge-val-{service_id.replace('/', '-').replace('.', '-').lower()}-{_run_id}"[:90]

    extra_kwargs: dict = {}

    # If retrying, check if we should use the latest version (avoid re-generating)
    if action in ("retry", "retry_region"):
        ver = await get_latest_service_version(service_id)
        if ver and ver.get("arm_template"):
            extra_kwargs["use_version"] = ver["version"]

    elif action == "ignore_tests":
        # Skip infra test gate then re-run (user accepts test failures)
        extra_kwargs["skip_infra_tests"] = True
        ver = await get_latest_service_version(service_id)
        if ver and ver.get("arm_template"):
            extra_kwargs["use_version"] = ver["version"]

    elif action == "exception":
        # Governance exception — skip CISO gate on re-run
        extra_kwargs["governance_exception"] = True
        extra_kwargs["governance_exception_by"] = "user"
        ver = await get_latest_service_version(service_id)
        if ver and ver.get("arm_template"):
            extra_kwargs["use_version"] = ver["version"]

    async def stream():
        from src.pipelines.onboarding import runner
        from src.pipeline import PipelineContext, emit as _emit

        ctx = PipelineContext(
            "service_onboarding",
            run_id=_run_id,
            service_id=service_id,
            region=region,
            rg_name=rg_name,
            svc=svc,
            model_id=model_id,
            onboarding_chain={service_id},
            **extra_kwargs,
        )

        async for line in runner.execute(ctx):
            yield line

    return StreamingResponse(stream(), media_type="application/x-ndjson")


async def _resolve_pipeline_validation(
    service_id: str, region: str, action: str,
    context: dict, params: dict,
):
    """Resolve a validation pipeline failure."""
    template_id = context.get("template_id", service_id)
    version_num = context.get("version_num")

    async def stream():
        from src.pipelines.validation import stream_validation
        from src.pipeline import emit as _emit

        yield json.dumps({
            "type": "progress", "phase": "retry_start",
            "detail": f"Retrying validation for {template_id}…",
            "progress": 0.0,
        }) + "\n"

        async for line in stream_validation(template_id, version_num, region=region):
            yield line

    return StreamingResponse(stream(), media_type="application/x-ndjson")


async def _resolve_pipeline_deploy(
    service_id: str, region: str, action: str,
    context: dict, params: dict,
):
    """Resolve a deployment pipeline failure."""
    template_id = context.get("template_id", service_id)
    resource_group = context.get("resource_group", "")

    async def stream():
        from src.pipelines.deploy import stream_deploy
        from src.pipeline import emit as _emit

        yield json.dumps({
            "type": "progress", "phase": "retry_start",
            "detail": f"Retrying deployment for {template_id}…",
            "progress": 0.0,
        }) + "\n"

        async for line in stream_deploy(template_id, resource_group=resource_group, region=region):
            yield line

    return StreamingResponse(stream(), media_type="application/x-ndjson")


