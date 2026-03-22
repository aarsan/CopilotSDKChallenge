"""
InfraForge — Admin Router

Extracted from web.py. Contains:
  - Admin: Backup & Restore API
  - Approval Management API
  - Governance API
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

from src.config import get_enforcement_mode, set_enforcement_mode
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
        standards = await db_get_standards(category=category, enabled_only=False)
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
        frameworks = await db_get_frameworks(enabled_only=False)
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
        policies = await db_get_policies(category=category, enabled_only=False)
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



# ── Toggle Governance Policy ──────────────────────────────────

@router.put("/api/governance/policies/{policy_id}")
async def toggle_governance_policy(policy_id: str, request: Request):
    """Enable or disable a single governance policy."""
    from src.database import get_governance_policies as db_get_policies, upsert_governance_policy

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    enabled = body.get("enabled")
    if enabled is None:
        raise HTTPException(status_code=400, detail="'enabled' field is required")

    all_policies = await db_get_policies(enabled_only=False)
    current = next((p for p in all_policies if p["id"] == policy_id), None)
    if not current:
        raise HTTPException(status_code=404, detail=f"Policy '{policy_id}' not found")

    current["enabled"] = bool(enabled)
    await upsert_governance_policy(current)
    action = "enabled" if enabled else "disabled"
    logger.info(f"Governance policy {policy_id} {action}")
    return JSONResponse({"status": action, "policy_id": policy_id, "enabled": bool(enabled)})


# ── Governance Enforcement Mode ───────────────────────────────

@router.get("/api/settings/enforcement-mode")
async def get_enforcement_mode_setting():
    """Return the current governance enforcement mode."""
    return JSONResponse({
        "enforcement_mode": get_enforcement_mode(),
        "options": ["enforce", "audit"],
        "descriptions": {
            "enforce": "Governance policies block deployments when violations are found",
            "audit": "Governance policies log findings but never block deployments (default)",
        },
    })


@router.put("/api/settings/enforcement-mode")
async def update_enforcement_mode_setting(request: Request):
    """Change the governance enforcement mode at runtime."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    mode = body.get("mode", "").strip().lower()
    if not set_enforcement_mode(mode):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode '{mode}'. Valid modes: enforce, audit",
        )

    logger.info(f"Governance enforcement mode changed to: {mode}")
    return JSONResponse({"enforcement_mode": mode, "status": "updated"})


# ══════════════════════════════════════════════════════════════
# AGENT PERFORMANCE & LEARNING — Admin Endpoints
# ══════════════════════════════════════════════════════════════

@router.post("/api/admin/agents/recalculate-scores")
async def recalculate_all_scores():
    """Recalculate performance scores for all agents."""
    from src.copilot_helpers import recalculate_all_agent_scores
    scores = await recalculate_all_agent_scores()
    return JSONResponse({"status": "ok", "scores": scores})


@router.post("/api/admin/agents/{agent_key}/recalculate-score")
async def recalculate_single_score(agent_key: str):
    """Recalculate performance score for a single agent."""
    from src.copilot_helpers import _async_recalculate_scores, _compute_scores
    await _async_recalculate_scores(agent_key)
    scores = _compute_scores(agent_key)
    return JSONResponse({"status": "ok", "agent": agent_key, "scores": scores})


@router.get("/api/admin/agents/{agent_key}/improvement-queue")
async def get_improvement_queue(agent_key: str):
    """List prompt improvement suggestions for an agent."""
    from src.database import get_prompt_improvements
    improvements = await get_prompt_improvements(agent_name=agent_key)
    return JSONResponse({"agent": agent_key, "improvements": improvements})


@router.post("/api/admin/agents/{agent_key}/apply-improvement")
async def apply_improvement(agent_key: str, request: Request):
    """Approve and apply a prompt improvement suggestion."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    improvement_id = body.get("improvement_id")
    if not improvement_id:
        raise HTTPException(status_code=400, detail="improvement_id is required")

    reviewed_by = body.get("reviewed_by", "admin")
    from src.copilot_helpers import apply_prompt_improvement
    success = await apply_prompt_improvement(improvement_id, reviewed_by=reviewed_by)
    if not success:
        raise HTTPException(status_code=404, detail="Improvement not found or could not be applied")

    return JSONResponse({"status": "ok", "improvement_id": improvement_id, "applied": True})


@router.post("/api/admin/agents/{agent_key}/reject-improvement")
async def reject_improvement(agent_key: str, request: Request):
    """Reject a prompt improvement suggestion."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    improvement_id = body.get("improvement_id")
    if not improvement_id:
        raise HTTPException(status_code=400, detail="improvement_id is required")

    reviewed_by = body.get("reviewed_by", "admin")
    from src.database import update_prompt_improvement
    await update_prompt_improvement(improvement_id, "rejected", reviewed_by)
    return JSONResponse({"status": "ok", "improvement_id": improvement_id, "rejected": True})


@router.post("/api/admin/agents/{agent_key}/generate-improvement")
async def trigger_improvement_generation(agent_key: str):
    """Manually trigger prompt improvement analysis for an agent."""
    from src.database import get_agent_misses
    from src.copilot_helpers import generate_prompt_improvement

    misses = await get_agent_misses(agent_name=agent_key, resolved=False, limit=50)
    if not misses:
        return JSONResponse({"status": "ok", "message": "No unresolved misses to analyze"})

    await generate_prompt_improvement(agent_key, misses)
    return JSONResponse({"status": "ok", "message": f"Improvement generated from {len(misses)} misses"})
