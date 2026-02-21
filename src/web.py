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
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from copilot import CopilotClient

from src.config import (
    APP_NAME,
    APP_VERSION,
    APP_DESCRIPTION,
    COPILOT_MODEL,
    COPILOT_LOG_LEVEL,
    OUTPUT_DIR,
    SYSTEM_MESSAGE,
    WEB_HOST,
    WEB_PORT,
    SESSION_SECRET,
    AVAILABLE_MODELS,
    get_active_model,
    set_active_model,
)
from src.tools import get_all_tools
from src.auth import (
    UserContext,
    create_auth_url,
    complete_auth,
    get_pending_session,
    get_user_context,
    invalidate_session,
    create_demo_session,
    is_auth_configured,
)
from src.database import (
    init_db,
    save_session,
    save_chat_message,
    log_usage,
    get_usage_stats,
    cleanup_expired_sessions,
    get_approval_requests,
    update_approval_request,
)
from src.utils import ensure_output_dir
from src.standards import init_standards
from src.standards_api import router as standards_router
from src.model_router import Task, get_model_for_task, get_model_display, get_task_reason, get_routing_table

logger = logging.getLogger("infraforge.web")

# ── Healing loop utilities ───────────────────────────────────

def _summarize_fix(before: str, after: str) -> str:
    """Produce a short summary of what changed between two ARM template strings.

    Used to populate heal_history so the LLM knows what was already tried and
    can avoid repeating the same fix.
    """
    if before == after:
        return "NO CHANGE (fix produced identical output)"
    try:
        b = json.loads(before)
        a = json.loads(after)
    except Exception:
        return f"Template text changed (before: {len(before)} chars → after: {len(after)} chars)"

    changes: list[str] = []

    # Compare resource counts & types
    b_res = b.get("resources", [])
    a_res = a.get("resources", [])
    b_types = sorted({r.get("type", "?") for r in b_res if isinstance(r, dict)})
    a_types = sorted({r.get("type", "?") for r in a_res if isinstance(r, dict)})
    if len(b_res) != len(a_res):
        changes.append(f"resource count: {len(b_res)} → {len(a_res)}")
    removed_types = set(b_types) - set(a_types)
    added_types = set(a_types) - set(b_types)
    if removed_types:
        changes.append(f"removed resources: {', '.join(removed_types)}")
    if added_types:
        changes.append(f"added resources: {', '.join(added_types)}")

    # Compare API versions
    b_apis = {r.get("type", "?"): r.get("apiVersion", "?") for r in b_res if isinstance(r, dict)}
    a_apis = {r.get("type", "?"): r.get("apiVersion", "?") for r in a_res if isinstance(r, dict)}
    for rt in set(b_apis) & set(a_apis):
        if b_apis[rt] != a_apis[rt]:
            changes.append(f"API version for {rt}: {b_apis[rt]} → {a_apis[rt]}")

    # Compare parameters
    b_params = set(b.get("parameters", {}).keys())
    a_params = set(a.get("parameters", {}).keys())
    if b_params != a_params:
        added_p = a_params - b_params
        removed_p = b_params - a_params
        if added_p:
            changes.append(f"added params: {', '.join(added_p)}")
        if removed_p:
            changes.append(f"removed params: {', '.join(removed_p)}")

    if not changes:
        # Fall back to size comparison
        changes.append(f"template modified (size: {len(before)} → {len(after)} chars)")

    return "; ".join(changes[:5])


_PARAM_DEFAULTS: dict[str, object] = {
    "resourceName": "infraforge-resource",
    "location": "[resourceGroup().location]",
    "environment": "dev",
    "projectName": "infraforge",
    "ownerEmail": "platform-team@company.com",
    "costCenter": "IT-0001",
}


def _ensure_parameter_defaults(template_json: str) -> str:
    """Ensure every parameter in an ARM template has a defaultValue.

    Deployed templates are sent with ``parameters={}``, so any parameter
    without a ``defaultValue`` causes:
        "The value for the template parameter 'X' is not provided."

    This function injects sensible defaults for well-known params and a
    generic placeholder for anything else.  Returns the (possibly
    modified) JSON string.
    """
    try:
        tmpl = json.loads(template_json)
    except (json.JSONDecodeError, TypeError):
        return template_json  # can't fix what we can't parse

    params = tmpl.get("parameters")
    if not params or not isinstance(params, dict):
        return template_json

    patched = False
    for pname, pdef in params.items():
        if not isinstance(pdef, dict):
            continue
        if "defaultValue" not in pdef:
            pdef["defaultValue"] = _PARAM_DEFAULTS.get(pname, f"infraforge-{pname}")
            patched = True

    if patched:
        patched_names = [p for p in params if "defaultValue" in params[p]]
        logger.info("Injected missing defaultValues for params: %s", patched_names)
        return json.dumps(tmpl, indent=2)
    return template_json


def _version_to_semver(version_int: int) -> str:
    """Convert an integer version number to semver format.

    Scheme:  version N → N.0.0
    Each new onboarding creates a new major version (the template is
    regenerated from scratch). The heal loop updates in-place and doesn't
    create new versions, so minor/patch stay at 0.
    """
    return f"{version_int}.0.0"


def _stamp_template_metadata(
    template_json: str,
    *,
    service_id: str,
    version_int: int,
    gen_source: str = "unknown",
    region: str = "eastus2",
) -> str:
    """Embed InfraForge provenance metadata into an ARM template.

    Adds a top-level ``metadata`` property (ARM supports this for any
    template) and updates ``contentVersion`` to the semver string.
    This makes every template self-describing — version, origin, and
    content hash travel with the template even outside the database.

    ARM ignores the ``metadata`` property during deployment, so this
    is safe to include.
    """
    import hashlib
    from datetime import datetime, timezone

    try:
        tmpl = json.loads(template_json)
    except (json.JSONDecodeError, TypeError):
        return template_json

    semver = _version_to_semver(version_int)

    # Update contentVersion to match our semver
    tmpl["contentVersion"] = semver

    # Compute a content hash of the resources section (stable fingerprint)
    resources_str = json.dumps(tmpl.get("resources", []), sort_keys=True)
    content_hash = hashlib.sha256(resources_str.encode()).hexdigest()[:12]

    tmpl["metadata"] = {
        "_generator": {
            "name": "InfraForge",
            "version": semver,
            "templateHash": content_hash,
        },
        "infrapiForge": {
            "serviceId": service_id,
            "version": version_int,
            "semver": semver,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "generatedBy": gen_source,
            "region": region,
            "platform": "InfraForge Self-Service Infrastructure",
        },
    }

    return json.dumps(tmpl, indent=2)


def _extract_param_values(template: dict) -> dict:
    """Extract explicit parameter values from a template's defaultValues.

    ARM *should* use ``defaultValue`` when ``parameters={}`` is passed, but
    in practice the validate/deploy endpoints sometimes reject templates
    with required parameters even when defaults are defined.  By extracting
    the defaults and passing them as explicit values, we guarantee ARM never
    complains about missing parameters.

    Skips ``location`` because ARM expressions like
    ``[resourceGroup().location]`` cannot be provided as a literal value —
    they only work as defaultValues inside the template.
    """
    params = template.get("parameters", {})
    values: dict[str, object] = {}
    for pname, pdef in params.items():
        if not isinstance(pdef, dict):
            continue
        dv = pdef.get("defaultValue")
        if dv is None:
            # No default — provide one from our well-known list
            dv = _PARAM_DEFAULTS.get(pname)
        if dv is None:
            dv = f"infraforge-{pname}"
        # Skip ARM expressions — they only work inside the template, not as
        # explicit parameter values.
        if isinstance(dv, str) and dv.startswith("["):
            continue
        values[pname] = dv
    return values


# ── Global state ─────────────────────────────────────────────
copilot_client: Optional[CopilotClient] = None
_copilot_init_lock = asyncio.Lock()


async def ensure_copilot_client() -> Optional[CopilotClient]:
    """Lazily initialize the Copilot SDK client on first use.

    Returns the client, or None if initialization fails.
    """
    global copilot_client
    if copilot_client is not None:
        return copilot_client
    async with _copilot_init_lock:
        if copilot_client is not None:
            return copilot_client
        try:
            logger.info("Lazy-initializing Copilot SDK client...")
            copilot_client = CopilotClient({"log_level": COPILOT_LOG_LEVEL})
            await copilot_client.start()
            logger.info("Copilot SDK client started successfully")
            return copilot_client
        except Exception as e:
            logger.error(f"Copilot SDK failed to start: {e}")
            copilot_client = None
            return None


# Track active Copilot sessions: session_token → { copilot_session, user_context }
# (Chat history and usage analytics are persisted in the database)
active_sessions: dict[str, dict] = {}

# ── Active validation job tracker (in-memory) ────────────────
# service_id → { status, service_name, started_at, updated_at, phase, attempt,
#                max_attempts, progress, events: [dict], error?, rg_name? }
_active_validations: dict[str, dict] = {}


def _user_context_to_dict(user: UserContext) -> dict:
    """Convert UserContext to a dict for database persistence."""
    return {
        "user_id": user.user_id,
        "display_name": user.display_name,
        "email": user.email,
        "job_title": user.job_title,
        "department": user.department,
        "cost_center": user.cost_center,
        "manager": user.manager,
        "groups": user.groups,
        "roles": user.roles,
        "team": user.team,
        "is_platform_team": user.is_platform_team,
        "is_admin": user.is_admin,
    }


# ── Lifespan ─────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start/stop the Copilot SDK client with the server lifecycle."""
    global copilot_client
    logger.info("Initializing database...")
    await init_db()
    await cleanup_expired_sessions()
    logger.info("Initializing organization standards...")
    await init_standards()
    logger.info("Deferring Copilot SDK client start (lazy init on first chat)...")
    copilot_client = None  # Will be started lazily on first WebSocket connection
    ensure_output_dir(OUTPUT_DIR)

    # Azure resource provider sync — runs on-demand via the Sync button.
    # Removed from startup to avoid blocking or crashing the server.

    logger.info("InfraForge web server ready")
    yield
    logger.info("Shutting down Copilot SDK client...")
    # Clean up active sessions
    for session_data in active_sessions.values():
        try:
            await session_data["copilot_session"].destroy()
        except Exception:
            pass
    if copilot_client:
        try:
            await copilot_client.stop()
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

# Serve static files (HTML, CSS, JS)
static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ── Auth Endpoints ───────────────────────────────────────────

