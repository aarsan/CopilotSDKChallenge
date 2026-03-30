"""
InfraForge — Org Processes Router

Endpoints for managing user-defined processes and their steps.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from src.database import (
    create_org_process,
    get_org_processes,
    get_org_process,
    update_org_process,
    delete_org_process,
    get_process_steps,
    add_process_step,
    update_process_step,
    delete_process_step,
)

logger = logging.getLogger("infraforge.web")

router = APIRouter()


# ══════════════════════════════════════════════════════════════
# PROCESSES
# ══════════════════════════════════════════════════════════════

@router.get("/api/processes")
async def list_processes():
    """Return all org processes."""
    procs = await get_org_processes()
    return JSONResponse(procs)


@router.get("/api/processes/{proc_id}")
async def get_process(proc_id: str):
    """Return a single process with its steps."""
    proc = await get_org_process(proc_id)
    if not proc:
        raise HTTPException(status_code=404, detail="Process not found")
    steps = await get_process_steps(proc_id)
    proc["steps"] = steps
    return JSONResponse(proc)


@router.post("/api/processes")
async def create_process(request: Request):
    """Create a new process."""
    body = await request.json()
    if not body.get("name"):
        raise HTTPException(status_code=400, detail="name is required")
    proc_id = await create_org_process(body)
    proc = await get_org_process(proc_id)
    return JSONResponse(proc, status_code=201)


@router.put("/api/processes/{proc_id}")
async def update_process(proc_id: str, request: Request):
    """Update a process."""
    body = await request.json()
    ok = await update_org_process(proc_id, body)
    if not ok:
        raise HTTPException(status_code=404, detail="Process not found")
    proc = await get_org_process(proc_id)
    return JSONResponse(proc)


@router.delete("/api/processes/{proc_id}")
async def delete_process_endpoint(proc_id: str):
    """Delete a process and all its steps."""
    ok = await delete_org_process(proc_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Process not found")
    return JSONResponse({"deleted": True})


# ══════════════════════════════════════════════════════════════
# PROCESS STEPS
# ══════════════════════════════════════════════════════════════

@router.get("/api/processes/{proc_id}/steps")
async def list_steps(proc_id: str):
    """Return steps for a process."""
    proc = await get_org_process(proc_id)
    if not proc:
        raise HTTPException(status_code=404, detail="Process not found")
    steps = await get_process_steps(proc_id)
    return JSONResponse(steps)


@router.post("/api/processes/{proc_id}/steps")
async def create_step(proc_id: str, request: Request):
    """Add a step to a process."""
    proc = await get_org_process(proc_id)
    if not proc:
        raise HTTPException(status_code=404, detail="Process not found")
    body = await request.json()
    step_id = await add_process_step(proc_id, body)
    if step_id is None:
        raise HTTPException(status_code=500, detail="Failed to create step")
    steps = await get_process_steps(proc_id)
    return JSONResponse(steps, status_code=201)


@router.put("/api/processes/{proc_id}/steps/{step_id}")
async def update_step_endpoint(proc_id: str, step_id: int, request: Request):
    """Update a process step."""
    body = await request.json()
    ok = await update_process_step(step_id, body)
    if not ok:
        raise HTTPException(status_code=404, detail="Step not found")
    steps = await get_process_steps(proc_id)
    return JSONResponse(steps)


@router.delete("/api/processes/{proc_id}/steps/{step_id}")
async def delete_step_endpoint(proc_id: str, step_id: int):
    """Delete a process step."""
    ok = await delete_process_step(step_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Step not found")
    steps = await get_process_steps(proc_id)
    return JSONResponse(steps)
