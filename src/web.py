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

logger = logging.getLogger("infraforge.web")

# ── Global state ─────────────────────────────────────────────
copilot_client: Optional[CopilotClient] = None

# Track active Copilot sessions: session_token → { copilot_session, user_context }
# (Chat history and usage analytics are persisted in the database)
active_sessions: dict[str, dict] = {}


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
    logger.info("Starting Copilot SDK client...")
    copilot_client = CopilotClient({"log_level": COPILOT_LOG_LEVEL})
    await copilot_client.start()
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
    await copilot_client.stop()
    logger.info("Shutdown complete")


# ── App ──────────────────────────────────────────────────────

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description=APP_DESCRIPTION,
    lifespan=lifespan,
)

app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

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
):
    """Return the template catalog from the database."""
    from src.database import get_all_templates

    try:
        templates = await get_all_templates(category=category, fmt=fmt)
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


# ── Service Approval Artifacts (3-Gate Workflow) ──────────────

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
    """Run deployment validation with auto-healing.

    On What-If failure the Copilot SDK automatically rewrites the ARM template
    (and/or policy) using the error output, saves the updated artifact, and
    retries — up to MAX_HEAL_ATTEMPTS times.

    Streams NDJSON progress events including per-iteration status.
    """
    from src.database import (
        get_service, get_service_artifacts, save_service_artifact,
        promote_service_after_validation, fail_service_validation,
    )

    MAX_HEAL_ATTEMPTS = 5

    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    # Verify both gates are approved
    artifacts = await get_service_artifacts(service_id)
    if not artifacts["_summary"]["all_approved"]:
        raise HTTPException(
            status_code=400,
            detail="Both gates must be approved before running deployment validation",
        )

    # Get the ARM template content
    template_artifact = artifacts.get("template", {})
    template_content = template_artifact.get("content", "").strip()
    if not template_content:
        raise HTTPException(status_code=400, detail="ARM template artifact has no content")

    # Parse optional config from request body
    try:
        body = await request.json()
    except Exception:
        body = {}

    region = body.get("region", "eastus2")
    rg_name = f"infraforge-validation-{service_id.replace('/', '-').replace('.', '-').lower()}"[:90]

    # ── helpers ───────────────────────────────────────────────

    async def _copilot_fix_artifact(
        artifact_type: str,
        current_content: str,
        error_message: str,
    ) -> str:
        """Ask the Copilot SDK to fix an artifact based on an error message."""
        if artifact_type == "template":
            fix_prompt = (
                "The following ARM template was rejected by Azure ARM What-If validation.\n\n"
                f"--- ERROR ---\n{error_message}\n--- END ERROR ---\n\n"
                f"--- CURRENT TEMPLATE ---\n{current_content}\n--- END TEMPLATE ---\n\n"
                "Fix the template so it passes What-If validation. "
                "Return ONLY the corrected raw JSON — no markdown fences, no explanation. "
                "Keep the same resource intent. Fix schema issues, missing required "
                "properties, invalid API versions, and structural problems."
            )
        else:
            fix_prompt = (
                "The following Azure Policy JSON definition has a syntax or structural error.\n\n"
                f"--- ERROR ---\n{error_message}\n--- END ERROR ---\n\n"
                f"--- CURRENT POLICY ---\n{current_content}\n--- END POLICY ---\n\n"
                "Fix the policy so it is valid, deployable Azure Policy JSON. "
                "Return ONLY the corrected raw JSON — no markdown fences, no explanation."
            )

        session = None
        try:
            session = await copilot_client.create_session({
                "model": COPILOT_MODEL,
                "streaming": True,
                "tools": [],
                "system_message": {
                    "content": (
                        "You are an Azure infrastructure expert specializing in "
                        "debugging and fixing ARM templates and Azure Policy definitions. "
                        "Return ONLY raw JSON — no markdown, no code fences, no explanation."
                    )
                },
            })

            chunks: list[str] = []
            done_event = asyncio.Event()

            def on_event(event):
                try:
                    if event.type.value == "assistant.message_delta":
                        chunks.append(event.data.delta_content or "")
                    elif event.type.value in ("assistant.message", "session.idle"):
                        done_event.set()
                except Exception:
                    done_event.set()

            unsubscribe = session.on(on_event)
            try:
                await session.send({"prompt": fix_prompt})
                await asyncio.wait_for(done_event.wait(), timeout=90)
            finally:
                unsubscribe()

            fixed = "".join(chunks).strip()

            # Strip markdown fences if the model wrapped them
            if fixed.startswith("```"):
                lines = fixed.split("\n")
                lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                fixed = "\n".join(lines).strip()

            return fixed

        finally:
            if session:
                try:
                    await session.destroy()
                except Exception:
                    pass

    # ── main streaming generator ──────────────────────────────

    async def stream_validation():
        """Run What-If with auto-healing loop."""
        nonlocal template_content
        current_template = template_content

        try:
            for attempt in range(1, MAX_HEAL_ATTEMPTS + 1):
                is_last = attempt == MAX_HEAL_ATTEMPTS
                base_progress = (attempt - 1) / MAX_HEAL_ATTEMPTS

                yield json.dumps({
                    "type": "iteration_start",
                    "attempt": attempt,
                    "max_attempts": MAX_HEAL_ATTEMPTS,
                    "detail": f"Attempt {attempt}/{MAX_HEAL_ATTEMPTS} — parsing ARM template…",
                    "progress": base_progress + 0.02,
                }) + "\n"

                # ── Parse JSON ────────────────────────────────
                try:
                    template_json = json.loads(current_template)
                except json.JSONDecodeError as parse_err:
                    error_msg = f"ARM template is not valid JSON: {parse_err}"

                    if is_last:
                        await fail_service_validation(service_id, error_msg)
                        yield json.dumps({
                            "type": "error",
                            "phase": "parsing",
                            "attempt": attempt,
                            "detail": f"Attempt {attempt} — {error_msg}",
                        }) + "\n"
                        return

                    yield json.dumps({
                        "type": "healing",
                        "phase": "fixing_template",
                        "attempt": attempt,
                        "detail": f"Attempt {attempt} — JSON parse error, asking AI to fix template…",
                        "error": error_msg,
                        "progress": base_progress + 0.05,
                    }) + "\n"

                    current_template = await _copilot_fix_artifact("template", current_template, error_msg)
                    await save_service_artifact(
                        service_id, "template",
                        content=current_template,
                        status="approved",
                        notes=f"Auto-healed (attempt {attempt}): fixed JSON parse error",
                    )

                    yield json.dumps({
                        "type": "healing_done",
                        "phase": "template_fixed",
                        "attempt": attempt,
                        "detail": f"Attempt {attempt} — AI rewrote template, retrying…",
                        "progress": base_progress + 0.08,
                    }) + "\n"
                    continue  # next attempt

                # ── What-If ───────────────────────────────────
                yield json.dumps({
                    "type": "progress",
                    "phase": "what_if",
                    "attempt": attempt,
                    "detail": f"Attempt {attempt} — running What-If against Azure ({region})…",
                    "progress": base_progress + 0.04,
                }) + "\n"

                try:
                    from src.tools.deploy_engine import run_what_if

                    what_if_result = await run_what_if(
                        resource_group=rg_name,
                        template=template_json,
                        parameters={},
                        region=region,
                    )
                except Exception as wif_exc:
                    what_if_result = {"status": "error", "errors": [str(wif_exc)]}

                if what_if_result.get("status") == "success":
                    # ── What-If passed ────────────────────────
                    yield json.dumps({
                        "type": "progress",
                        "phase": "what_if_complete",
                        "attempt": attempt,
                        "detail": f"Attempt {attempt} — What-If passed: {what_if_result.get('total_changes', 0)} resource(s)",
                        "progress": base_progress + 0.07,
                        "result": what_if_result,
                    }) + "\n"

                    # Validate policy JSON
                    yield json.dumps({
                        "type": "progress",
                        "phase": "policy_check",
                        "attempt": attempt,
                        "detail": "Validating policy definition format…",
                        "progress": base_progress + 0.08,
                    }) + "\n"

                    policy_content = (artifacts.get("policy", {}).get("content") or "").strip()
                    if policy_content:
                        try:
                            json.loads(policy_content)
                        except json.JSONDecodeError as pe:
                            policy_err = f"Policy definition is not valid JSON: {pe}"
                            # Try to auto-heal the policy too
                            if not is_last:
                                yield json.dumps({
                                    "type": "healing",
                                    "phase": "fixing_policy",
                                    "attempt": attempt,
                                    "detail": f"Policy JSON error — asking AI to fix…",
                                    "error": policy_err,
                                    "progress": base_progress + 0.085,
                                }) + "\n"

                                fixed_policy = await _copilot_fix_artifact("policy", policy_content, policy_err)
                                await save_service_artifact(
                                    service_id, "policy",
                                    content=fixed_policy,
                                    status="approved",
                                    notes=f"Auto-healed (attempt {attempt}): fixed policy JSON",
                                )
                                # Re-fetch artifacts so the next iteration uses the fixed version
                                artifacts["policy"]["content"] = fixed_policy

                                yield json.dumps({
                                    "type": "healing_done",
                                    "phase": "policy_fixed",
                                    "attempt": attempt,
                                    "detail": "AI rewrote policy, continuing…",
                                    "progress": base_progress + 0.09,
                                }) + "\n"

                                # Policy fixed, re-validate inline
                                try:
                                    json.loads(fixed_policy)
                                except json.JSONDecodeError:
                                    continue  # will try again next iteration

                            else:
                                await fail_service_validation(service_id, policy_err)
                                yield json.dumps({
                                    "type": "error",
                                    "phase": "policy_check",
                                    "attempt": attempt,
                                    "detail": policy_err,
                                }) + "\n"
                                return

                    # ── All validation passed ─────────────────
                    yield json.dumps({
                        "type": "progress",
                        "phase": "promoting",
                        "attempt": attempt,
                        "detail": "All validation passed — approving service…",
                        "progress": 0.95,
                    }) + "\n"

                    await promote_service_after_validation(service_id, what_if_result)

                    yield json.dumps({
                        "type": "done",
                        "phase": "approved",
                        "attempt": attempt,
                        "total_attempts": attempt,
                        "detail": f"🎉 {svc['name']} approved! Passed on attempt {attempt}{'.' if attempt == 1 else f' after {attempt - 1} auto-fix(es).'}",
                        "progress": 1.0,
                        "what_if_summary": what_if_result.get("change_counts", {}),
                        "total_changes": what_if_result.get("total_changes", 0),
                    }) + "\n"
                    return  # success — exit the loop

                # ── What-If FAILED ────────────────────────────
                errors = what_if_result.get("errors", [])
                error_msg = "; ".join(str(e) for e in errors) if errors else "Unknown What-If error"

                if is_last:
                    full_error = f"What-If failed after {MAX_HEAL_ATTEMPTS} attempts: {error_msg}"
                    await fail_service_validation(service_id, full_error)
                    yield json.dumps({
                        "type": "error",
                        "phase": "what_if",
                        "attempt": attempt,
                        "detail": full_error,
                        "result": what_if_result,
                    }) + "\n"
                    return

                yield json.dumps({
                    "type": "healing",
                    "phase": "fixing_template",
                    "attempt": attempt,
                    "detail": f"Attempt {attempt} — What-If failed, asking AI to fix template…",
                    "error": error_msg,
                    "progress": base_progress + 0.06,
                }) + "\n"

                # Ask Copilot SDK to fix the template
                current_template = await _copilot_fix_artifact("template", current_template, error_msg)

                # Persist the fixed template
                await save_service_artifact(
                    service_id, "template",
                    content=current_template,
                    status="approved",
                    notes=f"Auto-healed (attempt {attempt}): {error_msg[:200]}",
                )

                yield json.dumps({
                    "type": "healing_done",
                    "phase": "template_fixed",
                    "attempt": attempt,
                    "detail": f"Attempt {attempt} — AI rewrote template, retrying…",
                    "progress": base_progress + 0.08,
                }) + "\n"

                # Loop continues to next attempt

        except Exception as e:
            logger.error(f"Deployment validation failed for {service_id}: {e}")
            try:
                await fail_service_validation(service_id, str(e))
            except Exception:
                pass
            yield json.dumps({
                "type": "error",
                "phase": "unknown",
                "detail": str(e),
            }) + "\n"

    return StreamingResponse(
        stream_validation(),
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
        session = None
        try:
            # Create a temporary Copilot session for this generation
            session = await copilot_client.create_session({
                "model": COPILOT_MODEL,
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
        personalized_system_message = (
            SYSTEM_MESSAGE + "\n" + user_context.to_prompt_context()
        )

        tools = get_all_tools()
        copilot_session = await copilot_client.create_session({
            "model": COPILOT_MODEL,
            "streaming": True,
            "tools": tools,
            "system_message": {"content": personalized_system_message},
        })

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