@app.get("/")
async def root():
    """Serve the main page."""
    index_path = os.path.join(static_dir, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/onboarding-docs")
async def onboarding_docs():
    """Serve the onboarding pipeline documentation page."""
    docs_path = os.path.join(static_dir, "onboarding-docs.html")
    with open(docs_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/api/auth/config")
async def auth_config():
    """Return auth configuration for the frontend MSAL.js client."""
    from src.config import ENTRA_CLIENT_ID, ENTRA_TENANT_ID, ENTRA_REDIRECT_URI

    return JSONResponse({
        "configured": is_auth_configured(),
        "clientId": ENTRA_CLIENT_ID,
        "tenantId": ENTRA_TENANT_ID,
        "redirectUri": ENTRA_REDIRECT_URI,
    })


@app.get("/api/auth/login")
async def login():
    """Initiate the Entra ID login flow."""
    if not is_auth_configured():
        # Demo mode — create a demo session and persist to DB
        session_token, user = create_demo_session()
        pending = get_pending_session(session_token)
        if pending:
            await save_session(session_token, _user_context_to_dict(user), "demo-token")
        return JSONResponse({
            "mode": "demo",
            "sessionToken": session_token,
            "user": {
                "displayName": user.display_name,
                "email": user.email,
                "jobTitle": user.job_title,
                "department": user.department,
                "costCenter": user.cost_center,
                "team": user.team,
                "isAdmin": user.is_admin,
                "isPlatformTeam": user.is_platform_team,
            },
        })

    auth_url, flow_id = create_auth_url()
    return JSONResponse({
        "mode": "entra",
        "authUrl": auth_url,
        "flowId": flow_id,
    })


@app.get("/api/auth/callback")
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


@app.post("/api/auth/demo")
async def demo_login():
    """Create a demo session (when Entra ID is not configured)."""
    session_token, user = create_demo_session()
    pending = get_pending_session(session_token)
    if pending:
        await save_session(session_token, _user_context_to_dict(user), "demo-token")
    return JSONResponse({
        "sessionToken": session_token,
        "user": {
            "displayName": user.display_name,
            "email": user.email,
            "jobTitle": user.job_title,
            "department": user.department,
            "costCenter": user.cost_center,
            "team": user.team,
            "isAdmin": user.is_admin,
            "isPlatformTeam": user.is_platform_team,
        },
    })


@app.post("/api/auth/logout")
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


@app.get("/api/auth/me")
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

@app.get("/api/settings/model")
async def get_model_settings():
    """Return the current active LLM model and all available models."""
    active = get_active_model()
    return JSONResponse({
        "active_model": active,
        "available_models": AVAILABLE_MODELS,
    })


@app.get("/api/settings/model-routing")
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


@app.put("/api/settings/model")
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


# ── Usage Analytics (Work IQ) ────────────────────────────────

@app.get("/api/analytics/usage")
async def get_usage_analytics(request: Request):
    """Return usage analytics for the Work IQ dashboard.

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

@app.get("/api/activity")
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

        # Include services that are validating, validation_failed, or recently approved
        if status in ("validating", "validation_failed", "approved"):
            # Check if there's a live tracker entry
            live = _active_validations.get(svc_id)

            job = {
                "service_id": svc_id,
                "service_name": svc.get("name", svc_id),
                "category": svc.get("category", ""),
                "status": status,
                "is_running": live is not None and live.get("status") == "running",
                "phase": live.get("phase", "") if live else "",
                "detail": live.get("detail", "") if live else "",
                "attempt": live.get("attempt", 0) if live else 0,
                "max_attempts": live.get("max_attempts", 5) if live else 5,
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


# ── Service Catalog API ──────────────────────────────────────

@app.get("/api/catalog/services")
async def get_service_catalog():
    """Return the approved Azure services catalog from the database.

    This powers the interactive service browser in the welcome screen,
    letting users see at a glance which services are approved, conditional,
    under review, or not yet approved.
    """
    from src.database import get_all_services

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
    from src.database import get_all_templates

    try:
        templates = await get_all_templates(
            category=category, fmt=fmt, template_type=template_type,
        )
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
    from src.database import upsert_service, get_service

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

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
    from src.database import get_service, upsert_service

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Fetch the existing service
    existing = await get_service(service_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

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
    from src.database import upsert_template, get_template_by_id

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

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
        from src.database import get_all_services, get_active_service_version, get_service_versions
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
    from src.database import (
        get_service, get_active_service_version, get_service_version, upsert_template,
    )
    from src.tools.arm_generator import generate_arm_template, has_builtin_skeleton
    import json as _json

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

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

    for sel in selections:
        sid = sel.get("service_id", "")
        qty = max(1, int(sel.get("quantity", 1)))
        chosen_params = set(sel.get("parameters", []))
        chosen_version = sel.get("version")  # None means "use active/latest"

        svc = await get_service(sid)
        if not svc:
            raise HTTPException(status_code=404, detail=f"Service '{sid}' not found")
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
                except Exception:
                    pass
        if not tpl_dict and has_builtin_skeleton(sid):
            tpl_dict = generate_arm_template(sid)
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

    # ── Compose the combined ARM template ─────────────────────
    from src.tools.arm_generator import _STANDARD_PARAMETERS, _TEMPLATE_WRAPPER, _STANDARD_TAGS

    combined_params = dict(_STANDARD_PARAMETERS)
    combined_resources = []
    combined_outputs = {}
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

        src_params = tpl.get("parameters", {})
        src_resources = tpl.get("resources", [])
        src_outputs = tpl.get("outputs", {})

        for idx in range(1, qty + 1):
            suffix = f"_{short_name}" if qty == 1 else f"_{short_name}{idx}"

            # Add a resourceName parameter for this instance
            instance_name_param = f"resourceName{suffix}"
            combined_params[instance_name_param] = {
                "type": "string",
                "metadata": {
                    "description": f"Name for {svc.get('name', sid)}"
                    + (f" (instance {idx})" if qty > 1 else ""),
                },
            }

            # Add chosen extra parameters (with suffix to avoid collisions)
            for pname in chosen:
                if pname in STANDARD_PARAMS:
                    continue
                pdef = src_params.get(pname)
                if not pdef:
                    continue
                suffixed = f"{pname}{suffix}"
                combined_params[suffixed] = dict(pdef)
                meta = combined_params[suffixed].setdefault("metadata", {})
                if qty > 1:
                    meta["description"] = meta.get("description", pname) + f" (instance {idx})"

            # Clone resources, replacing parameter references
            for res in src_resources:
                cloned = _json.loads(_json.dumps(res))
                # Replace [parameters('resourceName')] with instance param
                res_str = _json.dumps(cloned)
                res_str = res_str.replace(
                    "[parameters('resourceName')]",
                    f"[parameters('{instance_name_param}')]",
                )
                # Replace chosen extra param references
                for pname in chosen:
                    if pname in STANDARD_PARAMS:
                        continue
                    suffixed = f"{pname}{suffix}"
                    res_str = res_str.replace(
                        f"[parameters('{pname}')]",
                        f"[parameters('{suffixed}')]",
                    )
                combined_resources.append(_json.loads(res_str))

            # Clone outputs with suffixed names
            for oname, odef in src_outputs.items():
                out_name = f"{oname}{suffix}"
                out_val = _json.dumps(odef)
                out_val = out_val.replace(
                    "[parameters('resourceName')]",
                    f"[parameters('{instance_name_param}')]",
                )
                combined_outputs[out_name] = _json.loads(out_val)

    # Build the final composed template
    composed = dict(_TEMPLATE_WRAPPER)
    composed["parameters"] = combined_params
    composed["variables"] = {}
    composed["resources"] = combined_resources
    composed["outputs"] = combined_outputs

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
        "status": "approved",
        "registered_by": "template-composer",
        # Dependency metadata
        "template_type": dep_analysis["template_type"],
        "provides": dep_analysis["provides"],
        "requires": dep_analysis["requires"],
        "optional_refs": dep_analysis["optional_refs"],
    }

    try:
        await upsert_template(catalog_entry)
    except Exception as e:
        logger.error(f"Failed to save composed template: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({
        "status": "ok",
        "template_id": template_id,
        "template": catalog_entry,
        "resource_count": len(combined_resources),
        "parameter_count": len(combined_params),
        "dependency_analysis": dep_analysis,
    })


@app.delete("/api/catalog/services/{service_id}")
async def delete_service_endpoint(service_id: str):
    """Remove a service from the catalog."""
    from src.database import get_backend

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
    from src.database import get_all_services

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


@app.delete("/api/catalog/templates/{template_id}")
async def delete_template_endpoint(template_id: str):
    """Remove a template from the catalog."""
    from src.database import delete_template

    deleted = await delete_template(template_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Template not found")
    return JSONResponse({"status": "ok", "deleted": template_id})


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


@app.post("/api/templates/analyze-dependencies")
async def analyze_template_dependencies(request: Request):
    """Analyze dependencies for a set of service IDs.

    Body: { "service_ids": ["Microsoft.Compute/virtualMachines", ...] }

    Returns: template_type, provides, requires, optional_refs, auto_created,
    and whether the template is deployable_standalone.
    """
    from src.template_engine import analyze_dependencies

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

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
    from src.database import get_service_artifacts, get_service

    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    artifacts = await get_service_artifacts(service_id)
    return JSONResponse(artifacts)


@app.put("/api/services/{service_id:path}/artifacts/{artifact_type}")
async def save_artifact_endpoint(service_id: str, artifact_type: str, request: Request):
    """Save or update an artifact (policy, template, or pipeline) for a service."""
    from src.database import save_service_artifact, get_service, ARTIFACT_TYPES

    if artifact_type not in ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid artifact type. Must be one of: {', '.join(ARTIFACT_TYPES)}",
        )

    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

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
    from src.database import approve_service_artifact, get_service, ARTIFACT_TYPES

    if artifact_type not in ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid artifact type. Must be one of: {', '.join(ARTIFACT_TYPES)}",
        )

    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    try:
        body = await request.json()
    except Exception:
        body = {}

    approved_by = body.get("approved_by", "IT Staff")

    try:
        artifact = await approve_service_artifact(
            service_id=service_id,
            artifact_type=artifact_type,
            approved_by=approved_by,
        )

        # Check if all gates are now approved → validation required
        from src.database import get_service_artifacts
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
    from src.database import unapprove_service_artifact, ARTIFACT_TYPES

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
    from src.database import (
        get_service, get_service_artifacts, save_service_artifact,
        promote_service_after_validation, fail_service_validation,
    )

    MAX_HEAL_ATTEMPTS = 5

    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    artifacts = await get_service_artifacts(service_id)
    if not artifacts["_summary"]["all_approved"]:
        raise HTTPException(status_code=400, detail="Both gates must be approved before validation")

    template_artifact = artifacts.get("template", {})
    template_content = template_artifact.get("content", "").strip()
    if not template_content:
        raise HTTPException(status_code=400, detail="ARM template artifact has no content")

    try:
        body = await request.json()
    except Exception:
        body = {}

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

            # Previous attempt history
            if previous_attempts:
                prompt += "--- PREVIOUS FAILED ATTEMPTS (DO NOT repeat these fixes) ---\n"
                for pa in previous_attempts:
                    prompt += (
                        f"Attempt {pa['attempt']}: Error was: {pa['error'][:300]}\n"
                        f"  Fix tried: {pa['fix_summary']}\n"
                        f"  Result: STILL FAILED — do something DIFFERENT\n\n"
                    )
                prompt += "--- END PREVIOUS ATTEMPTS ---\n\n"

            prompt += (
                "Fix the template so it deploys successfully. Return ONLY the "
                "corrected raw JSON — no markdown fences, no explanation.\n\n"
                "CRITICAL RULES:\n"
                "- Keep ALL location parameters as \"[resourceGroup().location]\" or "
                "\"[parameters('location')]\" — NEVER hardcode a region like 'centralus', "
                "'eastus2', etc.\n"
                "- Keep the same resource intent and resource names.\n"
                "- Fix schema issues, missing required properties, invalid API versions, "
                "and structural problems.\n"
                "- If a resource like diagnosticSettings requires an external dependency "
                "(Log Analytics workspace, storage account), REMOVE it rather than adding "
                "a fake dependency.\n"
                "- Ensure EVERY parameter has a \"defaultValue\". This template is deployed "
                "with parameters={}, so any parameter without a default will cause: "
                "'The value for the template parameter ... is not provided'. If a parameter "
                "is missing a default, ADD one (e.g. resourceName \u2192 \"infraforge-resource\").\n"
            )

            # Escalation strategies for later attempts
            if attempt_num >= 4:
                prompt += (
                    f"\n\nESCALATION (attempt {attempt_num}/5 — drastic measures needed):\n"
                    "- SIMPLIFY the template: remove optional/nice-to-have resources\n"
                    "- Remove diagnosticSettings, locks, autoscale rules if causing issues\n"
                    "- Use the SIMPLEST valid configuration for each resource\n"
                    "- Strip down to ONLY the primary resource with minimal properties\n"
                    "- Use well-known, stable API versions (prefer 2023-xx-xx or 2024-xx-xx)\n"
                )
            elif attempt_num >= 2:
                prompt += (
                    f"\n\nThis is attempt {attempt_num}/5. The previous fix(es) did NOT work.\n"
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

        session = None
        try:
            _client = await ensure_copilot_client()
            if _client is None:
                raise RuntimeError("Copilot SDK not available")
            session = await _client.create_session({
                "model": get_model_for_task(Task.CODE_FIXING), "streaming": True, "tools": [],
                "system_message": {"content": (
                    "You are an Azure infrastructure expert. "
                    "Return ONLY raw JSON — no markdown, no code fences."
                )},
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

            fixed = "".join(chunks).strip()
            if fixed.startswith("```"):
                lines = fixed.split("\n")[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                fixed = "\n".join(lines).strip()

            # ── Guard: ensure healer didn't corrupt the location parameter ──
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
                        _rloc = _res.get("location", "")
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

            return fixed
        finally:
            if session:
                try:
                    await session.destroy()
                except Exception:
                    pass

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
                "attempt": 0,
                "max_attempts": MAX_HEAL_ATTEMPTS,
                "progress": 0,
                "rg_name": rg_name,
                "events": [],
                "error": "",
            }
            _active_validations[service_id] = tracker
        tracker["updated_at"] = now
        if evt.get("phase"):
            tracker["phase"] = evt["phase"]
        if evt.get("attempt"):
            tracker["attempt"] = evt["attempt"]
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

        # ── Extract template metadata for verbose display ─────
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
            "attempt": 0,
            "max_attempts": MAX_HEAL_ATTEMPTS,
            "progress": 0,
            "rg_name": rg_name,
            "region": region,
            "subscription": _sub_id,
            "template_meta": tmpl_meta,
            "steps_completed": [],
            "events": [],
            "error": "",
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
                "max_attempts": MAX_HEAL_ATTEMPTS,
                "has_policy": bool((artifacts.get("policy", {}).get("content") or "").strip()),
            },
        }) + "\n"

        try:
            for attempt in range(1, MAX_HEAL_ATTEMPTS + 1):
                is_last = attempt == MAX_HEAL_ATTEMPTS
                att_base = (attempt - 1) / MAX_HEAL_ATTEMPTS

                yield json.dumps({
                    "type": "iteration_start",
                    "attempt": attempt,
                    "max_attempts": MAX_HEAL_ATTEMPTS,
                    "detail": f"Attempt {attempt}/{MAX_HEAL_ATTEMPTS} — Parsing and validating ARM template JSON ({tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s): {', '.join(tmpl_meta['resource_types'][:3]) or 'unknown'})…",
                    "progress": att_base + 0.01,
                }) + "\n"

                # ── 1. Parse JSON ─────────────────────────────
                try:
                    template_json = json.loads(current_template)
                except json.JSONDecodeError as e:
                    error_msg = f"ARM template is not valid JSON — parse error at line {e.lineno}, col {e.colno}: {e.msg}"
                    if is_last:
                        await fail_service_validation(service_id, error_msg)
                        yield json.dumps({"type": "error", "phase": "parsing", "attempt": attempt, "detail": error_msg}) + "\n"
                        return
                    yield json.dumps({"type": "healing", "phase": "fixing_template", "attempt": attempt, "detail": f"JSON parse error at line {e.lineno}, col {e.colno}: {e.msg} — invoking Copilot SDK to auto-heal (attempt {attempt}, {len(heal_history)} prior fixes tried)…", "error": error_msg, "progress": att_base + 0.02}) + "\n"
                    _pre_fix = current_template
                    current_template = await _copilot_fix("template", current_template, error_msg, previous_attempts=heal_history)
                    heal_history.append({"attempt": attempt, "phase": "parsing", "error": error_msg, "fix_summary": _summarize_fix(_pre_fix, current_template)})
                    tmpl_meta = _extract_template_meta(current_template)
                    await save_service_artifact(service_id, "template", content=current_template, status="approved", notes=f"Auto-healed (attempt {attempt}): JSON parse error")
                    yield json.dumps({"type": "healing_done", "phase": "template_fixed", "attempt": attempt, "detail": f"Copilot SDK rewrote template (now {tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s)) — retrying validation…", "progress": att_base + 0.03}) + "\n"
                    continue

                # ── 2. What-If ────────────────────────────────
                res_types_str = ", ".join(tmpl_meta["resource_types"][:5]) or "unknown"
                yield json.dumps({
                    "type": "progress", "phase": "what_if", "attempt": attempt,
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
                        yield json.dumps({"type": "progress", "phase": "infra_retry", "attempt": attempt,
                            "detail": f"Transient Azure infrastructure error (not a template problem, won't count as a heal attempt) — waiting 10s before retry. Error: {errors[:200]}",
                            "progress": att_base + 0.05}) + "\n"
                        await asyncio.sleep(10)
                        continue

                    if is_last:
                        await fail_service_validation(service_id, f"What-If failed after {MAX_HEAL_ATTEMPTS} attempts: {errors}")
                        yield json.dumps({"type": "error", "phase": "what_if", "attempt": attempt, "detail": f"What-If analysis rejected by Azure Resource Manager after {MAX_HEAL_ATTEMPTS} auto-heal attempts. Error: {errors}"}) + "\n"
                        return
                    yield json.dumps({"type": "healing", "phase": "fixing_template", "attempt": attempt, "detail": f"What-If rejected by ARM — invoking Copilot SDK to auto-heal (attempt {attempt}, {len(heal_history)} prior fixes tried). Error: {errors[:300]}", "error": errors, "progress": att_base + 0.05}) + "\n"
                    _pre_fix = current_template
                    current_template = await _copilot_fix("template", current_template, errors, previous_attempts=heal_history)
                    heal_history.append({"attempt": attempt, "phase": "what_if", "error": errors[:500], "fix_summary": _summarize_fix(_pre_fix, current_template)})
                    tmpl_meta = _extract_template_meta(current_template)
                    await save_service_artifact(service_id, "template", content=current_template, status="approved", notes=f"Auto-healed (attempt {attempt}): {errors[:200]}")
                    yield json.dumps({"type": "healing_done", "phase": "template_fixed", "attempt": attempt, "detail": f"Copilot SDK rewrote template (now {tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s): {', '.join(tmpl_meta['resource_types'][:3])}) — restarting validation pipeline…", "progress": att_base + 0.07}) + "\n"
                    continue

                change_summary = ", ".join(f"{v} {k}" for k, v in wif.get("change_counts", {}).items())
                # Build per-resource details for verbose display
                change_details = []
                for ch in wif.get("changes", [])[:10]:
                    change_details.append(f"{ch.get('change_type','?')}: {ch.get('resource_type','?')}/{ch.get('resource_name','?')}")
                change_detail_str = "; ".join(change_details) if change_details else "no resource-level changes"
                yield json.dumps({
                    "type": "progress", "phase": "what_if_complete", "attempt": attempt,
                    "detail": f"✓ What-If analysis passed — ARM accepted the template. Changes: {change_summary or 'no changes detected'}. Resources: {change_detail_str}",
                    "progress": att_base + 0.06,
                    "result": wif,
                }) + "\n"

                # ── 3. Actual Deploy ──────────────────────────
                yield json.dumps({
                    "type": "progress", "phase": "deploying", "attempt": attempt,
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

                    yield json.dumps({
                        "type": "progress", "phase": "deploy_failed", "attempt": attempt,
                        "detail": f"ARM deployment 'validate-{attempt}' failed in resource group '{rg_name}' ({region}). Error from Azure: {deploy_error[:400]}",
                        "progress": att_base + 0.12,
                    }) + "\n"

                    if _is_infra_deploy:
                        yield json.dumps({"type": "progress", "phase": "infra_retry", "attempt": attempt,
                            "detail": f"Transient Azure infrastructure error (not a template problem, won't count as a heal attempt) — waiting 10s before retrying into the same RG. Error: {deploy_error[:200]}",
                            "progress": att_base + 0.13}) + "\n"
                        await asyncio.sleep(10)
                        continue

                    if is_last:
                        await _cleanup_rg(rg_name)
                        await fail_service_validation(service_id, f"Deploy failed after {MAX_HEAL_ATTEMPTS} attempts: {deploy_error}")
                        yield json.dumps({"type": "error", "phase": "deploy", "attempt": attempt, "detail": f"Deployment failed after {MAX_HEAL_ATTEMPTS} auto-heal attempts. Final error from Azure: {deploy_error}"}) + "\n"
                        return

                    yield json.dumps({"type": "healing", "phase": "fixing_template", "attempt": attempt, "detail": f"Deployment rejected by Azure — invoking Copilot SDK to auto-heal (attempt {attempt}, {len(heal_history)} prior fixes tried). Error: {deploy_error[:300]}", "error": deploy_error, "progress": att_base + 0.13}) + "\n"
                    _pre_fix = current_template
                    current_template = await _copilot_fix("template", current_template, deploy_error, previous_attempts=heal_history)
                    heal_history.append({"attempt": attempt, "phase": "deploy", "error": deploy_error[:500], "fix_summary": _summarize_fix(_pre_fix, current_template)})
                    tmpl_meta = _extract_template_meta(current_template)
                    await save_service_artifact(service_id, "template", content=current_template, status="approved", notes=f"Auto-healed (attempt {attempt}): deploy error — {deploy_error[:200]}")
                    yield json.dumps({"type": "healing_done", "phase": "template_fixed", "attempt": attempt, "detail": f"Copilot SDK rewrote template (now {tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s): {', '.join(tmpl_meta['resource_types'][:3])}) — redeploying into same RG (incremental mode)…", "progress": att_base + 0.15}) + "\n"
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
                    "type": "progress", "phase": "deploy_complete", "attempt": attempt,
                    "detail": f"✓ ARM deployment 'validate-{attempt}' succeeded — {len(provisioned)} resource(s) provisioned in '{rg_name}': {'; '.join(resource_summaries[:5]) or 'none'}",
                    "progress": att_base + 0.12,
                    "resources": provisioned,
                }) + "\n"

                # ── 4. Verify resources exist ─────────────────
                yield json.dumps({
                    "type": "progress", "phase": "resource_check", "attempt": attempt,
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
                        "type": "progress", "phase": "resource_check_complete", "attempt": attempt,
                        "detail": f"✓ Verified {len(resource_details)} live resource(s) in Azure: {'; '.join(res_detail_strs)}",
                        "progress": att_base + 0.14,
                        "resources": [{"name": r["name"], "type": r["type"], "location": r["location"]} for r in resource_details],
                    }) + "\n"

                except Exception as e:
                    logger.warning(f"Resource check failed: {e}")
                    resource_details = []
                    yield json.dumps({
                        "type": "progress", "phase": "resource_check_warning", "attempt": attempt,
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
                        "type": "progress", "phase": "policy_testing", "attempt": attempt,
                        "detail": f"Evaluating {len(resource_details)} deployed resource(s) against organization policy ({_policy_size} KB, {_rule_count} rule(s)). Checking tags, SKUs, locations, networking, and security configurations…",
                        "progress": att_base + 0.15,
                    }) + "\n"

                    try:
                        policy_json = json.loads(policy_content)
                    except json.JSONDecodeError as pe:
                        # Auto-heal policy if invalid
                        if not is_last:
                            yield json.dumps({"type": "healing", "phase": "fixing_policy", "attempt": attempt, "detail": f"Policy JSON error — asking AI to fix…", "error": str(pe), "progress": att_base + 0.155}) + "\n"
                            fixed_policy = await _copilot_fix("policy", policy_content, str(pe))
                            await save_service_artifact(service_id, "policy", content=fixed_policy, status="approved", notes=f"Auto-healed (attempt {attempt}): policy JSON error")
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
                            yield json.dumps({"type": "error", "phase": "policy", "attempt": attempt, "detail": f"Policy JSON invalid: {pe}"}) + "\n"
                            return

                    policy_results = _test_policy_compliance(policy_json, resource_details)
                    all_compliant = all(r["compliant"] for r in policy_results)
                    compliant_count = sum(1 for r in policy_results if r["compliant"])

                    for pr in policy_results:
                        icon = "✅" if pr["compliant"] else "❌"
                        yield json.dumps({
                            "type": "policy_result", "phase": "policy_testing", "attempt": attempt,
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
                            "type": "progress", "phase": "policy_failed", "attempt": attempt,
                            "detail": fail_msg,
                            "progress": att_base + 0.17,
                        }) + "\n"

                        if is_last:
                            await _cleanup_rg(rg_name)
                            await fail_service_validation(service_id, fail_msg)
                            yield json.dumps({"type": "error", "phase": "policy", "attempt": attempt, "detail": f"Policy compliance failed after {MAX_HEAL_ATTEMPTS} auto-heal attempts. Violations: {violation_desc}"}) + "\n"
                            return

                        fix_error = f"Policy violation: {violation_desc}. The policy requires: {policy_content[:500]}"
                        yield json.dumps({"type": "healing", "phase": "fixing_template", "attempt": attempt, "detail": f"Policy violations on {len(violations)} resource(s) — invoking Copilot SDK to auto-heal (attempt {attempt}, {len(heal_history)} prior fixes tried). Violations: {violation_desc[:300]}", "error": fix_error, "progress": att_base + 0.175}) + "\n"
                        _pre_fix = current_template
                        current_template = await _copilot_fix("template", current_template, fix_error, previous_attempts=heal_history)
                        heal_history.append({"attempt": attempt, "phase": "policy_compliance", "error": fix_error[:500], "fix_summary": _summarize_fix(_pre_fix, current_template)})
                        tmpl_meta = _extract_template_meta(current_template)
                        await save_service_artifact(service_id, "template", content=current_template, status="approved", notes=f"Auto-healed (attempt {attempt}): policy violation")
                        yield json.dumps({"type": "healing_done", "phase": "template_fixed", "attempt": attempt, "detail": f"Copilot SDK rewrote template for policy compliance (now {tmpl_meta['size_kb']} KB) — redeploying into same RG and re-testing…", "progress": att_base + 0.18}) + "\n"
                        # Don't cleanup — redeploy into the same RG (incremental mode)
                        continue
                else:
                    yield json.dumps({
                        "type": "progress", "phase": "policy_skip", "attempt": attempt,
                        "detail": "No policy content or no resources to test — skipping policy check",
                        "progress": att_base + 0.16,
                    }) + "\n"

                # ── 6. Cleanup validation RG ──────────────────
                yield json.dumps({
                    "type": "progress", "phase": "cleanup", "attempt": attempt,
                    "detail": f"All checks passed — initiating deletion of validation resource group '{rg_name}' and all {len(resource_details)} resource(s) within it. This is fire-and-forget; Azure will complete deletion asynchronously.",
                    "progress": 0.90,
                }) + "\n"

                await _cleanup_rg(rg_name)
                deployed_rg = None

                yield json.dumps({
                    "type": "progress", "phase": "cleanup_complete", "attempt": attempt,
                    "detail": f"✓ Resource group '{rg_name}' deletion initiated — Azure will remove all validation resources in the background",
                    "progress": 0.93,
                }) + "\n"

                # ── 7. Promote ────────────────────────────────
                validation_summary = {
                    "what_if": wif,
                    "deployed_resources": [{"name": r["name"], "type": r["type"], "location": r["location"]} for r in resource_details],
                    "policy_compliance": policy_results,
                    "all_compliant": all(r["compliant"] for r in policy_results) if policy_results else True,
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
                    "type": "progress", "phase": "promoting", "attempt": attempt,
                    "detail": f"All validation gates passed — promoting {svc['name']} ({service_id}) from 'validating' → 'approved' in the service catalog…",
                    "progress": 0.97,
                }) + "\n"

                await promote_service_after_validation(service_id, validation_summary)

                compliant_str = f", all {len(policy_results)} policy check(s) passed" if policy_results else ""
                res_types_done = ", ".join(tmpl_meta["resource_types"][:5]) or "N/A"
                yield json.dumps({
                    "type": "done", "phase": "approved", "attempt": attempt,
                    "total_attempts": attempt,
                    "detail": f"🎉 {svc['name']} approved! Successfully deployed {len(resource_details)} resource(s) [{res_types_done}] to Azure{compliant_str}. Validation resource group cleaned up.{'' if attempt == 1 else f' Required {attempt} attempt(s) with Copilot SDK auto-healing.'}",
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
            yield json.dumps({"type": "error", "phase": "unknown", "detail": str(e)}) + "\n"
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
    from src.database import get_service, ARTIFACT_TYPES

    if artifact_type not in ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid artifact type. Must be one of: {', '.join(ARTIFACT_TYPES)}",
        )

    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

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

        session = None
        try:
            # Create a temporary Copilot session for this generation
            _client = await ensure_copilot_client()
            if _client is None:
                raise RuntimeError("Copilot SDK not available")
            session = await _client.create_session({
                "model": _artifact_model,
                "streaming": True,
                "tools": [],  # No tools needed for pure generation
                "system_message": {
                    "content": (
                        "You are an Azure infrastructure expert. "
                        "Generate production-ready infrastructure artifacts. "
                        "Return ONLY the raw code/configuration — no markdown, "
                        "no explanation text, no code fences."
                    )
                },
            })

            response_chunks: list[str] = []
            done_event = asyncio.Event()

            def on_event(event):
                try:
                    if event.type.value == "assistant.message_delta":
                        delta = event.data.delta_content or ""
                        response_chunks.append(delta)
                    elif event.type.value in ("assistant.message", "session.idle"):
                        done_event.set()
                except Exception as e:
                    logger.error(f"Generation event error: {e}")
                    done_event.set()

            unsubscribe = session.on(on_event)

            try:
                await session.send({"prompt": generation_prompt})
                await asyncio.wait_for(done_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                yield json.dumps({"type": "error", "message": "Generation timed out"}) + "\n"
                return
            finally:
                unsubscribe()

            full_content = "".join(response_chunks).strip()

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

        except Exception as e:
            logger.error(f"Artifact generation failed: {e}")
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"
        finally:
            if session:
                try:
                    await session.destroy()
                except Exception:
                    pass

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
    from src.database import get_service, get_service_versions

    try:
        svc = await get_service(service_id)
        if not svc:
            raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

        versions = await get_service_versions(service_id, status=status)
        # Strip arm_template from listing to keep payload small; use single-version endpoint to fetch it
        versions_summary = []
        for v in versions:
            vs = {k: v2 for k, v2 in v.items() if k != "arm_template"}
            vs["template_size_bytes"] = len(v.get("arm_template") or "") if v.get("arm_template") else 0
            versions_summary.append(vs)
        return JSONResponse({
            "service_id": service_id,
            "active_version": svc.get("active_version"),
            "versions": versions_summary,
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error fetching versions for {service_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/services/{service_id:path}/versions/{version:int}")
async def get_service_version_detail(service_id: str, version: int):
    """Get a single version including the full ARM template content."""
    from src.database import get_service, get_service_versions

    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    versions = await get_service_versions(service_id)
    match = next((v for v in versions if v.get("version") == version), None)
    if not match:
        raise HTTPException(status_code=404, detail=f"Version {version} not found for '{service_id}'")

    return JSONResponse(match)


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
    from src.database import (
        get_service, get_service_versions, create_service_version,
    )
    from src.tools.arm_generator import modify_arm_template_with_copilot

    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be JSON with a 'prompt' field")

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
            from src.database import get_backend as _get_db_backend
            _db = await _get_db_backend()
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
                gen_source=f"llm-modify ({model_id})",
                region="eastus2",
            )

            yield json.dumps({
                "type": "progress",
                "phase": "saving",
                "detail": f"Saving as v{new_semver} (version {new_ver})…",
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
    from src.database import (
        get_service, create_service_version, update_service_version_status,
        update_service_version_template, set_active_service_version,
        fail_service_validation, get_governance_policies_as_dict,
        update_service_version_deployment_info,
    )
    from src.tools.arm_generator import (
        generate_arm_template, has_builtin_skeleton,
        generate_arm_template_with_copilot,
    )
    from src.tools.static_policy_validator import (
        validate_template, build_remediation_prompt,
    )
    from src.standards import (
        get_standards_for_service,
        build_arm_generation_context,
        build_policy_generation_context,
    )

    MAX_HEAL_ATTEMPTS = 5

    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    try:
        body = await request.json()
    except Exception:
        body = {}

    region = body.get("region", "eastus2")
    # Allow per-request model override, fall back to active global model
    model_id = body.get("model", get_active_model())
    # If use_version is set, skip generation and validate the existing draft version
    use_version: int | None = body.get("use_version")
    import uuid as _uuid
    _run_id = _uuid.uuid4().hex[:8]
    rg_name = f"infraforge-val-{service_id.replace('/', '-').replace('.', '-').lower()}-{_run_id}"[:90]

    # ── LLM reasoning helper ─────────────────────────────────

    async def _llm_reason(prompt: str, system_msg: str = "", task: Task = Task.PLANNING) -> str:
        """Ask the LLM to reason about a topic and return its full response.

        The model is selected automatically based on the task type.
        """
        task_model = get_model_for_task(task)
        logger.info(f"[ModelRouter] _llm_reason task={task.value} → model={task_model}")
        session = None
        try:
            _client = await ensure_copilot_client()
            if _client is None:
                raise RuntimeError("Copilot SDK not available")
            session = await _client.create_session({
                "model": task_model, "streaming": True, "tools": [],
                "system_message": {"content": system_msg or (
                    "You are an Azure infrastructure expert performing a detailed analysis. "
                    "Think step-by-step and explain your reasoning clearly."
                )},
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
            return "".join(chunks).strip()
        finally:
            if session:
                try:
                    await session.destroy()
                except Exception:
                    pass

    # ── Copilot fix helper ────────────────────────────────────

    async def _copilot_fix(content: str, error: str, standards_ctx: str = "",
                           planning_context: str = "",
                           previous_attempts: list[dict] | None = None) -> str:
        """Ask the Copilot SDK to fix an ARM template.

        Uses the CODE_FIXING model (gpt-4.1) for surgical error repair.
        Includes the architecture plan so the healer knows the template's intent.
        Tracks previous attempts so each iteration tries a DIFFERENT strategy.
        """
        fix_model = get_model_for_task(Task.CODE_FIXING)
        attempt_num = len(previous_attempts) + 1 if previous_attempts else 1
        logger.info(f"[ModelRouter] _copilot_fix → model={fix_model}, attempt={attempt_num}")

        prompt = (
            "The following ARM template failed Azure deployment validation.\n\n"
            f"--- ERROR ---\n{error}\n--- END ERROR ---\n\n"
            f"--- CURRENT TEMPLATE ---\n{content}\n--- END TEMPLATE ---\n\n"
        )

        # ── Previous attempt history (prevents repeating the same fix) ──
        if previous_attempts:
            prompt += "--- PREVIOUS FAILED ATTEMPTS (DO NOT repeat these fixes) ---\n"
            for pa in previous_attempts:
                prompt += (
                    f"Attempt {pa['attempt']}: Error was: {pa['error'][:300]}\n"
                    f"  Fix tried: {pa['fix_summary']}\n"
                    f"  Result: STILL FAILED — do something DIFFERENT\n\n"
                )
            prompt += "--- END PREVIOUS ATTEMPTS ---\n\n"

        if planning_context:
            prompt += (
                f"--- ARCHITECTURE PLAN (what this template is supposed to achieve) ---\n"
                f"{planning_context}\n--- END PLAN ---\n\n"
            )
        if standards_ctx:
            prompt += (
                f"--- ORGANIZATION STANDARDS (MUST be satisfied) ---\n{standards_ctx}\n"
                "--- END STANDARDS ---\n\n"
            )
        prompt += (
            "Fix the template so it deploys successfully. Return ONLY the "
            "corrected raw JSON — no markdown fences, no explanation.\n\n"
            "CRITICAL RULES:\n"
            "- Keep ALL location parameters as \"[resourceGroup().location]\" or "
            "\"[parameters('location')]\" — NEVER hardcode a region like 'centralus', "
            "'eastus2', etc.\n"
            "- Keep the same resource intent and resource names.\n"
            "- Fix schema issues, missing required properties, invalid API versions, "
            "and structural problems.\n"
            "- If a resource like diagnosticSettings requires an external dependency "
            "(Log Analytics workspace, storage account), REMOVE it rather than adding "
            "a fake dependency.\n"
            "- Ensure EVERY parameter has a \"defaultValue\". This template is deployed "
            "with parameters={}, so any parameter without a default will cause: "
            "'The value for the template parameter ... is not provided'. If a parameter "
            "is missing a default, ADD one (e.g. resourceName \u2192 \"infraforge-resource\").\n"
            "- Ensure ALL resources have tags: environment, owner, costCenter, project.\n"
            "- NEVER add properties that require subscription-level feature registration. "
            "These will fail with 'feature is not enabled for this subscription':\n"
            "  • securityProfile.encryptionAtHost (requires Microsoft.Compute/EncryptionAtHost)\n"
            "  • properties.diskControllerType (requires Microsoft.Compute/DiskControllerTypes)\n"
            "  • securityProfile.securityType 'ConfidentialVM' (requires Microsoft.Compute/ConfidentialVMPreview)\n"
            "  • properties.ultraSSDEnabled (requires Microsoft.Compute/UltraSSDWithVMSS)\n"
            "  If the error mentions 'feature is not enabled', REMOVE the property entirely.\n"
        )

        # ── Escalation strategies for later attempts ──
        if attempt_num >= 4:
            prompt += (
                f"\nESCALATION (attempt {attempt_num}/5 — drastic measures needed):\n"
                "- SIMPLIFY the template: remove optional/nice-to-have resources\n"
                "- Remove diagnosticSettings, locks, autoscale rules if they are causing issues\n"
                "- Use the SIMPLEST valid configuration for each resource\n"
                "- Strip down to ONLY the primary resource with minimal properties\n"
                "- Use well-known, stable API versions (prefer 2023-xx-xx or 2024-xx-xx)\n"
            )
        elif attempt_num >= 2:
            prompt += (
                f"\nThis is attempt {attempt_num}/5. The previous fix(es) did NOT work.\n"
                "You MUST try a FUNDAMENTALLY DIFFERENT approach:\n"
                "- Try a different API version for the failing resource\n"
                "- Restructure resource dependencies\n"
                "- Remove or replace the problematic sub-resource\n"
                "- Check if required properties changed in newer API versions\n"
            )

        session = None
        try:
            _client = await ensure_copilot_client()
            if _client is None:
                raise RuntimeError("Copilot SDK not available")
            session = await _client.create_session({
                "model": fix_model, "streaming": True, "tools": [],
                "system_message": {"content": (
                    "You are an Azure infrastructure expert. "
                    "Return ONLY raw JSON — no markdown, no code fences."
                )},
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

            fixed = "".join(chunks).strip()
            if fixed.startswith("```"):
                lines = fixed.split("\n")[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                fixed = "\n".join(lines).strip()

            # Guard: if healer returned empty or non-JSON, return original
            if not fixed:
                logger.warning("Copilot healer returned empty response — keeping original template")
                return content

            # Try to extract JSON if healer wrapped it in text
            if not fixed.startswith("{"):
                # Try to find JSON object in the response
                _json_start = fixed.find("{")
                _json_end = fixed.rfind("}")
                if _json_start >= 0 and _json_end > _json_start:
                    fixed = fixed[_json_start:_json_end + 1]
                else:
                    logger.warning("Copilot healer returned non-JSON text — keeping original template")
                    return content

            # Validate it's actually valid JSON before returning
            try:
                json.loads(fixed)
            except json.JSONDecodeError:
                logger.warning("Copilot healer returned invalid JSON — keeping original template")
                return content

            # Guard: ensure healer didn't corrupt the location parameter
            try:
                _ft = json.loads(fixed)
                _params = _ft.get("parameters", {})
                _loc = _params.get("location", {})
                _dv = _loc.get("defaultValue", "")
                if isinstance(_dv, str) and _dv and not _dv.startswith("["):
                    _loc["defaultValue"] = "[resourceGroup().location]"
                    logger.warning(f"Copilot healer corrupted location to '{_dv}' — restored")
                    fixed = json.dumps(_ft, indent=2)
                for _res in _ft.get("resources", []):
                    _rloc = _res.get("location", "")
                    if isinstance(_rloc, str) and _rloc and not _rloc.startswith("["):
                        _res["location"] = "[parameters('location')]"
                        logger.warning(f"Copilot healer hardcoded resource location to '{_rloc}' — restored")
                        fixed = json.dumps(_ft, indent=2)
            except (json.JSONDecodeError, AttributeError):
                pass

            # ── Guard: ensure every param has a defaultValue ──
            fixed = _ensure_parameter_defaults(fixed)

            return fixed
        finally:
            if session:
                try:
                    await session.destroy()
                except Exception:
                    pass

    # ── Cleanup helper ────────────────────────────────────────

    async def _cleanup_rg(rg: str):
        from src.tools.deploy_engine import _get_resource_client
        client = _get_resource_client()
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, lambda: client.resource_groups.begin_delete(rg)
            )
            logger.info(f"Cleanup: deletion started for resource group '{rg}'")
        except Exception as e:
            logger.warning(f"Cleanup: failed to delete resource group '{rg}': {e}")

    # ── Template metadata helper ──────────────────────────────

    def _extract_meta(tmpl_str: str) -> dict:
        try:
            t = json.loads(tmpl_str)
        except Exception:
            return {"resource_count": 0, "resource_types": [], "schema": "unknown",
                    "size_kb": round(len(tmpl_str) / 1024, 1)}
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

    # ── Policy compliance evaluation helpers ──────────────────

    def _test_policy_compliance(policy_json: dict, resources: list[dict]) -> list[dict]:
        """Evaluate deployed resources against an Azure Policy definition.

        Interprets the policy's 'if' condition against each resource's
        actual Azure properties. Returns per-resource compliance results.
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
        """Recursively evaluate an Azure Policy condition against a resource."""
        if "allOf" in condition:
            results = [_evaluate_condition(c, resource) for c in condition["allOf"]]
            result = all(results)
            logger.debug(f"  allOf({len(condition['allOf'])} conditions) = {result} (individual: {results})")
            return result
        if "anyOf" in condition:
            results = [_evaluate_condition(c, resource) for c in condition["anyOf"]]
            result = any(results)
            logger.debug(f"  anyOf({len(condition['anyOf'])} conditions) = {result} (individual: {results})")
            return result
        if "not" in condition:
            inner = _evaluate_condition(condition["not"], resource)
            logger.debug(f"  not({inner}) = {not inner}")
            return not inner

        field = condition.get("field", "")
        resource_val = _resolve_field(field, resource)

        # Determine which operator is used and evaluate
        result = False
        op = "unknown"
        if "equals" in condition:
            op = "equals"
            result = str(resource_val).lower() == str(condition["equals"]).lower()
        elif "notEquals" in condition:
            op = "notEquals"
            result = str(resource_val).lower() != str(condition["notEquals"]).lower()
        elif "in" in condition:
            op = "in"
            result = str(resource_val).lower() in [str(v).lower() for v in condition["in"]]
        elif "notIn" in condition:
            op = "notIn"
            result = str(resource_val).lower() not in [str(v).lower() for v in condition["notIn"]]
        elif "contains" in condition:
            op = "contains"
            result = str(condition["contains"]).lower() in str(resource_val).lower()
        elif "like" in condition:
            op = "like"
            import fnmatch
            result = fnmatch.fnmatch(str(resource_val).lower(), str(condition["like"]).lower())
        elif "exists" in condition:
            op = "exists"
            exists = resource_val is not None and resource_val != ""
            # Normalize string booleans: LLMs often return "false"/"true" strings
            want_exists = condition["exists"]
            if isinstance(want_exists, str):
                want_exists = want_exists.lower() not in ("false", "0", "no")
            result = exists if want_exists else not exists
        elif "greater" in condition:
            op = "greater"
            try:
                result = float(resource_val or 0) > float(condition["greater"])
            except (ValueError, TypeError):
                result = False
        elif "less" in condition:
            op = "less"
            try:
                result = float(resource_val or 0) < float(condition["less"])
            except (ValueError, TypeError):
                result = False

        logger.info(f"  Policy eval: field='{field}' op={op} expected={condition.get(op, '?')} actual='{resource_val}' → {result}")
        return result

    def _resolve_field(field: str, resource: dict):
        """Resolve an Azure Policy field reference against a resource dict."""
        field_lower = field.lower()
        if field_lower == "type":
            return resource.get("type", "")
        if field_lower == "location":
            return resource.get("location", "")
        if field_lower == "name":
            return resource.get("name", "")
        if field_lower.startswith("tags["):
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
                matched = None
                for k in val:
                    if k.lower() == part.lower():
                        matched = k
                        break
                val = val.get(matched) if matched else None
            else:
                return None
        return val

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
                "attempt": 0,
                "max_attempts": MAX_HEAL_ATTEMPTS,
                "progress": 0,
                "rg_name": rg_name,
                "events": [],
                "error": "",
            }
            _active_validations[service_id] = tracker
        tracker["updated_at"] = now
        if evt.get("phase"):
            tracker["phase"] = evt["phase"]
        if evt.get("attempt"):
            tracker["attempt"] = evt["attempt"]
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
        elif evt.get("type") == "error":
            tracker["status"] = "failed"
            tracker["error"] = evt.get("detail", "")

    # ── Main streaming generator ──────────────────────────────

    async def stream_onboarding():
        deployed_rg = None
        version_num = None
        current_template = ""
        standards_ctx = ""  # org standards context for ARM generation
        planning_response = ""  # architecture plan from the PLAN phase — fed into EXECUTE
        heal_history: list[dict] = []  # tracks each heal attempt to avoid repeating the same fix

        import os as _os
        _sub_id = _os.environ.get("AZURE_SUBSCRIPTION_ID", "unknown")[:12] + "…"

        # Register job start
        _active_validations[service_id] = {
            "status": "running",
            "service_name": svc.get("name", service_id),
            "category": svc.get("category", ""),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "phase": "starting",
            "detail": "Initializing onboarding pipeline…",
            "attempt": 0,
            "max_attempts": MAX_HEAL_ATTEMPTS,
            "progress": 0,
            "rg_name": rg_name,
            "region": region,
            "subscription": _sub_id,
            "steps_completed": [],
            "events": [],
            "error": "",
        }

        try:
            # ═══════════════════════════════════════════════════
            # PHASE 0: INITIALIZATION & MODEL ROUTING TABLE
            # ═══════════════════════════════════════════════════

            # Build the per-task model routing summary for this run
            _routing = {
                "planning":        {"model": get_model_for_task(Task.PLANNING),          "display": get_model_display(Task.PLANNING),          "reason": get_task_reason(Task.PLANNING)},
                "code_generation": {"model": get_model_for_task(Task.CODE_GENERATION),   "display": get_model_display(Task.CODE_GENERATION),   "reason": get_task_reason(Task.CODE_GENERATION)},
                "code_fixing":     {"model": get_model_for_task(Task.CODE_FIXING),       "display": get_model_display(Task.CODE_FIXING),       "reason": get_task_reason(Task.CODE_FIXING)},
                "policy_gen":      {"model": get_model_for_task(Task.POLICY_GENERATION), "display": get_model_display(Task.POLICY_GENERATION), "reason": get_task_reason(Task.POLICY_GENERATION)},
                "analysis":        {"model": get_model_for_task(Task.VALIDATION_ANALYSIS),"display": get_model_display(Task.VALIDATION_ANALYSIS),"reason": get_task_reason(Task.VALIDATION_ANALYSIS)},
            }

            yield json.dumps({
                "type": "progress", "phase": "init_model",
                "detail": f"🤖 Model routing configured — each pipeline phase uses the optimal model for its task",
                "progress": 0.01,
                "model_routing": _routing,
            }) + "\n"

            # Emit each model assignment as a visible log entry
            for task_key, info in _routing.items():
                yield json.dumps({
                    "type": "llm_reasoning", "phase": "init_model",
                    "detail": f"  {task_key}: {info['display']} — {info['reason'][:80]}",
                    "progress": 0.01,
                }) + "\n"

            # ═══════════════════════════════════════════════════
            # SHORTCUT: use_version — skip generation, validate existing draft
            # ═══════════════════════════════════════════════════

            _skip_generation = False

            if use_version is not None:
                from src.database import get_service_versions as _get_svc_versions
                _all_vers = await _get_svc_versions(service_id)
                _draft = next((v for v in _all_vers if v.get("version") == use_version), None)
                if not _draft:
                    yield json.dumps({
                        "type": "error", "phase": "init",
                        "detail": f"Version {use_version} not found for {service_id}",
                    }) + "\n"
                    return

                current_template = _draft.get("arm_template", "")
                if not current_template:
                    yield json.dumps({
                        "type": "error", "phase": "init",
                        "detail": f"Version {use_version} has no ARM template content",
                    }) + "\n"
                    return

                version_num = use_version
                _draft_semver = _draft.get("semver") or f"{use_version}.0.0"
                gen_source = _draft.get("created_by") or "draft"

                # Mark version as validating
                await update_service_version_status(service_id, use_version, "validating")

                tmpl_meta = _extract_meta(current_template)
                applicable_standards = await get_standards_for_service(service_id)
                policy_standards_ctx = await build_policy_generation_context(service_id)

                yield json.dumps({
                    "type": "progress", "phase": "use_version",
                    "detail": f"📋 Using existing draft v{_draft_semver} — skipping generation, proceeding to validation…",
                    "progress": 0.10,
                }) + "\n"

                yield json.dumps({
                    "type": "init", "phase": "generated",
                    "detail": f"✓ Draft ARM template v{_draft_semver} loaded — "
                              f"{tmpl_meta['resource_count']} resource(s), {tmpl_meta['size_kb']} KB",
                    "progress": 0.12,
                    "version": version_num,
                    "semver": _draft_semver,
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
                        "max_attempts": MAX_HEAL_ATTEMPTS,
                        "version": version_num,
                        "gen_source": gen_source,
                        "model_routing": _routing,
                        "standards_count": len(applicable_standards),
                    },
                }) + "\n"

                _skip_generation = True

            # ═══════════════════════════════════════════════════
            # PHASE 1: ORGANIZATION STANDARDS ANALYSIS
            # (skipped when validating an existing draft via use_version)
            # ═══════════════════════════════════════════════════

            if not _skip_generation:
                yield json.dumps({
                    "type": "progress", "phase": "standards_analysis",
                    "detail": f"Fetching organization standards applicable to {service_id}…",
                    "progress": 0.02,
                }) + "\n"

                applicable_standards = await get_standards_for_service(service_id)
                standards_ctx = await build_arm_generation_context(service_id)
                policy_standards_ctx = await build_policy_generation_context(service_id)

                if applicable_standards:
                    # Emit each standard as a separate event for the log
                    for std in applicable_standards:
                        rule = std.get("rule", {})
                        sev_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(std.get("severity", ""), "⚪")
                        rule_summary = ""
                        rt = rule.get("type", "")
                        if rt == "property":
                            rule_summary = f" → {rule.get('key', '?')} {rule.get('operator', '==')} {json.dumps(rule.get('value', True))}"
                        elif rt == "tags":
                            rule_summary = f" → require tags: {', '.join(rule.get('required_tags', []))}"
                        elif rt == "allowed_values":
                            rule_summary = f" → {rule.get('key', '?')} in [{', '.join(str(v) for v in rule.get('values', [])[:5])}]"
                        elif rt == "cost_threshold":
                            rule_summary = f" → max ${rule.get('max_monthly_usd', 0)}/month"

                        yield json.dumps({
                            "type": "standard_check", "phase": "standards_analysis",
                            "detail": f"{sev_icon} [{std.get('severity', '?').upper()}] {std['name']}: {std['description']}{rule_summary}",
                            "standard": {"id": std["id"], "name": std["name"], "severity": std.get("severity"), "category": std.get("category")},
                            "progress": 0.03,
                        }) + "\n"

                    yield json.dumps({
                        "type": "progress", "phase": "standards_complete",
                        "detail": f"✓ {len(applicable_standards)} organization standard(s) apply — these will constrain ARM template generation and policy validation",
                        "progress": 0.04,
                    }) + "\n"
                else:
                    yield json.dumps({
                        "type": "progress", "phase": "standards_complete",
                        "detail": "No organization standards match this service type — proceeding with default governance rules",
                        "progress": 0.04,
                    }) + "\n"

                # ═══════════════════════════════════════════════════
                # PHASE 2: ARCHITECTURE PLANNING (REASONING MODEL)
                # ═══════════════════════════════════════════════════
                #
                # This is the PLAN phase. It uses a reasoning model (o3-mini) to
                # think deeply about what the ARM template should contain. The
                # plan output is then fed into the EXECUTE phase (code generation)
                # so the generation model doesn't have to figure out architecture
                # — it just follows the plan.

                _planning_model = get_model_display(Task.PLANNING)
                yield json.dumps({
                    "type": "progress", "phase": "planning",
                    "detail": f"🧠 PLAN phase — {_planning_model} is reasoning about architecture for {service_id}…",
                    "progress": 0.05,
                }) + "\n"

                planning_prompt = (
                    f"You are planning an ARM template for the Azure resource type '{service_id}' "
                    f"(service: {svc['name']}, category: {svc.get('category', 'general')}).\n\n"
                )
                if standards_ctx:
                    planning_prompt += (
                        f"The organization has these mandatory standards that MUST be satisfied:\n"
                        f"{standards_ctx}\n\n"
                    )
                planning_prompt += (
                    "Produce a structured architecture plan. This plan will be handed to a "
                    "separate code generation model, so be specific and concrete.\n\n"
                    "## Required Output Sections:\n"
                    "1. **Resources**: List every Azure resource to create (type, API version, purpose)\n"
                    "2. **Security**: Specific security configs (TLS version, encryption, managed identity, network rules)\n"
                    "3. **Parameters**: Template parameters to expose (name, type, default, purpose)\n"
                    "4. **Properties**: Critical properties to set for production readiness\n"
                    "5. **Standards Compliance**: How each org standard will be satisfied\n"
                    "6. **Validation Criteria**: What should pass for this template to be considered correct\n\n"
                    "Be specific — include actual property names, API versions, and configuration values. "
                    "This plan drives code generation."
                )

                try:
                    planning_response = await _llm_reason(planning_prompt, task=Task.PLANNING)
                except Exception as e:
                    logger.warning(f"Planning phase failed (non-fatal): {e}")
                    planning_response = ""

                # Stream the planning response line by line
                for line in planning_response.split("\n"):
                    line = line.strip()
                    if line:
                        yield json.dumps({
                            "type": "llm_reasoning", "phase": "planning",
                            "detail": line,
                            "progress": 0.06,
                        }) + "\n"

                if not planning_response:
                    yield json.dumps({
                        "type": "progress", "phase": "planning_complete",
                        "detail": f"⚠️ Planning phase returned no response — proceeding with ARM template generation without plan",
                        "progress": 0.08,
                    }) + "\n"
                else:
                    yield json.dumps({
                        "type": "progress", "phase": "planning_complete",
                        "detail": f"✓ Architecture plan complete ({len(planning_response)} chars) — handing to code generation model",
                        "progress": 0.08,
                    }) + "\n"

                # ═══════════════════════════════════════════════════
                # PHASE 3: EXECUTE — ARM TEMPLATE GENERATION
                # ═══════════════════════════════════════════════════
                #
                # This is the EXECUTE phase. The code generation model receives
                # the architecture plan and produces the ARM template. It doesn't
                # need to reason about what to build — just follow the plan.

                _gen_model = get_model_display(Task.CODE_GENERATION)
                _gen_model_id = get_model_for_task(Task.CODE_GENERATION)
                yield json.dumps({
                    "type": "progress", "phase": "generating",
                    "detail": f"⚙️ EXECUTE phase — {_gen_model} is generating ARM template guided by the architecture plan…",
                    "progress": 0.09,
                }) + "\n"

                if has_builtin_skeleton(service_id):
                    template_dict = generate_arm_template(service_id)
                    current_template = json.dumps(template_dict, indent=2)
                    gen_source = "built-in skeleton"
                    yield json.dumps({
                        "type": "llm_reasoning", "phase": "generating",
                        "detail": f"📦 Using built-in ARM skeleton for {service_id} — pre-tested template, no LLM generation needed.",
                        "progress": 0.10,
                    }) + "\n"
                else:
                    yield json.dumps({
                        "type": "llm_reasoning", "phase": "generating",
                        "detail": f"No built-in skeleton — {_gen_model} generating ARM template with architecture plan + org standards…",
                        "progress": 0.10,
                    }) + "\n"
                    try:
                        _gen_client = await ensure_copilot_client()
                        if _gen_client is None:
                            raise RuntimeError("Copilot SDK not available for ARM generation")
                        current_template = await generate_arm_template_with_copilot(
                            service_id, svc["name"], _gen_client, _gen_model_id,
                            standards_context=standards_ctx,
                            planning_context=planning_response,
                        )
                    except Exception as gen_err:
                        logger.error(f"ARM generation failed for {service_id}: {gen_err}", exc_info=True)
                        yield json.dumps({
                            "type": "error", "phase": "generating",
                            "detail": f"ARM template generation failed: {str(gen_err)[:300]}",
                        }) + "\n"
                        await fail_service_validation(service_id, f"ARM generation failed: {gen_err}")
                        return
                    gen_source = f"Copilot SDK ({_gen_model})"

                # Validate we actually have JSON before proceeding
                if not current_template or not current_template.strip():
                    yield json.dumps({
                        "type": "error", "phase": "generating",
                        "detail": "ARM template generation returned empty content",
                    }) + "\n"
                    await fail_service_validation(service_id, "ARM template generation returned empty content")
                    return

                try:
                    json.loads(current_template)
                except json.JSONDecodeError as e:
                    yield json.dumps({
                        "type": "error", "phase": "generating",
                        "detail": f"Generated ARM template is not valid JSON: {e}",
                    }) + "\n"
                    await fail_service_validation(service_id, f"Generated ARM template is not valid JSON: {e}")
                    return

                # ── Safety guard: ensure every parameter has a defaultValue ──
                current_template = _ensure_parameter_defaults(current_template)

                tmpl_meta = _extract_meta(current_template)

                # Peek at the next version number for metadata stamping
                from src.database import get_backend as _get_db_backend
                _db = await _get_db_backend()
                _vrows = await _db.execute(
                    "SELECT MAX(version) as max_ver FROM service_versions WHERE service_id = ?",
                    (service_id,),
                )
                _next_ver = (_vrows[0]["max_ver"] if _vrows and _vrows[0]["max_ver"] else 0) + 1
                _semver = _version_to_semver(_next_ver)

                # ── Stamp InfraForge metadata into the ARM template ──
                current_template = _stamp_template_metadata(
                    current_template,
                    service_id=service_id,
                    version_int=_next_ver,
                    gen_source=gen_source,
                    region=region,
                )

                # Create version record
                ver = await create_service_version(
                    service_id=service_id,
                    arm_template=current_template,
                    version=_next_ver,
                    semver=_semver,
                    status="validating",
                    changelog=f"Auto-generated via {gen_source}",
                    created_by=gen_source,
                )
                version_num = ver["version"]

                yield json.dumps({
                    "type": "init", "phase": "generated",
                    "detail": f"✓ ARM template v{_semver} generated via {gen_source} — "
                              f"{tmpl_meta['resource_count']} resource(s), {tmpl_meta['size_kb']} KB",
                    "progress": 0.12,
                    "version": version_num,
                    "semver": _semver,
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
                        "max_attempts": MAX_HEAL_ATTEMPTS,
                        "version": version_num,
                        "gen_source": gen_source,
                        "model_routing": _routing,
                        "standards_count": len(applicable_standards),
                    },
                }) + "\n"

            # ═══════════════════════════════════════════════════
            # PHASE 3.5: AZURE POLICY GENERATION
            # ═══════════════════════════════════════════════════

            _policy_model = get_model_display(Task.POLICY_GENERATION)
            yield json.dumps({
                "type": "progress", "phase": "policy_generation",
                "detail": f"🛡️ Generating Azure Policy definition for {svc['name']} using {_policy_model}…",
                "progress": 0.13,
            }) + "\n"

            generated_policy = None
            policy_gen_prompt = (
                f"Generate an Azure Policy definition JSON for '{svc['name']}' "
                f"(type: {service_id}).\n\n"
            )
            if policy_standards_ctx:
                policy_gen_prompt += (
                    f"Organization standards to enforce:\n{policy_standards_ctx}\n\n"
                )
            policy_gen_prompt += (
                "IMPORTANT — Azure Policy semantics for 'deny' effect:\n"
                "The 'if' condition must describe the VIOLATION (non-compliant state).\n"
                "If the 'if' MATCHES, the resource is DENIED. So use 'exists': false for missing tags,\n"
                "'notIn' for wrong regions, etc.\n\n"
                "DO NOT generate policy conditions for subscription-gated features like:\n"
                "- securityProfile.encryptionAtHost\n"
                "- diskControllerType\n"
                "- securityProfile.securityType (ConfidentialVM)\n"
                "- ultraSSDEnabled\n"
                "These require explicit subscription feature registration and will cause false violations.\n\n"
                "Structure: top-level allOf with [type-check, anyOf-of-violations].\n"
                "Return ONLY raw JSON — NO markdown, NO explanation. Start with {"
            )

            try:
                policy_raw = await _llm_reason(
                    policy_gen_prompt,
                    "You are an Azure Policy expert. Return ONLY raw JSON — no markdown, no code fences.",
                    task=Task.POLICY_GENERATION,
                )
                logger.info(f"Raw policy LLM response ({len(policy_raw)} chars): {policy_raw[:500]}")

                # Robust JSON extraction: handle markdown fences, explanation text, etc.
                _cleaned = policy_raw.strip()

                # Strip markdown code fences (```json ... ``` or ``` ... ```)
                import re as _re
                _fence_match = _re.search(r'```(?:json)?\s*\n(.*?)```', _cleaned, _re.DOTALL)
                if _fence_match:
                    _cleaned = _fence_match.group(1).strip()

                # If still not starting with {, try to find the first { ... } block
                if not _cleaned.startswith('{'):
                    _brace_start = _cleaned.find('{')
                    if _brace_start >= 0:
                        # Find the matching closing brace
                        depth = 0
                        for i in range(_brace_start, len(_cleaned)):
                            if _cleaned[i] == '{':
                                depth += 1
                            elif _cleaned[i] == '}':
                                depth -= 1
                                if depth == 0:
                                    _cleaned = _cleaned[_brace_start:i+1]
                                    break

                generated_policy = json.loads(_cleaned)
                _policy_size = round(len(_cleaned) / 1024, 1)
                logger.info(f"Generated Azure Policy for {service_id}:\n{json.dumps(generated_policy, indent=2)[:2000]}")

                # Describe the generated policy structure
                _rule = generated_policy.get("properties", generated_policy).get("policyRule", {})
                _effect = _rule.get("then", {}).get("effect", "unknown")
                _if_cond = _rule.get("if", {})
                _cond_count = 0
                if "allOf" in _if_cond:
                    _cond_count = len(_if_cond["allOf"])
                elif "anyOf" in _if_cond:
                    _cond_count = len(_if_cond["anyOf"])
                else:
                    _cond_count = 1 if _if_cond else 0

                yield json.dumps({
                    "type": "llm_reasoning", "phase": "policy_generation",
                    "detail": f"📋 Policy generated: {_cond_count} condition(s), effect: {_effect}, size: {_policy_size} KB",
                    "progress": 0.14,
                }) + "\n"

                yield json.dumps({
                    "type": "progress", "phase": "policy_generation_complete",
                    "detail": f"✓ Azure Policy definition generated — will test against deployed resources after deployment",
                    "progress": 0.15,
                }) + "\n"
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"Policy generation via LLM failed: {e} — using deterministic fallback")

                # ── Deterministic fallback: build policy from org standards ──
                _violations = []
                # Always check required tags
                for tag in ["environment", "owner", "costCenter", "project"]:
                    _violations.append({"field": f"tags['{tag}']", "exists": False})
                # Check approved regions
                _violations.append({
                    "field": "location",
                    "notIn": ["eastus2", "westus2", "westeurope"],
                })

                generated_policy = {
                    "properties": {
                        "displayName": f"Governance policy for {svc['name']}",
                        "policyType": "Custom",
                        "mode": "All",
                        "policyRule": {
                            "if": {
                                "allOf": [
                                    {"field": "type", "equals": service_id},
                                    {"anyOf": _violations},
                                ]
                            },
                            "then": {"effect": "deny"},
                        },
                    }
                }
                _policy_size = round(len(json.dumps(generated_policy)) / 1024, 1)
                logger.info(f"Fallback policy for {service_id}: {json.dumps(generated_policy, indent=2)[:1000]}")

                yield json.dumps({
                    "type": "llm_reasoning", "phase": "policy_generation",
                    "detail": f"📋 LLM policy generation failed — using deterministic fallback policy: {len(_violations)} condition(s), effect: deny, size: {_policy_size} KB",
                    "progress": 0.14,
                }) + "\n"
                yield json.dumps({
                    "type": "progress", "phase": "policy_generation_complete",
                    "detail": f"✓ Fallback Azure Policy generated — will test against deployed resources after deployment",
                    "progress": 0.15,
                }) + "\n"

            # ═══════════════════════════════════════════════════
            # PHASE 4: HEALING LOOP (validation + auto-healing)
            # ═══════════════════════════════════════════════════

            gov_policies = await get_governance_policies_as_dict()

            for attempt in range(1, MAX_HEAL_ATTEMPTS + 1):
                is_last = attempt == MAX_HEAL_ATTEMPTS
                att_base = (attempt - 1) / MAX_HEAL_ATTEMPTS

                yield json.dumps({
                    "type": "iteration_start", "attempt": attempt,
                    "max_attempts": MAX_HEAL_ATTEMPTS,
                    "detail": f"Attempt {attempt}/{MAX_HEAL_ATTEMPTS} — validating ARM template v{version_num} "
                              f"({tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s))",
                    "progress": att_base + 0.01,
                }) + "\n"

                # ── 2. Parse JSON ─────────────────────────────
                try:
                    template_json = json.loads(current_template)
                except json.JSONDecodeError as e:
                    error_msg = f"ARM template is not valid JSON — line {e.lineno}, col {e.colno}: {e.msg}"
                    if is_last:
                        await update_service_version_status(service_id, version_num, "failed",
                            validation_result={"error": error_msg})
                        await fail_service_validation(service_id, error_msg)
                        yield json.dumps({"type": "error", "phase": "parsing", "attempt": attempt, "detail": error_msg}) + "\n"
                        return
                    yield json.dumps({"type": "healing", "phase": "fixing_template", "attempt": attempt,
                        "detail": f"JSON parse error — invoking {get_model_display(Task.CODE_FIXING)} to auto-heal (attempt {attempt}, {len(heal_history)} prior fixes tried)…", "progress": att_base + 0.02}) + "\n"
                    _pre_fix = current_template
                    current_template = await _copilot_fix(current_template, error_msg, standards_ctx, planning_context=planning_response, previous_attempts=heal_history)
                    heal_history.append({"attempt": attempt, "phase": "parsing", "error": error_msg, "fix_summary": _summarize_fix(_pre_fix, current_template)})
                    tmpl_meta = _extract_meta(current_template)
                    await update_service_version_template(service_id, version_num, current_template, "copilot-healed")
                    yield json.dumps({"type": "healing_done", "phase": "template_fixed", "attempt": attempt,
                        "detail": f"{get_model_display(Task.CODE_FIXING)} rewrote template — retrying…", "progress": att_base + 0.03}) + "\n"
                    continue

                # ── 3. Static Policy Check ────────────────────
                yield json.dumps({
                    "type": "progress", "phase": "static_policy_check", "attempt": attempt,
                    "detail": f"Running static policy validation against {len(gov_policies)} organization governance rules…",
                    "progress": att_base + 0.04,
                }) + "\n"

                report = validate_template(template_json, gov_policies)

                # Emit individual check results
                for check in report.results:
                    icon = "✅" if check.passed else ("⚠️" if check.enforcement == "warn" else "❌")
                    yield json.dumps({
                        "type": "policy_result", "phase": "static_policy_check", "attempt": attempt,
                        "detail": f"{icon} [{check.rule_id}] {check.rule_name}: {check.message}",
                        "passed": check.passed,
                        "severity": check.severity,
                        "progress": att_base + 0.05,
                    }) + "\n"

                if not report.passed:
                    fail_msg = f"Static policy check: {report.passed_checks}/{report.total_checks} passed, {report.blockers} blocker(s)"
                    yield json.dumps({
                        "type": "progress", "phase": "static_policy_failed", "attempt": attempt,
                        "detail": fail_msg, "progress": att_base + 0.06,
                    }) + "\n"

                    if is_last:
                        await update_service_version_status(service_id, version_num, "failed",
                            policy_check=report.to_dict())
                        await fail_service_validation(service_id, fail_msg)
                        yield json.dumps({"type": "error", "phase": "static_policy", "attempt": attempt,
                            "detail": f"Static policy validation failed after {MAX_HEAL_ATTEMPTS} attempts"}) + "\n"
                        return

                    # Build targeted remediation prompt from failed checks
                    failed_checks = [c for c in report.results if not c.passed and c.enforcement == "block"]
                    fix_prompt = build_remediation_prompt(current_template, failed_checks)
                    yield json.dumps({"type": "healing", "phase": "fixing_template", "attempt": attempt,
                        "detail": f"Policy violations detected — {get_model_display(Task.CODE_FIXING)} auto-healing template for {len(failed_checks)} blocker(s) (attempt {attempt}, {len(heal_history)} prior fixes tried)…",
                        "progress": att_base + 0.07}) + "\n"
                    _pre_fix = current_template
                    current_template = await _copilot_fix(current_template, fix_prompt, standards_ctx, planning_context=planning_response, previous_attempts=heal_history)
                    heal_history.append({"attempt": attempt, "phase": "static_policy", "error": fix_prompt[:500], "fix_summary": _summarize_fix(_pre_fix, current_template)})
                    tmpl_meta = _extract_meta(current_template)
                    await update_service_version_template(service_id, version_num, current_template, "copilot-healed")
                    yield json.dumps({"type": "healing_done", "phase": "template_fixed", "attempt": attempt,
                        "detail": f"{get_model_display(Task.CODE_FIXING)} remediated template — retrying…", "progress": att_base + 0.08}) + "\n"
                    continue

                yield json.dumps({
                    "type": "progress", "phase": "static_policy_complete", "attempt": attempt,
                    "detail": f"✓ Static policy check passed — {report.passed_checks}/{report.total_checks} checks, 0 blockers",
                    "progress": att_base + 0.08,
                }) + "\n"

                # Save policy check results
                await update_service_version_status(service_id, version_num, "validating",
                    policy_check=report.to_dict())

                # ── 4. What-If ────────────────────────────────
                res_types_str = ", ".join(tmpl_meta["resource_types"][:5]) or "unknown"
                yield json.dumps({
                    "type": "progress", "phase": "what_if", "attempt": attempt,
                    "detail": f"Submitting ARM What-If to Azure — previewing {tmpl_meta['resource_count']} resource(s) "
                              f"[{res_types_str}] in '{rg_name}' ({region})",
                    "progress": att_base + 0.10,
                }) + "\n"

                try:
                    from src.tools.deploy_engine import run_what_if
                    wif = await run_what_if(resource_group=rg_name, template=template_json,
                                           parameters=_extract_param_values(template_json), region=region)
                except Exception as e:
                    logger.error(f"What-If attempt {attempt} exception: {e}", exc_info=True)
                    wif = {"status": "error", "errors": [str(e)]}

                if wif.get("status") != "success":
                    errors = "; ".join(str(e) for e in wif.get("errors", [])) or "Unknown What-If error"

                    # Detect transient infra errors
                    _infra_keywords = ("beingdeleted", "being deleted", "deprovisioning",
                                       "throttled", "toomanyrequests", "retryable",
                                       "serviceunavailable", "internalservererror")
                    if any(kw in errors.lower() for kw in _infra_keywords):
                        yield json.dumps({"type": "progress", "phase": "infra_retry", "attempt": attempt,
                            "detail": f"Transient Azure error — waiting 10s…", "progress": att_base + 0.11}) + "\n"
                        await asyncio.sleep(10)
                        continue

                    if is_last:
                        await update_service_version_status(service_id, version_num, "failed",
                            validation_result={"error": errors, "phase": "what_if"})
                        await fail_service_validation(service_id, f"What-If failed: {errors}")
                        yield json.dumps({"type": "error", "phase": "what_if", "attempt": attempt,
                            "detail": f"What-If failed after {MAX_HEAL_ATTEMPTS} attempts: {errors}"}) + "\n"
                        return

                    yield json.dumps({"type": "healing", "phase": "fixing_template", "attempt": attempt,
                        "detail": f"What-If rejected by ARM — invoking {get_model_display(Task.CODE_FIXING)} to auto-heal (attempt {attempt}, {len(heal_history)} prior fixes tried)… Error: {errors[:300]}",
                        "progress": att_base + 0.12}) + "\n"
                    _pre_fix = current_template
                    current_template = await _copilot_fix(current_template, errors, standards_ctx, planning_context=planning_response, previous_attempts=heal_history)
                    heal_history.append({"attempt": attempt, "phase": "what_if", "error": errors[:500], "fix_summary": _summarize_fix(_pre_fix, current_template)})
                    tmpl_meta = _extract_meta(current_template)
                    await update_service_version_template(service_id, version_num, current_template, "copilot-healed")
                    yield json.dumps({"type": "healing_done", "phase": "template_fixed", "attempt": attempt,
                        "detail": f"{get_model_display(Task.CODE_FIXING)} rewrote template — retrying…", "progress": att_base + 0.13}) + "\n"
                    continue

                change_summary = ", ".join(f"{v} {k}" for k, v in wif.get("change_counts", {}).items())
                yield json.dumps({
                    "type": "progress", "phase": "what_if_complete", "attempt": attempt,
                    "detail": f"✓ What-If passed — changes: {change_summary or 'none'}",
                    "progress": att_base + 0.14,
                    "result": wif,
                }) + "\n"

                # ── 5. Deploy ─────────────────────────────────
                yield json.dumps({
                    "type": "progress", "phase": "deploying", "attempt": attempt,
                    "detail": f"Deploying {tmpl_meta['resource_count']} resource(s) into '{rg_name}' ({region})…",
                    "progress": att_base + 0.16,
                }) + "\n"

                try:
                    from src.tools.deploy_engine import execute_deployment
                    deploy_result = await execute_deployment(
                        resource_group=rg_name, template=template_json,
                        parameters=_extract_param_values(template_json), region=region,
                        deployment_name=f"validate-{attempt}",
                        initiated_by="InfraForge Validator",
                    )
                    deploy_status = deploy_result.get("status", "unknown")
                except Exception as e:
                    logger.error(f"Deploy attempt {attempt} exception: {e}", exc_info=True)
                    deploy_result = {"status": "failed", "error": str(e)}
                    deploy_status = "failed"

                deployed_rg = rg_name

                if deploy_status != "succeeded":
                    deploy_error = deploy_result.get("error", "Unknown deployment error")

                    # Fetch operation-level errors if generic
                    if "Please list deployment operations" in deploy_error or "At least one resource" in deploy_error:
                        try:
                            from src.tools.deploy_engine import _get_resource_client, _get_deployment_operation_errors
                            _rc = _get_resource_client()
                            _lp = asyncio.get_event_loop()
                            op_errors = await _get_deployment_operation_errors(_rc, _lp, rg_name, f"validate-{attempt}")
                            if op_errors:
                                deploy_error = f"{deploy_error} | Operation errors: {op_errors}"
                        except Exception:
                            pass

                    _is_infra = any(kw in deploy_error.lower() for kw in
                        ("beingdeleted", "being deleted", "throttled", "toomanyrequests",
                         "serviceunavailable", "internalservererror"))

                    yield json.dumps({"type": "progress", "phase": "deploy_failed", "attempt": attempt,
                        "detail": f"Deployment failed: {deploy_error[:400]}", "progress": att_base + 0.20}) + "\n"

                    if _is_infra:
                        yield json.dumps({"type": "progress", "phase": "infra_retry", "attempt": attempt,
                            "detail": "Transient infra error — waiting 10s…", "progress": att_base + 0.21}) + "\n"
                        await asyncio.sleep(10)
                        continue

                    if is_last:
                        await _cleanup_rg(rg_name)
                        await update_service_version_status(service_id, version_num, "failed",
                            validation_result={"error": deploy_error, "phase": "deploy"})
                        await fail_service_validation(service_id, f"Deploy failed: {deploy_error}")
                        yield json.dumps({"type": "error", "phase": "deploy", "attempt": attempt,
                            "detail": f"Deploy failed after {MAX_HEAL_ATTEMPTS} attempts"}) + "\n"
                        return

                    yield json.dumps({"type": "healing", "phase": "fixing_template", "attempt": attempt,
                        "detail": f"Deployment failed — {get_model_display(Task.CODE_FIXING)} auto-healing (attempt {attempt}, {len(heal_history)} prior fixes tried)… Error: {deploy_error[:300]}",
                        "progress": att_base + 0.21}) + "\n"
                    _pre_fix = current_template
                    current_template = await _copilot_fix(current_template, deploy_error, standards_ctx, planning_context=planning_response, previous_attempts=heal_history)
                    heal_history.append({"attempt": attempt, "phase": "deploy", "error": deploy_error[:500], "fix_summary": _summarize_fix(_pre_fix, current_template)})
                    tmpl_meta = _extract_meta(current_template)
                    await update_service_version_template(service_id, version_num, current_template, "copilot-healed")
                    yield json.dumps({"type": "healing_done", "phase": "template_fixed", "attempt": attempt,
                        "detail": f"{get_model_display(Task.CODE_FIXING)} rewrote template — redeploying…", "progress": att_base + 0.22}) + "\n"
                    continue

                # Deploy succeeded!
                provisioned = deploy_result.get("provisioned_resources", [])
                resource_summaries = [f"{r.get('type','?')}/{r.get('name','?')}" for r in provisioned]

                # Persist deployment tracking info to the version row
                _deploy_name = f"validate-{attempt}"
                _subscription_id = deploy_result.get("subscription_id", "")
                _deployment_id = deploy_result.get("deployment_id", "")
                await update_service_version_deployment_info(
                    service_id, version_num,
                    run_id=_run_id,
                    resource_group=rg_name,
                    deployment_name=_deploy_name,
                    subscription_id=_subscription_id,
                )

                yield json.dumps({
                    "type": "progress", "phase": "deploy_complete", "attempt": attempt,
                    "detail": f"✓ Deployment succeeded — {len(provisioned)} resource(s): {'; '.join(resource_summaries[:5])}",
                    "progress": att_base + 0.22,
                    "resources": provisioned,
                }) + "\n"

                # ── 6. Resource verification (with full properties) ──
                yield json.dumps({
                    "type": "progress", "phase": "resource_check", "attempt": attempt,
                    "detail": f"Querying Azure to verify {len(provisioned)} resource(s) and fetch full properties for policy evaluation…",
                    "progress": att_base + 0.24,
                }) + "\n"

                from src.tools.deploy_engine import _get_resource_client
                rc = _get_resource_client()
                loop = asyncio.get_event_loop()
                try:
                    live_resources = await loop.run_in_executor(
                        None, lambda: list(rc.resources.list_by_resource_group(rg_name))
                    )
                    resource_details = []
                    for r in live_resources:
                        detail = {
                            "id": r.id, "name": r.name, "type": r.type,
                            "location": r.location, "tags": dict(r.tags) if r.tags else {},
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
                        "type": "progress", "phase": "resource_check_complete", "attempt": attempt,
                        "detail": f"✓ Verified {len(resource_details)} live resource(s) with full properties: {'; '.join(res_detail_strs)}",
                        "progress": att_base + 0.26,
                        "resources": [{"name": r["name"], "type": r["type"], "location": r["location"]} for r in resource_details],
                    }) + "\n"
                except Exception as e:
                    resource_details = []
                    yield json.dumps({
                        "type": "progress", "phase": "resource_check_warning", "attempt": attempt,
                        "detail": f"Could not enumerate resources (non-fatal): {e}",
                        "progress": att_base + 0.26,
                    }) + "\n"

                # ── 6.5 Runtime policy compliance test ────────
                policy_results = []
                all_policy_compliant = True

                if generated_policy and resource_details:
                    _policy_rule = generated_policy.get("properties", generated_policy).get("policyRule", {})
                    _policy_effect = _policy_rule.get("then", {}).get("effect", "deny")
                    yield json.dumps({
                        "type": "progress", "phase": "policy_testing", "attempt": attempt,
                        "detail": f"🛡️ Evaluating {len(resource_details)} deployed resource(s) against generated Azure Policy (effect: {_policy_effect})…",
                        "progress": att_base + 0.27,
                    }) + "\n"

                    logger.info(f"Policy evaluation — {len(resource_details)} resource(s), policy if-condition: {json.dumps(_policy_rule.get('if', {}), indent=2)[:1000]}")
                    for rd in resource_details:
                        logger.info(f"Resource to evaluate: name={rd.get('name')} type={rd.get('type')} tags={rd.get('tags')} location={rd.get('location')}")

                    policy_results = _test_policy_compliance(generated_policy, resource_details)
                    all_policy_compliant = all(r["compliant"] for r in policy_results)
                    compliant_count = sum(1 for r in policy_results if r["compliant"])
                    for pr in policy_results:
                        logger.info(f"Policy result: {pr['resource_name']} compliant={pr['compliant']} reason={pr['reason']}")

                    for pr in policy_results:
                        icon = "✅" if pr["compliant"] else "❌"
                        yield json.dumps({
                            "type": "policy_result", "phase": "policy_testing", "attempt": attempt,
                            "detail": f"{icon} {pr['resource_type']}/{pr['resource_name']} — {pr['reason']}",
                            "compliant": pr["compliant"],
                            "resource": pr,
                            "progress": att_base + 0.28,
                        }) + "\n"

                    if not all_policy_compliant:
                        violations = [pr for pr in policy_results if not pr["compliant"]]
                        violation_desc = "; ".join(f"{v['resource_name']}: {v['reason']}" for v in violations)
                        fail_msg = (
                            f"{compliant_count}/{len(policy_results)} resources compliant — "
                            f"{len(violations)} policy violation(s): {violation_desc[:300]}"
                        )
                        yield json.dumps({
                            "type": "progress", "phase": "policy_failed", "attempt": attempt,
                            "detail": fail_msg,
                            "progress": att_base + 0.29,
                        }) + "\n"

                        if is_last:
                            await _cleanup_rg(rg_name)
                            deployed_rg = None
                            await update_service_version_status(service_id, version_num, "failed",
                                validation_result={"error": fail_msg, "phase": "policy_compliance"})
                            await fail_service_validation(service_id, fail_msg)
                            yield json.dumps({
                                "type": "error", "phase": "policy", "attempt": attempt,
                                "detail": f"Runtime policy compliance failed after {MAX_HEAL_ATTEMPTS} auto-heal attempts. Violations: {violation_desc}",
                            }) + "\n"
                            return

                        import json as _json_mod
                        _policy_str = _json_mod.dumps(generated_policy, indent=2)[:500]
                        fix_error = f"Runtime policy violation: {violation_desc}. The policy requires: {_policy_str}"
                        yield json.dumps({
                            "type": "healing", "phase": "fixing_template", "attempt": attempt,
                            "detail": f"Policy violations on {len(violations)} resource(s) — {get_model_display(Task.CODE_FIXING)} auto-healing (attempt {attempt}, {len(heal_history)} prior fixes tried)…",
                            "progress": att_base + 0.30,
                        }) + "\n"
                        _pre_fix = current_template
                        current_template = await _copilot_fix(current_template, fix_error, standards_ctx, planning_context=planning_response, previous_attempts=heal_history)
                        heal_history.append({"attempt": attempt, "phase": "policy_compliance", "error": fix_error[:500], "fix_summary": _summarize_fix(_pre_fix, current_template)})
                        tmpl_meta = _extract_meta(current_template)
                        await update_service_version_template(service_id, version_num, current_template, "copilot-healed")
                        yield json.dumps({
                            "type": "healing_done", "phase": "template_fixed", "attempt": attempt,
                            "detail": f"{get_model_display(Task.CODE_FIXING)} rewrote template for policy compliance — redeploying…",
                            "progress": att_base + 0.31,
                        }) + "\n"
                        continue
                    else:
                        yield json.dumps({
                            "type": "progress", "phase": "policy_testing_complete", "attempt": attempt,
                            "detail": f"✓ All {len(policy_results)} resource(s) passed runtime policy compliance check",
                            "progress": att_base + 0.30,
                        }) + "\n"
                elif not generated_policy:
                    yield json.dumps({
                        "type": "progress", "phase": "policy_skip", "attempt": attempt,
                        "detail": "No Azure Policy was generated — skipping runtime policy compliance test",
                        "progress": att_base + 0.30,
                    }) + "\n"
                else:
                    yield json.dumps({
                        "type": "progress", "phase": "policy_skip", "attempt": attempt,
                        "detail": "No resources to test — skipping runtime policy compliance test",
                        "progress": att_base + 0.30,
                    }) + "\n"

                # ── 7. Cleanup ────────────────────────────────
                yield json.dumps({
                    "type": "progress", "phase": "cleanup", "attempt": attempt,
                    "detail": f"All checks passed — deleting validation RG '{rg_name}'…",
                    "progress": 0.90,
                }) + "\n"

                await _cleanup_rg(rg_name)
                deployed_rg = None

                yield json.dumps({
                    "type": "progress", "phase": "cleanup_complete", "attempt": attempt,
                    "detail": f"✓ Validation RG '{rg_name}' deletion initiated",
                    "progress": 0.93,
                }) + "\n"

                # ── 8. Promote ────────────────────────────────
                validation_summary = {
                    "run_id": _run_id,
                    "resource_group": rg_name,
                    "deployment_name": _deploy_name,
                    "subscription_id": _subscription_id,
                    "deployment_id": _deployment_id,
                    "what_if": wif,
                    "deploy_result": {
                        "status": deploy_result.get("status"),
                        "started_at": deploy_result.get("started_at"),
                        "completed_at": deploy_result.get("completed_at"),
                        "deployment_id": _deployment_id,
                    },
                    "deployed_resources": [{"name": r["name"], "type": r["type"], "location": r["location"]}
                                           for r in resource_details],
                    "policy_check": report.to_dict(),
                    "policy_compliance": policy_results,
                    "all_policy_compliant": all_policy_compliant,
                    "has_runtime_policy": generated_policy is not None,
                    "attempts": attempt,
                    "heal_history": heal_history,
                }

                yield json.dumps({
                    "type": "progress", "phase": "promoting", "attempt": attempt,
                    "detail": f"Promoting {svc['name']} v{version_num} → approved…",
                    "progress": 0.97,
                }) + "\n"

                await update_service_version_status(
                    service_id, version_num, "approved",
                    validation_result=validation_summary,
                    policy_check=report.to_dict(),
                )
                await set_active_service_version(service_id, version_num)

                _policy_str = ""
                if policy_results:
                    _pc = sum(1 for r in policy_results if r["compliant"])
                    _policy_str = f", {_pc}/{len(policy_results)} runtime policy check(s) passed"
                yield json.dumps({
                    "type": "done", "phase": "approved", "attempt": attempt,
                    "total_attempts": attempt,
                    "version": version_num,
                    "detail": f"🎉 {svc['name']} v{version_num} approved! "
                              f"{len(resource_details)} resource(s) validated, "
                              f"{report.passed_checks}/{report.total_checks} static policy checks passed"
                              f"{_policy_str}."
                              f"{'' if attempt == 1 else f' Required {attempt} auto-heal attempt(s).'}",
                    "progress": 1.0,
                    "summary": validation_summary,
                }) + "\n"
                return  # ✅ success

        except Exception as e:
            logger.error(f"Onboarding error for {service_id}: {e}", exc_info=True)
            if version_num:
                try:
                    await update_service_version_status(service_id, version_num, "failed",
                        validation_result={"error": str(e)})
                except Exception:
                    pass
            try:
                await fail_service_validation(service_id, str(e))
            except Exception:
                pass
            yield json.dumps({"type": "error", "phase": "unknown", "detail": str(e)}) + "\n"
        except (GeneratorExit, asyncio.CancelledError):
            logger.warning(f"Onboarding cancelled for {service_id}")
            try:
                await fail_service_validation(service_id, "Cancelled — please retry.")
            except Exception:
                pass
        finally:
            if deployed_rg:
                try:
                    await _cleanup_rg(deployed_rg)
                except Exception:
                    pass

    async def _tracked_stream():
        try:
            async for line in stream_onboarding():
                _track(line)
                yield line
        finally:
            async def _cleanup_tracker():
                await asyncio.sleep(300)
                _active_validations.pop(service_id, None)
            asyncio.create_task(_cleanup_tracker())

    return StreamingResponse(
        _tracked_stream(),
        media_type="application/x-ndjson",
    )


