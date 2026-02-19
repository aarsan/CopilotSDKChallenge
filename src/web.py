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
    logger.info("Initializing Fabric SQL Database...")
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


@app.delete("/api/catalog/templates/{template_id}")
async def delete_template_endpoint(template_id: str):
    """Remove a template from the catalog."""
    from src.database import delete_template

    deleted = await delete_template(template_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Template not found")
    return JSONResponse({"status": "ok", "deleted": template_id})


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

                # Persist to Fabric SQL Database
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
