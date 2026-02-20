"""
InfraForge — Organization Standards API

REST endpoints for managing organization-wide governance standards.
All data stored in Azure SQL Database (org_standards + org_standards_history tables).
"""

import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from src.standards import (
    get_all_standards,
    get_standard,
    create_standard,
    update_standard,
    delete_standard,
    get_standard_history,
    get_standards_categories,
    get_standards_for_service,
    build_policy_generation_context,
    build_arm_generation_context,
)

logger = logging.getLogger("infraforge.standards_api")

router = APIRouter(prefix="/api/standards", tags=["standards"])


# ── List all standards ────────────────────────────────────────

@router.get("")
async def list_standards(category: str = None, enabled_only: bool = False):
    """Get all organization standards, optionally filtered by category."""
    standards = await get_all_standards(category=category, enabled_only=enabled_only)
    return JSONResponse({"standards": standards, "count": len(standards)})


# ── Get distinct categories ───────────────────────────────────

@router.get("/categories")
async def list_categories():
    """Get all distinct standard categories."""
    categories = await get_standards_categories()
    return JSONResponse({"categories": categories})


# ── Get a single standard ────────────────────────────────────

@router.get("/{standard_id}")
async def get_one_standard(standard_id: str):
    """Get a single standard by ID."""
    std = await get_standard(standard_id)
    if not std:
        raise HTTPException(status_code=404, detail="Standard not found")
    return JSONResponse(std)


# ── Get version history ──────────────────────────────────────

@router.get("/{standard_id}/history")
async def get_history(standard_id: str):
    """Get version history for a standard."""
    std = await get_standard(standard_id)
    if not std:
        raise HTTPException(status_code=404, detail="Standard not found")
    history = await get_standard_history(standard_id)
    return JSONResponse({"standard_id": standard_id, "versions": history})


# ── Create a new standard ────────────────────────────────────

@router.post("")
async def create_new_standard(request: dict):
    """Create a new organization standard.

    Body: { name, description, category, severity, scope, rule, enabled? }
    """
    if not request.get("name"):
        raise HTTPException(status_code=400, detail="name is required")
    if not request.get("category"):
        raise HTTPException(status_code=400, detail="category is required")

    try:
        std = await create_standard(
            request,
            created_by=request.get("created_by", "platform-team"),
        )
        return JSONResponse(std, status_code=201)
    except Exception as e:
        logger.error(f"Failed to create standard: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Update an existing standard ──────────────────────────────

@router.put("/{standard_id}")
async def update_existing_standard(standard_id: str, request: dict):
    """Update a standard. Creates a version history entry.

    Body: { name?, description?, category?, severity?, scope?, rule?, enabled?, change_reason? }
    """
    result = await update_standard(
        standard_id,
        updates=request,
        changed_by=request.get("changed_by", "platform-team"),
        change_reason=request.get("change_reason", ""),
    )
    if not result:
        raise HTTPException(status_code=404, detail="Standard not found")
    return JSONResponse(result)


# ── Delete a standard ────────────────────────────────────────

@router.delete("/{standard_id}")
async def delete_existing_standard(standard_id: str):
    """Delete a standard and its version history."""
    deleted = await delete_standard(standard_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Standard not found")
    return JSONResponse({"deleted": True, "id": standard_id})


# ── Standards for a specific service ─────────────────────────

@router.get("/for-service/{service_id:path}")
async def standards_for_service(service_id: str):
    """Get all enabled standards that apply to a given service resource type."""
    standards = await get_standards_for_service(service_id)
    return JSONResponse({
        "service_id": service_id,
        "standards": standards,
        "count": len(standards),
    })


# ── Prompt context endpoints (used by onboarding) ────────────

@router.get("/context/policy/{service_id:path}")
async def policy_context(service_id: str):
    """Get the policy generation prompt context for a service."""
    context = await build_policy_generation_context(service_id)
    return JSONResponse({"service_id": service_id, "context": context})


@router.get("/context/arm/{service_id:path}")
async def arm_context(service_id: str):
    """Get the ARM template generation prompt context for a service."""
    context = await build_arm_generation_context(service_id)
    return JSONResponse({"service_id": service_id, "context": context})