# ── Deployment API ────────────────────────────────────────────

@app.get("/api/deployments")
async def list_deployments_endpoint(
    status: Optional[str] = None,
    resource_group: Optional[str] = None,
):
    """List deployment history."""
    from src.database import get_deployments

    try:
        deployments = await get_deployments(
            status=status,
            resource_group=resource_group,
        )
        return JSONResponse({
            "deployments": deployments,
            "total": len(deployments),
        })
    except Exception as e:
        logger.error(f"Failed to list deployments: {e}")
        return JSONResponse({"deployments": [], "total": 0})


@app.get("/api/deployments/{deployment_id}")
async def get_deployment_endpoint(deployment_id: str):
    """Get a single deployment's details."""
    from src.database import get_deployment

    deployment = await get_deployment(deployment_id)
    if not deployment:
        # Check in-memory (may be still running)
        from src.tools.deploy_engine import deploy_manager
        record = deploy_manager.deployments.get(deployment_id)
        if record:
            return JSONResponse(record.to_dict())
        raise HTTPException(status_code=404, detail="Deployment not found")
    return JSONResponse(deployment)


@app.get("/api/deployments/{deployment_id}/stream")
async def stream_deployment_progress(deployment_id: str):
    """Stream real-time deployment progress via SSE.

    Subscribe to live progress events for an active deployment.
    Replays history on connect so late-joiners see current state.
    """
    from src.tools.deploy_engine import deploy_manager

    record = deploy_manager.deployments.get(deployment_id)
    if not record:
        raise HTTPException(status_code=404, detail="Deployment not found or already completed")

    async def _event_stream():
        q = deploy_manager.subscribe(deployment_id)
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            deploy_manager.unsubscribe(deployment_id, q)

    return StreamingResponse(_event_stream(), media_type="text/event-stream")


