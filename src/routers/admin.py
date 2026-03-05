"""
InfraForge — Admin Router

Extracted from web.py. Contains:
  - Admin: Backup & Restore API
  - Approval Management API
  - Governance API
  - Fabric Analytics API
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

from src.database import get_approval_requests, update_approval_request

logger = logging.getLogger("infraforge.web")

router = APIRouter()

# ── Admin: Backup & Restore API ───────────────────────────────

@router.post("/api/admin/backup")
async def create_backup_endpoint(request: Request):
    """Create a database backup and return it as JSON download."""
    from scripts.backup_restore import create_backup, save_backup_to_file

    try:
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass

        include_sessions = body.get("include_sessions", False)
        save_to_disk = body.get("save_to_disk", True)
        note = body.get("note", "")

        backup = await create_backup(
            include_sessions=include_sessions, note=note
        )

        # Optionally save to disk
        filepath = None
        if save_to_disk:
            filepath = await save_backup_to_file(
                include_sessions=include_sessions, note=note
            )

        return JSONResponse({
            "status": "ok",
            "metadata": backup["metadata"],
            "filepath": filepath,
            "backup": backup if not save_to_disk else None,
        })
    except Exception as e:
        logger.error(f"Backup failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Backup failed: {str(e)[:200]}")


@router.get("/api/admin/backup/download")
async def download_backup_endpoint(
    include_sessions: bool = False,
    note: str = "",
):
    """Create and download a backup as a JSON file."""
    from scripts.backup_restore import create_backup
    from starlette.responses import Response

    try:
        backup = await create_backup(
            include_sessions=include_sessions, note=note
        )
        content = json.dumps(backup, indent=2, default=str, ensure_ascii=False)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"infraforge_backup_{timestamp}.json"

        return Response(
            content=content,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
    except Exception as e:
        logger.error(f"Backup download failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Backup failed: {str(e)[:200]}")


@router.post("/api/admin/restore")
async def restore_backup_endpoint(request: Request):
    """Restore the database from a JSON backup.

    Accepts the backup JSON as the request body.
    Query params:
      mode: 'replace' (default) or 'merge'
      skip_sessions: true (default) — skip user_sessions and chat_messages
    """
    from scripts.backup_restore import restore_from_backup

    try:
        body = await request.json()

        # The body might be the backup itself, or a wrapper with options
        if "tables" in body:
            backup_data = body
            mode = request.query_params.get("mode", "replace")
        else:
            backup_data = body.get("backup", body)
            mode = body.get("mode", request.query_params.get("mode", "replace"))

        if "tables" not in backup_data:
            raise HTTPException(
                status_code=400,
                detail="Invalid backup format: missing 'tables' key",
            )

        skip_sessions = request.query_params.get("skip_sessions", "true") == "true"
        skip_tables = []
        if skip_sessions:
            skip_tables = ["user_sessions", "chat_messages"]

        summary = await restore_from_backup(
            backup_data, mode=mode, skip_tables=skip_tables
        )

        return JSONResponse({
            "status": "ok",
            "summary": summary,
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Restore failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Restore failed: {str(e)[:200]}")


@router.get("/api/admin/backups")
async def list_backups_endpoint():
    """List available backup files on disk."""
    from scripts.backup_restore import list_backup_files

    try:
        backups = list_backup_files()
        return JSONResponse({"backups": backups, "total": len(backups)})
    except Exception as e:
        logger.error(f"List backups failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)[:200])


@router.post("/api/admin/restore/file")
async def restore_from_file_endpoint(request: Request):
    """Restore from a backup file on disk.

    Body: { "filepath": "backups/infraforge_backup_xxx.json", "mode": "replace" }
    """
    from scripts.backup_restore import restore_from_file

    try:
        body = await request.json()
        filepath = body.get("filepath", "")
        mode = body.get("mode", "replace")
        if not filepath:
            raise HTTPException(status_code=400, detail="filepath is required")

        summary = await restore_from_file(filepath, mode=mode)
        return JSONResponse({"status": "ok", "summary": summary})
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Restore from file failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Restore failed: {str(e)[:200]}")


# ── Approval Management API ──────────────────────────────────

@router.get("/api/approvals")
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


@router.get("/api/approvals/{request_id}")
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


@router.post("/api/approvals/{request_id}/review")
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


@router.post("/api/policy-exception-requests")
async def submit_policy_exception_request(request: Request):
    """Submit a policy exception request when a modification is blocked by policy.

    Stores the request in the approval_requests table with a PER- prefix
    so admins can review and potentially grant policy exceptions.
    """
    from src.database import save_approval_request

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    user_request = body.get("user_request", "").strip()
    policy_rules = body.get("policy_rules", [])
    justification = body.get("justification", "").strip()
    template_id = body.get("template_id", "")
    template_name = body.get("template_name", "")

    if not user_request:
        raise HTTPException(status_code=400, detail="user_request is required")
    if not justification:
        raise HTTPException(status_code=400, detail="justification is required")

    # Build a structured business justification
    rules_text = "\n".join(f"  - {r}" for r in policy_rules) if policy_rules else "  (no specific rules cited)"
    biz_justification = (
        f"POLICY EXCEPTION REQUEST\n"
        f"========================\n"
        f"Original request: {user_request}\n\n"
        f"Blocked by policies:\n{rules_text}\n\n"
        f"Business justification:\n{justification}\n\n"
        f"Template: {template_name or template_id or 'N/A'}"
    )

    from datetime import datetime, timezone
    request_id = f"PER-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

    await save_approval_request({
        "id": request_id,
        "service_name": f"Policy Exception: {', '.join(policy_rules[:3]) or 'governance'}",
        "service_resource_type": template_id or "policy-exception",
        "current_status": "policy_exception",
        "risk_tier": "high",
        "business_justification": biz_justification,
        "project_name": template_name or "Template Modification",
        "environment": "production",
        "status": "submitted",
    })

    return JSONResponse({
        "request_id": request_id,
        "status": "submitted",
        "message": f"Policy exception request {request_id} submitted for platform team review. "
                   "Typical review time: 1–3 business days for policy exceptions.",
    })


# ── Governance API ───────────────────────────────────────────

@router.get("/api/governance/security-standards")
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


@router.get("/api/governance/compliance-frameworks")
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


@router.get("/api/governance/policies")
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


# ── Fabric Analytics API ─────────────────────────────────────


@router.get("/api/analytics/dashboard")
async def get_analytics_dashboard():
    """Get full analytics dashboard data.

    Aggregates pipeline, governance, service, deployment, and compliance
    analytics from SQL Server.  Data is also synced to Microsoft Fabric
    OneLake for Power BI / Semantic Model consumption.
    """
    from src.fabric import AnalyticsEngine
    try:
        dashboard = await AnalyticsEngine.get_full_dashboard()
        return JSONResponse(dashboard)
    except Exception as e:
        logger.error(f"Analytics dashboard error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/analytics/pipelines")
async def get_pipeline_analytics():
    """Pipeline execution trends and success rates."""
    from src.fabric import AnalyticsEngine
    try:
        return JSONResponse(await AnalyticsEngine.get_pipeline_analytics())
    except Exception as e:
        logger.error(f"Pipeline analytics error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/analytics/governance")
async def get_governance_analytics():
    """Governance review verdict distribution and trends."""
    from src.fabric import AnalyticsEngine
    try:
        return JSONResponse(await AnalyticsEngine.get_governance_analytics())
    except Exception as e:
        logger.error(f"Governance analytics error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/analytics/services")
async def get_service_analytics():
    """Service catalog adoption metrics."""
    from src.fabric import AnalyticsEngine
    try:
        return JSONResponse(await AnalyticsEngine.get_service_analytics())
    except Exception as e:
        logger.error(f"Service analytics error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/analytics/deployments")
async def get_deployment_analytics():
    """Deployment success rates and regional distribution."""
    from src.fabric import AnalyticsEngine
    try:
        return JSONResponse(await AnalyticsEngine.get_deployment_analytics())
    except Exception as e:
        logger.error(f"Deployment analytics error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/analytics/compliance")
async def get_compliance_analytics():
    """Compliance assessment score trends."""
    from src.fabric import AnalyticsEngine
    try:
        return JSONResponse(await AnalyticsEngine.get_compliance_analytics())
    except Exception as e:
        logger.error(f"Compliance analytics error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/fabric/sync")
async def trigger_fabric_sync():
    """Trigger a data sync from SQL Server to Microsoft Fabric OneLake.

    Exports denormalized analytics tables as CSV to OneLake DFS,
    making data available for Fabric Semantic Models and Power BI.
    """
    from src.fabric import get_sync_engine
    engine = get_sync_engine()
    if not engine:
        return JSONResponse({
            "status": "not_configured",
            "message": "Fabric is not configured. Set FABRIC_WORKSPACE_ID and FABRIC_ONELAKE_DFS_ENDPOINT in .env",
        }, status_code=503)
    try:
        result = await engine.sync_all()
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Fabric sync error: {e}")
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.post("/api/fabric/sync/{table_name}")
async def trigger_fabric_sync_table(table_name: str):
    """Sync a single table to Fabric OneLake."""
    from src.fabric import get_sync_engine, SYNC_TABLES
    if table_name not in SYNC_TABLES:
        return JSONResponse(
            {"error": f"Unknown table: {table_name}. Available: {list(SYNC_TABLES.keys())}"},
            status_code=400,
        )
    engine = get_sync_engine()
    if not engine:
        return JSONResponse({"status": "not_configured"}, status_code=503)
    try:
        result = await engine.sync_table(table_name)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Fabric sync error for {table_name}: {e}")
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.get("/api/fabric/status")
async def get_fabric_status():
    """Get Fabric integration status and health."""
    from src.fabric import get_fabric_client, get_sync_engine, SYNC_TABLES
    client = get_fabric_client()
    if not client:
        return JSONResponse({
            "configured": False,
            "message": "Fabric integration not configured",
        })

    health = await client.health_check()
    engine = get_sync_engine()
    return JSONResponse({
        "configured": True,
        "health": health,
        "sync": {
            "last_sync": engine.last_sync if engine else None,
            "history": engine.sync_history if engine else [],
            "tables": list(SYNC_TABLES.keys()),
        },
    })
