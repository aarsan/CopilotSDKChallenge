"""
InfraForge — Auth, Settings & Analytics Router

Extracted from web.py. Contains:
  - Auth Endpoints (root, version, login, callback, demo, logout, me)
  - Model Settings API
  - Usage Analytics
  - Activity Monitor API
"""

import logging
import os

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from src.config import (
    APP_NAME,
    APP_VERSION,
    AVAILABLE_MODELS,
    get_active_model,
    set_active_model,
)
from src.auth import (
    create_auth_url,
    complete_auth,
    get_pending_session,
    get_user_context,
    invalidate_session,
    is_auth_configured,
)
from src.database import save_session, get_usage_stats
from src.model_router import get_routing_table
from src.web_shared import (
    active_sessions,
    _active_validations,
    _user_context_to_dict,
)

logger = logging.getLogger("infraforge.web")

router = APIRouter()

static_dir = os.path.join(os.path.dirname(__file__), "..", "..", "static")

# ── Auth Endpoints ───────────────────────────────────────────

@router.get("/")
async def root():
    """Serve the main page."""
    index_path = os.path.join(static_dir, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@router.get("/api/version")
async def get_version():
    """Return app version information."""
    return JSONResponse({
        "name": APP_NAME,
        "version": APP_VERSION,
    })


@router.get("/onboarding-docs")
async def onboarding_docs():
    """Serve the onboarding pipeline documentation page."""
    docs_path = os.path.join(static_dir, "onboarding-docs.html")
    with open(docs_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@router.get("/api/auth/config")
async def auth_config():
    """Return auth configuration for the frontend MSAL.js client."""
    from src.config import ENTRA_CLIENT_ID, ENTRA_TENANT_ID, ENTRA_REDIRECT_URI

    return JSONResponse({
        "configured": is_auth_configured(),
        "clientId": ENTRA_CLIENT_ID,
        "tenantId": ENTRA_TENANT_ID,
        "redirectUri": ENTRA_REDIRECT_URI,
    })


@router.get("/api/auth/login")
async def login():
    """Initiate the Entra ID login flow."""
    if not is_auth_configured():
        return JSONResponse(
            {"error": "Microsoft Entra ID is not configured. Set ENTRA_CLIENT_ID, ENTRA_TENANT_ID, and ENTRA_CLIENT_SECRET to enable SSO."},
            status_code=503,
        )

    auth_url, flow_id = create_auth_url()
    return JSONResponse({
        "mode": "entra",
        "authUrl": auth_url,
        "flowId": flow_id,
    })


@router.get("/api/auth/callback")
async def auth_callback(request: Request):
    """Handle the Entra ID redirect after login."""
    flow_id = request.query_params.get("state", "")
    auth_response = dict(request.query_params)

    session_token = complete_auth(flow_id, auth_response)
    if not session_token:
        raise HTTPException(status_code=401, detail="Authentication failed")

    # Persist the session from auth.py's pending store → database
    pending = get_pending_session(session_token)
    if pending:
        user_ctx = pending["user_context"]
        await save_session(
            session_token,
            _user_context_to_dict(user_ctx),
            pending.get("access_token", ""),
            pending.get("claims"),
        )

    # Redirect to the main app with the session token
    return RedirectResponse(url=f"/?session={session_token}")


@router.post("/api/auth/logout")
async def logout(request: Request):
    """End the user session."""
    body = await request.json()
    session_token = body.get("sessionToken", "")

    # Clean up Copilot session
    if session_token in active_sessions:
        try:
            await active_sessions[session_token]["copilot_session"].destroy()
        except Exception:
            pass
        del active_sessions[session_token]

    await invalidate_session(session_token)
    return JSONResponse({"status": "ok"})


@router.get("/api/auth/me")
async def get_current_user(request: Request):
    """Get current user info from session token."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    session_token = auth_header[7:]
    user = await get_user_context(session_token)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired")

    return JSONResponse({
        "displayName": user.display_name,
        "email": user.email,
        "jobTitle": user.job_title,
        "department": user.department,
        "costCenter": user.cost_center,
        "team": user.team,
        "isAdmin": user.is_admin,
        "isPlatformTeam": user.is_platform_team,
    })


# ── Model Settings ────────────────────────────────────────────

@router.get("/api/settings/model")
async def get_model_settings():
    """Return the current active LLM model and all available models."""
    active = get_active_model()
    return JSONResponse({
        "active_model": active,
        "available_models": AVAILABLE_MODELS,
    })


@router.get("/api/settings/model-routing")
async def get_model_routing_settings():
    """Return the model routing table — which model handles which pipeline task and why."""
    return JSONResponse({
        "routing_table": get_routing_table(),
        "chat_model": get_active_model(),
        "description": (
            "InfraForge uses different models for different pipeline tasks. "
            "Reasoning tasks use o3-mini, code generation uses Claude Sonnet 4, "
            "and fixing uses GPT-4.1. The chat model is user-selectable."
        ),
    })


@router.get("/api/agents/activity")
async def get_agents_activity():
    """Return agent registry, routing table, live activity counters, and recent activity log."""
    from src.agents import AGENTS, AgentSpec
    from src.copilot_helpers import get_agent_activity, get_agent_counters

    # Build agent registry with categories
    AGENT_CATEGORIES = {
        "Interactive": ["web_chat", "ciso_advisor", "concierge"],
        "Orchestrator": ["gap_analyst", "arm_template_editor", "policy_checker", "request_parser"],
        "Standards": ["standards_extractor"],
        "ARM Generation": ["arm_modifier", "arm_generator"],
        "Deployment Pipeline": ["template_healer", "error_culprit_detector", "deploy_failure_analyst"],
        "Compliance": ["remediation_planner", "remediation_executor"],
        "Artifact & Healing": ["artifact_generator", "policy_fixer", "deep_template_healer", "llm_reasoner"],
        "Infrastructure Testing": ["infra_tester", "infra_test_analyzer"],
        "Governance Review": ["ciso_reviewer", "cto_reviewer"],
    }

    # Build model routing reasons lookup
    from src.model_router import TASK_MODEL_MAP
    task_reasons = {}
    for t, assignment in TASK_MODEL_MAP.items():
        task_reasons[t.value if hasattr(t, "value") else str(t)] = assignment.reason

    registry = []
    for category, keys in AGENT_CATEGORIES.items():
        for key in keys:
            spec = AGENTS.get(key)
            if spec:
                prompt_text = spec.system_prompt or ""
                prompt_len = len(prompt_text)
                # Rough token estimate (chars / 4)
                prompt_tokens_est = prompt_len // 4
                task_val = spec.task.value if hasattr(spec.task, "value") else str(spec.task)
                registry.append({
                    "key": key,
                    "name": spec.name,
                    "description": spec.description,
                    "task": task_val,
                    "timeout": spec.timeout,
                    "category": category,
                    "prompt_length": prompt_len,
                    "prompt_tokens_est": prompt_tokens_est,
                    "prompt_preview": prompt_text[:300] + ("…" if prompt_len > 300 else ""),
                    "model_reason": task_reasons.get(task_val, ""),
                })

    counters = get_agent_counters()
    activity = get_agent_activity(limit=200)

    return JSONResponse({
        "agents": registry,
        "routing_table": get_routing_table(),
        "counters": counters,
        "activity": activity,
    })


@router.get("/api/agents/heartbeat")
async def get_agents_heartbeat():
    """Lightweight heartbeat: active pipeline count + recent SDK call stats.

    No DB queries — pure in-memory reads from _active_validations and _activity_log.
    Designed for frequent polling (~9s) by the global agent pulse indicator.
    """
    import time
    from src.copilot_helpers import _activity_log, _activity_lock

    # Count active pipelines
    active_pipelines = sum(
        1 for v in _active_validations.values() if v.get("status") == "running"
    )

    # Count recent SDK calls in the last 60 seconds
    now = time.time()
    recent_calls = 0
    last_call_ago = -1

    with _activity_lock:
        for entry in reversed(_activity_log):
            ts_str = entry.get("timestamp", "")
            if not ts_str:
                continue
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                entry_epoch = dt.timestamp()
            except Exception:
                continue

            if last_call_ago < 0:
                last_call_ago = now - entry_epoch

            if now - entry_epoch <= 60:
                recent_calls += 1
            else:
                break  # deque is chronological, older entries follow

    return JSONResponse({
        "active_pipelines": active_pipelines,
        "recent_calls_1m": recent_calls,
        "last_call_ago_sec": round(last_call_ago, 1) if last_call_ago >= 0 else -1,
    })


@router.get("/api/agents/{agent_key}/prompt")
async def get_agent_prompt(agent_key: str):
    """Return the full system prompt for a specific agent."""
    from src.agents import AGENTS
    spec = AGENTS.get(agent_key)
    if not spec:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_key}' not found")

    prompt_text = spec.system_prompt or ""
    return JSONResponse({
        "key": agent_key,
        "name": spec.name,
        "prompt": prompt_text,
        "prompt_length": len(prompt_text),
        "prompt_tokens_est": len(prompt_text) // 4,
    })


@router.put("/api/settings/model")
async def update_model_settings(request: Request):
    """Change the active LLM model at runtime."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    model_id = body.get("model_id", "").strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="model_id is required")

    if not set_active_model(model_id):
        valid_ids = [m["id"] for m in AVAILABLE_MODELS]
        raise HTTPException(
            status_code=400,
            detail=f"Invalid model_id '{model_id}'. Valid models: {', '.join(valid_ids)}",
        )

    logger.info(f"Active LLM model changed to: {model_id}")
    return JSONResponse({"active_model": model_id, "status": "updated"})


# ── Usage Analytics ────────────────────────────────

@router.get("/api/analytics/usage")
async def get_usage_analytics(request: Request):
    """Return usage analytics for the dashboard.

    Shows who's provisioning what, team-level spend, template reuse rates,
    and policy compliance trends.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    session_token = auth_header[7:]
    user = await get_user_context(session_token)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired")

    # Query database — department filter for non-admins
    department_filter = None if (user.is_admin or user.is_platform_team) else user.department
    stats = await get_usage_stats(department=department_filter)

    return JSONResponse(stats)


# ── Activity Monitor API ─────────────────────────────────────

@router.get("/api/activity")
async def get_activity():
    """Return all validation activity: running jobs + recent completed/failed.

    Powers the Activity Monitor page for at-a-glance observability.
    """
    from src.database import get_all_services

    services = await get_all_services()

    # Build activity items from services with validation-related statuses
    jobs = []

    for svc in services:
        status = svc.get("status", "not_approved")
        svc_id = svc.get("id", "")

        # Include services that are validating, validation_failed, recently approved,
        # OR have a live pipeline running (status may still be not_approved early in pipeline)
        live = _active_validations.get(svc_id)
        if status in ("validating", "validation_failed", "approved") or (live and live.get("status") == "running"):

            job = {
                "service_id": svc_id,
                "service_name": svc.get("name", svc_id),
                "category": svc.get("category", ""),
                "status": status,
                "is_running": live is not None and live.get("status") == "running",
                "phase": live.get("phase", "") if live else "",
                "detail": live.get("detail", "") if live else "",
                "step": live.get("step", 0) if live else 0,
                "progress": live.get("progress", 0) if live else (1.0 if status == "approved" else 0),
                "started_at": live.get("started_at", "") if live else "",
                "updated_at": live.get("updated_at", "") if live else "",
                "rg_name": live.get("rg_name", "") if live else "",
                "region": live.get("region", "") if live else "",
                "subscription": live.get("subscription", "") if live else "",
                "template_meta": live.get("template_meta", {}) if live else {},
                "steps_completed": live.get("steps_completed", []) if live else [],
                "events": live.get("events", [])[-50:] if live else [],  # last 50 events
                "error": live.get("error", "") if live else (svc.get("review_notes", "") if status == "validation_failed" else ""),
            }
            jobs.append(job)

    # Sort: running first, then by updated_at descending
    jobs.sort(key=lambda j: (
        0 if j["is_running"] else 1,
        0 if j["status"] == "validating" else 1,
        -(j.get("updated_at") or "0").__hash__(),
    ))

    running_count = sum(1 for j in jobs if j["is_running"])
    validating_count = sum(1 for j in jobs if j["status"] == "validating")
    failed_count = sum(1 for j in jobs if j["status"] == "validation_failed")
    approved_count = sum(1 for j in jobs if j["status"] == "approved")

    return JSONResponse({
        "jobs": jobs,
        "summary": {
            "running": running_count,
            "validating": validating_count,
            "failed": failed_count,
            "approved": approved_count,
            "total": len(jobs),
        },
    })