# ── Approval Management API ──────────────────────────────────

@app.get("/api/approvals")
async def list_approvals(
    status: Optional[str] = None,
    requestor_email: Optional[str] = None,
):
    """List approval requests, optionally filtered by status or requestor."""
    try:
        requests = await get_approval_requests(
            status=status,
            requestor_email=requestor_email,
        )
        # Convert Row objects to dicts if needed
        result = []
        for r in requests:
            if isinstance(r, dict):
                result.append(r)
            else:
                result.append(dict(r))
        return JSONResponse({
            "requests": result,
            "total": len(result),
        })
    except Exception as e:
        logger.error(f"Failed to list approval requests: {e}")
        return JSONResponse({"requests": [], "total": 0})


@app.get("/api/approvals/{request_id}")
async def get_approval_detail(request_id: str):
    """Get details of a specific approval request."""
    try:
        requests = await get_approval_requests()
        matching = [r for r in requests if (r.get("id") if isinstance(r, dict) else r["id"]) == request_id]
        if not matching:
            raise HTTPException(status_code=404, detail="Approval request not found")
        req = matching[0]
        return JSONResponse(dict(req) if not isinstance(req, dict) else req)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get approval request {request_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/approvals/{request_id}/review")
async def review_approval(request_id: str, request: Request):
    """IT admin action: approve, conditionally approve, deny, or defer a request."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    decision = body.get("decision")
    reviewer = body.get("reviewer", "Platform Team")
    review_notes = body.get("review_notes", "")

    valid_decisions = {"approved", "conditional", "denied", "deferred"}
    if decision not in valid_decisions:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid decision. Must be one of: {', '.join(sorted(valid_decisions))}",
        )

    try:
        success = await update_approval_request(
            request_id=request_id,
            status=decision,
            reviewer=reviewer,
            review_notes=review_notes,
        )
        if not success:
            raise HTTPException(status_code=404, detail="Approval request not found or already finalized")

        return JSONResponse({
            "success": True,
            "request_id": request_id,
            "decision": decision,
            "reviewer": reviewer,
            "message": f"Request {request_id} has been {decision}.",
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to review approval request {request_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ── Governance API ───────────────────────────────────────────

@app.get("/api/governance/security-standards")
async def list_security_standards(category: Optional[str] = None):
    """Return all security standards, optionally filtered by category."""
    from src.database import get_security_standards as db_get_standards

    try:
        standards = await db_get_standards(category=category, enabled_only=True)
        # Convert Row to dict
        result = [dict(s) if not isinstance(s, dict) else s for s in standards]
        categories = sorted(set(s.get("category", "") for s in result))
        return JSONResponse({
            "standards": result,
            "categories": categories,
            "total": len(result),
        })
    except Exception as e:
        logger.error(f"Failed to load security standards: {e}")
        return JSONResponse({"standards": [], "categories": [], "total": 0})


@app.get("/api/governance/compliance-frameworks")
async def list_compliance_frameworks():
    """Return all compliance frameworks with their controls."""
    from src.database import get_compliance_frameworks as db_get_frameworks

    try:
        frameworks = await db_get_frameworks(enabled_only=True)
        result = []
        for fw in frameworks:
            fw_dict = dict(fw) if not isinstance(fw, dict) else fw
            # Controls are already hydrated by the CRUD function
            controls = fw_dict.get("controls", [])
            fw_dict["control_count"] = len(controls)
            result.append(fw_dict)
        return JSONResponse({
            "frameworks": result,
            "total": len(result),
        })
    except Exception as e:
        logger.error(f"Failed to load compliance frameworks: {e}")
        return JSONResponse({"frameworks": [], "total": 0})


@app.get("/api/governance/policies")
async def list_governance_policies(category: Optional[str] = None):
    """Return all governance policies, optionally filtered by category."""
    from src.database import get_governance_policies as db_get_policies

    try:
        policies = await db_get_policies(category=category, enabled_only=True)
        result = [dict(p) if not isinstance(p, dict) else p for p in policies]
        categories = sorted(set(p.get("category", "") for p in result))
        return JSONResponse({
            "policies": result,
            "categories": categories,
            "total": len(result),
        })
    except Exception as e:
        logger.error(f"Failed to load governance policies: {e}")
        return JSONResponse({"policies": [], "categories": [], "total": 0})


# ── WebSocket Chat ───────────────────────────────────────────

@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """WebSocket endpoint for streaming chat with InfraForge.

    Protocol:
    1. Client connects and sends: {"type": "auth", "sessionToken": "..."}
    2. Server validates and responds: {"type": "auth_ok", "user": {...}}
    3. Client sends messages: {"type": "message", "content": "..."}
    4. Server streams responses: {"type": "delta", "content": "..."} chunks
    5. Server sends completion: {"type": "done", "content": "full response"}
    6. Server sends tool calls: {"type": "tool_call", "name": "...", "status": "..."}
    """
    await websocket.accept()

    session_token: Optional[str] = None
    user_context: Optional[UserContext] = None

    try:
        # ── Step 1: Authenticate ─────────────────────────────
        auth_msg = await asyncio.wait_for(websocket.receive_json(), timeout=30)

        if auth_msg.get("type") != "auth":
            await websocket.send_json({"type": "error", "message": "Expected auth message"})
            await websocket.close()
            return

        session_token = auth_msg.get("sessionToken", "")
        user_context = await get_user_context(session_token)

        if not user_context:
            await websocket.send_json({"type": "error", "message": "Invalid or expired session"})
            await websocket.close()
            return

        # ── Step 2: Create Copilot session with user context ─
        client = await ensure_copilot_client()
        if client is None:
            await websocket.send_json({
                "type": "error",
                "message": "Copilot SDK is not available. Chat is disabled but the rest of InfraForge works.",
            })
            await websocket.close()
            return

        personalized_system_message = (
            SYSTEM_MESSAGE + "\n" + user_context.to_prompt_context()
        )

        tools = get_all_tools()
        try:
            copilot_session = await client.create_session({
                "model": get_active_model(),
                "streaming": True,
                "tools": tools,
                "system_message": {"content": personalized_system_message},
            })
        except Exception as e:
            logger.error(f"Failed to create Copilot session: {e}")
            await websocket.send_json({
                "type": "error",
                "message": f"Failed to create chat session: {e}",
            })
            await websocket.close()
            return

        active_sessions[session_token] = {
            "copilot_session": copilot_session,
            "user_context": user_context,
            "connected_at": time.time(),
        }

        await websocket.send_json({
            "type": "auth_ok",
            "user": {
                "displayName": user_context.display_name,
                "email": user_context.email,
                "department": user_context.department,
                "team": user_context.team,
            },
        })

        # ── Step 3: Chat loop ────────────────────────────────
        while True:
            data = await websocket.receive_json()

            if data.get("type") == "message":
                user_message = data.get("content", "").strip()
                if not user_message:
                    continue

                # Track for analytics
                request_record = {
                    "timestamp": time.time(),
                    "user": user_context.email,
                    "department": user_context.department,
                    "cost_center": user_context.cost_center,
                    "prompt": user_message[:200],  # Truncate for privacy
                    "resource_types": [],
                    "estimated_cost": 0.0,
                    "from_catalog": False,
                }

                # Stream the response
                response_chunks: list[str] = []
                done_event = asyncio.Event()

                def on_event(event):
                    try:
                        if event.type.value == "assistant.message_delta":
                            delta = event.data.delta_content or ""
                            response_chunks.append(delta)
                            asyncio.get_event_loop().create_task(
                                websocket.send_json({
                                    "type": "delta",
                                    "content": delta,
                                })
                            )
                        elif event.type.value == "assistant.message":
                            full_content = event.data.content or ""
                            asyncio.get_event_loop().create_task(
                                websocket.send_json({
                                    "type": "done",
                                    "content": full_content,
                                })
                            )
                        elif event.type.value == "tool.call":
                            tool_name = getattr(event.data, 'name', 'unknown')
                            asyncio.get_event_loop().create_task(
                                websocket.send_json({
                                    "type": "tool_call",
                                    "name": tool_name,
                                    "status": "running",
                                })
                            )
                            # Track catalog usage
                            if tool_name == "search_template_catalog":
                                request_record["from_catalog"] = True
                        elif event.type.value == "tool.result":
                            tool_name = getattr(event.data, 'name', 'unknown')
                            asyncio.get_event_loop().create_task(
                                websocket.send_json({
                                    "type": "tool_call",
                                    "name": tool_name,
                                    "status": "complete",
                                })
                            )
                        elif event.type.value == "session.idle":
                            done_event.set()
                    except Exception as e:
                        logger.error(f"Event handler error: {e}")
                        done_event.set()

                unsubscribe = copilot_session.on(on_event)

                try:
                    await copilot_session.send({"prompt": user_message})
                    await asyncio.wait_for(done_event.wait(), timeout=120)
                except asyncio.TimeoutError:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Request timed out. Please try again.",
                    })
                finally:
                    unsubscribe()

                # Persist to database
                full_response = "".join(response_chunks)
                await save_chat_message(session_token, "user", user_message)
                await save_chat_message(session_token, "assistant", full_response)
                await log_usage(request_record)

            elif data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        logger.info(f"Client disconnected: {user_context.email if user_context else 'unknown'}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        # Don't destroy the Copilot session on disconnect — user may reconnect
        pass
