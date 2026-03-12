"""
InfraForge — Auth, Settings & Analytics Router

Extracted from web.py. Contains:
  - Auth Endpoints (root, version, login, callback, logout, me)
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
        raise HTTPException(
            status_code=503,
            detail="Entra ID authentication is not configured. Set ENTRA_CLIENT_ID, ENTRA_TENANT_ID, and ENTRA_CLIENT_SECRET.",
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
        "Interactive": ["web_chat", "governance_agent", "ciso_advisor", "concierge"],
        "Orchestrator": ["gap_analyst", "arm_template_editor", "policy_checker", "request_parser"],
        "Standards": ["standards_extractor"],
        "ARM Generation": ["arm_modifier", "arm_generator"],
        "Deployment Pipeline": ["template_healer", "error_culprit_detector", "deploy_failure_analyst"],
        "Compliance": ["remediation_planner", "remediation_executor"],
        "Artifact & Healing": ["artifact_generator", "policy_fixer", "deep_template_healer", "llm_reasoner"],
        "Infrastructure Testing": ["infra_tester", "infra_test_analyzer"],
        "Governance Review": ["ciso_reviewer", "cto_reviewer"],
        "Analysis": ["upgrade_analyst"],
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

    # Fetch performance data (non-blocking best-effort)
    scores = {}
    misses_summary: dict = {}
    feedback_summary: dict = {}
    improvements: list = []
    try:
        from src.database import (
            get_agent_misses, get_agent_feedback_summary,
            get_prompt_improvements,
        )
        # Scores are already embedded in counters (performance_score, etc.)
        for agent_key, ctr in counters.items():
            scores[agent_key] = {
                "performance_score": ctr.get("performance_score", 50),
                "reliability_score": ctr.get("reliability_score", 50),
                "speed_score": ctr.get("speed_score", 50),
                "quality_score": ctr.get("quality_score", 50),
                "total_misses": ctr.get("total_misses", 0),
            }

        # Recent misses per agent (last 5 each)
        all_misses = await get_agent_misses(limit=200)
        for m in all_misses:
            aname = m.get("agent_name", "")
            if aname not in misses_summary:
                misses_summary[aname] = []
            if len(misses_summary[aname]) < 5:
                misses_summary[aname].append({
                    "id": m.get("id"),
                    "miss_type": m.get("miss_type"),
                    "context_summary": (m.get("context_summary") or "")[:200],
                    "error_detail": (m.get("error_detail") or "")[:300],
                    "pipeline_phase": m.get("pipeline_phase"),
                    "resolved": m.get("resolved"),
                    "created_at": m.get("created_at"),
                })

        feedback_summary = await get_agent_feedback_summary()
        improvements = await get_prompt_improvements(status="pending")
    except Exception:
        pass

    return JSONResponse({
        "agents": registry,
        "routing_table": get_routing_table(),
        "counters": counters,
        "activity": activity,
        "scores": scores,
        "misses": misses_summary,
        "feedback_summary": feedback_summary,
        "pending_improvements": [{
            "id": imp.get("id"),
            "agent_name": imp.get("agent_name"),
            "miss_pattern": imp.get("miss_pattern"),
            "miss_count": imp.get("miss_count"),
            "suggested_patch": (imp.get("suggested_patch") or "")[:500],
            "reasoning": (imp.get("reasoning") or "")[:300],
            "created_at": imp.get("created_at"),
        } for imp in improvements],
    })


@router.post("/api/agents/{agent_key}/feedback")
async def submit_agent_feedback(agent_key: str, request: Request):
    """Submit thumbs-up (5) or thumbs-down (1) feedback for an agent."""
    from src.agents import AGENTS
    if agent_key not in AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_key}' not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    rating = body.get("rating")
    if rating not in (1, 5):
        raise HTTPException(status_code=400, detail="rating must be 1 (thumbs-down) or 5 (thumbs-up)")

    from src.database import insert_agent_feedback
    fid = await insert_agent_feedback(
        agent_key,
        rating,
        activity_id=body.get("activity_id"),
        comment=body.get("comment", ""),
    )

    # If thumbs-down, also record as a miss
    if rating == 1:
        from src.copilot_helpers import record_agent_miss
        await record_agent_miss(
            agent_key, "user_downvote",
            context_summary="User gave thumbs-down feedback",
            error_detail=body.get("comment", "")[:500],
        )

    return JSONResponse({"status": "ok", "feedback_id": fid})


@router.get("/api/agents/{agent_key}/misses")
async def get_agent_misses_endpoint(agent_key: str, limit: int = 50):
    """Return recent misses for a specific agent."""
    from src.database import get_agent_misses
    misses = await get_agent_misses(agent_name=agent_key, limit=min(limit, 200))
    return JSONResponse({"agent": agent_key, "misses": misses, "total": len(misses)})


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
                "attempt": live.get("current_attempt", 1) if live else 1,
                "max_attempts": live.get("max_attempts", 5) if live else 5,
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


# ── System Health API ─────────────────────────────────────────

async def _check_sql():
    import time as _time
    try:
        from src.database import get_backend
        t0 = _time.monotonic()
        db = await get_backend()
        rows = await db.execute("SELECT 1 AS ok")
        latency = round((_time.monotonic() - t0) * 1000, 1)
        if rows and rows[0].get("ok") == 1:
            return {"status": "healthy", "latency_ms": latency}
        return {"status": "degraded", "message": "Unexpected query result"}
    except Exception as e:
        return {"status": "unhealthy", "message": str(e)[:300]}

async def _check_backend_api():
    return {"status": "healthy", "latency_ms": 0}

async def _check_entra_id():
    import time as _time
    try:
        configured = is_auth_configured()
        if not configured:
            return {
                "status": "unhealthy",
                "message": "Entra ID not configured (missing ENTRA_CLIENT_ID, ENTRA_TENANT_ID, or ENTRA_CLIENT_SECRET)",
            }
        import urllib.request
        from src.config import ENTRA_AUTHORITY
        t0 = _time.monotonic()
        url = f"{ENTRA_AUTHORITY}/v2.0/.well-known/openid-configuration"
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "InfraForge-HealthCheck/1.0")
        with urllib.request.urlopen(req, timeout=5) as resp:
            latency = round((_time.monotonic() - t0) * 1000, 1)
            if resp.status == 200:
                return {"status": "healthy", "latency_ms": latency}
            return {"status": "degraded", "message": f"HTTP {resp.status}"}
    except Exception as e:
        return {"status": "unhealthy", "message": str(e)[:300]}

async def _check_workiq():
    import time as _time
    try:
        from src.workiq_client import get_workiq_client
        client = get_workiq_client()
        t0 = _time.monotonic()
        available = await client.is_available()
        latency = round((_time.monotonic() - t0) * 1000, 1)
        if available:
            return {"status": "healthy", "latency_ms": latency}
        err = client.get_last_error() or "Work IQ CLI not available"
        return {"status": "unhealthy", "message": err}
    except Exception as e:
        return {"status": "unhealthy", "message": str(e)[:300]}

_HEALTH_CHECKERS = {
    "sql": _check_sql,
    "backend_api": _check_backend_api,
    "entra_id": _check_entra_id,
    "workiq": _check_workiq,
}

def _get_service_meta():
    """Return static endpoint/location metadata for each health service."""
    import re
    from src.config import (
        AZURE_SQL_CONNECTION_STRING, WEB_HOST, WEB_PORT,
        ENTRA_AUTHORITY, DEFAULT_AZURE_REGION,
    )
    # SQL: extract hostname from connection string
    sql_endpoint = "—"
    sql_location = DEFAULT_AZURE_REGION
    m = re.search(r"Server=tcp:([^,;]+)", AZURE_SQL_CONNECTION_STRING, re.IGNORECASE)
    if m:
        sql_endpoint = m.group(1)
        # Try to extract region from hostname (e.g. infraforge-eastus2.database.windows.net)
        parts = sql_endpoint.split(".")[0]  # hostname prefix
        for region in ("eastus2", "eastus", "westus2", "westus3", "westus",
                       "centralus", "northcentralus", "southcentralus",
                       "westeurope", "northeurope", "uksouth", "ukwest",
                       "southeastasia", "eastasia", "japaneast", "japanwest",
                       "australiaeast", "canadacentral", "brazilsouth"):
            if region in parts:
                sql_location = region
                break

    entra_endpoint = ENTRA_AUTHORITY or "Not configured"
    backend_endpoint = f"http://localhost:{WEB_PORT}"

    return {
        "sql":         {"endpoint": sql_endpoint, "location": sql_location},
        "backend_api": {"endpoint": backend_endpoint, "location": "Local"},
        "entra_id":    {"endpoint": entra_endpoint, "location": "Global"},
        "workiq":      {"endpoint": "MCP stdio", "location": "Local"},
    }

@router.get("/api/health")
async def get_system_health(check: str = None):
    """Return connectivity health. Use ?check=sql|backend_api|entra_id|workiq for a single service."""
    if check:
        checker = _HEALTH_CHECKERS.get(check)
        if not checker:
            return JSONResponse({"error": f"Unknown service: {check}"}, status_code=400)
        result = await checker()
        return JSONResponse({"check": check, "result": result})

    results = {}
    for key, fn in _HEALTH_CHECKERS.items():
        results[key] = await fn()

    statuses = [v["status"] for v in results.values()]
    if all(s == "healthy" for s in statuses):
        overall = "healthy"
    elif any(s == "unhealthy" for s in statuses):
        overall = "unhealthy"
    else:
        overall = "degraded"

    return JSONResponse({
        "overall": overall,
        "checks": results,
        "meta": _get_service_meta(),
    })
