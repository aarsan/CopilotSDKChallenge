"""
InfraForge — Deployment Router

Extracted from web.py. Contains:
  - Deployment API (list, get, stream, teardown)
  - Azure Managed Resources API (resource groups)
  - Orchestration Processes API (process definitions, playbooks, pipeline info)
"""

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger("infraforge.web")

router = APIRouter()

# ── Deployment API ────────────────────────────────────────────

@router.get("/api/deployments")
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


@router.get("/api/deployments/{deployment_id}")
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


@router.get("/api/deployments/{deployment_id}/stream")
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


@router.post("/api/deployments/{deployment_id}/teardown")
async def teardown_deployment_endpoint(deployment_id: str):
    """Tear down a deployment by deleting its resource group."""
    from src.tools.deploy_engine import execute_teardown

    result = await execute_teardown(deployment_id=deployment_id)

    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result["error"])

    return JSONResponse(result)


# ── Azure Managed Resources API ──────────────────────────────

@router.get("/api/azure/resource-groups")
async def list_azure_resource_groups_endpoint():
    """List all resource groups in the Azure subscription, annotated with
    InfraForge management info. Cross-references with deployment DB records."""
    from src.tools.deploy_engine import list_azure_resource_groups
    from src.database import get_deployments

    try:
        rgs = await list_azure_resource_groups()
        deployments = await get_deployments()

        # Build lookup: resource_group → deployment info
        deploy_map: dict[str, dict] = {}
        for d in deployments:
            rg = d.get("resource_group", "")
            if rg and rg not in deploy_map:
                deploy_map[rg] = {
                    "deployment_id": d.get("deployment_id"),
                    "status": d.get("status"),
                    "deployment_name": d.get("deployment_name"),
                    "template_id": d.get("template_id"),
                    "template_name": d.get("template_name"),
                    "template_version": d.get("template_version", 0),
                    "template_semver": d.get("template_semver", ""),
                    "started_at": d.get("started_at"),
                    "torn_down_at": d.get("torn_down_at"),
                }

        # Enrich RG data with deployment info
        for rg in rgs:
            dep_info = deploy_map.get(rg["name"])
            if dep_info:
                rg["deployment"] = dep_info
                rg["managed_by_infraforge"] = True  # has a deployment record
                if rg["rg_type"] == "unknown":
                    rg["rg_type"] = "deployment"

        managed = [r for r in rgs if r["managed_by_infraforge"]]
        unmanaged = [r for r in rgs if not r["managed_by_infraforge"]]

        return JSONResponse({
            "managed": managed,
            "unmanaged": unmanaged,
            "total": len(rgs),
            "managed_count": len(managed),
            "subscription_id": os.environ.get("AZURE_SUBSCRIPTION_ID", "")[:12] + "…",
        })
    except Exception as e:
        logger.error(f"Failed to list Azure resource groups: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to query Azure: {str(e)[:200]}")


@router.delete("/api/azure/resource-groups/{rg_name}")
async def delete_azure_resource_group_endpoint(rg_name: str):
    """Delete a single Azure resource group."""
    from src.tools.deploy_engine import delete_resource_group

    result = await delete_resource_group(rg_name)
    if result["status"] == "error":
        raise HTTPException(status_code=500, detail=result["error"])
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail=result["message"])
    return JSONResponse(result)


@router.post("/api/azure/resource-groups/cleanup")
async def cleanup_azure_resource_groups_endpoint(request: Request):
    """Bulk-delete InfraForge-managed resource groups.

    Body (optional):
      rg_names: list[str]  — specific RGs to delete (default: all validation RGs)
      type: str            — 'validation' | 'all' (default: 'validation')
    """
    from src.tools.deploy_engine import list_azure_resource_groups, delete_resource_group

    try:
        body = await request.json()
    except Exception:
        body = {}

    rg_names = body.get("rg_names")
    cleanup_type = body.get("type", "validation")

    if not rg_names:
        # Auto-discover: list managed RGs and filter
        all_rgs = await list_azure_resource_groups()
        if cleanup_type == "all":
            targets = [r["name"] for r in all_rgs if r["managed_by_infraforge"]]
        else:
            targets = [r["name"] for r in all_rgs if r["rg_type"] == "validation"]
    else:
        targets = rg_names

    if not targets:
        return JSONResponse({"deleted": [], "failed": [], "message": "No resource groups to clean up."})

    results = []
    for name in targets:
        result = await delete_resource_group(name)
        results.append({"name": name, **result})

    deleted = [r for r in results if r["status"] == "deleted"]
    failed = [r for r in results if r["status"] == "error"]

    return JSONResponse({
        "deleted": deleted,
        "failed": failed,
        "total_deleted": len(deleted),
        "total_failed": len(failed),
        "message": f"Deleted {len(deleted)} resource group(s)" + (f", {len(failed)} failed" if failed else ""),
    })


# ── Orchestration Processes API ──────────────────────────────

@router.get("/api/orchestration/processes")
async def list_orchestration_processes():
    """List all orchestration processes and their steps."""
    from src.database import get_all_processes
    processes = await get_all_processes()
    return JSONResponse({"processes": processes, "total": len(processes)})


@router.get("/api/orchestration/processes/{process_id}")
async def get_orchestration_process(process_id: str):
    """Get a specific orchestration process with its steps."""
    from src.database import get_process
    proc = await get_process(process_id)
    if not proc:
        raise HTTPException(status_code=404, detail=f"Process '{process_id}' not found")
    return JSONResponse(proc)


@router.get("/api/orchestration/processes/{process_id}/playbook")
async def get_orchestration_playbook(process_id: str):
    """Get a human/LLM-readable playbook for a process."""
    from src.orchestrator import get_process_playbook
    text = await get_process_playbook(process_id)
    return JSONResponse({"process_id": process_id, "playbook": text})


@router.post("/api/orchestration/processes/refresh")
async def refresh_orchestration_processes_endpoint():
    """Re-seed orchestration process definitions from the Python source of truth.

    Drops all existing process/step definitions and re-creates them from
    the seed data in database.py.  Use after updating process definitions
    in code so the running DB picks up the changes.
    """
    from src.database import refresh_orchestration_processes
    count = await refresh_orchestration_processes()
    return JSONResponse({
        "status": "ok",
        "message": f"Refreshed {count} orchestration process(es)",
        "count": count,
    })


@router.get("/api/orchestration/pipeline-info")
async def get_pipeline_info():
    """Get pipeline framework status — registered handlers, process definitions,
    and handler coverage for each process.

    This shows which DB-defined step actions have Python handlers registered
    and which are still unimplemented.
    """
    from src.database import get_all_processes

    processes = await get_all_processes()

    # For now, report which actions exist in each process.
    # Once pipelines are migrated, this will also show handler registration.
    result = []
    for proc in processes:
        step_actions = [s.get("action", "") for s in proc.get("steps", [])]
        result.append({
            "id": proc["id"],
            "name": proc["name"],
            "step_count": len(proc.get("steps", [])),
            "actions": step_actions,
            "enabled": proc.get("enabled", True),
        })

    return JSONResponse({
        "processes": result,
        "total": len(result),
        "framework": "src.pipeline.PipelineRunner",
    })
