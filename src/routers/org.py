"""
InfraForge — Org Hierarchy & Agent Workforce Router

Endpoints for managing:
  - Org units (departments / teams / squads)
  - Agent definitions (create, update, delete, toggle)
  - Org chart (nested tree view)
  - Available tools listing
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from src.database import (
    create_org_unit,
    get_org_units,
    get_org_unit,
    update_org_unit,
    delete_org_unit,
    get_org_chart,
    create_agent_definition,
    get_all_agent_definitions,
    get_agent_definition,
    update_agent_definition,
    delete_agent_definition,
    get_chat_enabled_agents,
)
from src.agents import AGENTS, load_agents_from_db

logger = logging.getLogger("infraforge.web")

router = APIRouter()


# ══════════════════════════════════════════════════════════════
# ORG CHART
# ══════════════════════════════════════════════════════════════

@router.get("/api/org/chart")
async def get_org_chart_endpoint():
    """Return the full org chart as a nested tree."""
    chart = await get_org_chart()
    return JSONResponse(chart)


# ══════════════════════════════════════════════════════════════
# ORG UNITS
# ══════════════════════════════════════════════════════════════

@router.get("/api/org/units")
async def list_org_units():
    """Return all org units as a flat list."""
    units = await get_org_units()
    return JSONResponse(units)


@router.post("/api/org/units")
async def create_org_unit_endpoint(request: Request):
    """Create a new org unit."""
    body = await request.json()
    if not body.get("name"):
        raise HTTPException(status_code=400, detail="name is required")
    unit_id = await create_org_unit(body)
    unit = await get_org_unit(unit_id)
    return JSONResponse(unit, status_code=201)


@router.put("/api/org/units/{unit_id}")
async def update_org_unit_endpoint(unit_id: str, request: Request):
    """Update an org unit."""
    body = await request.json()
    ok = await update_org_unit(unit_id, body)
    if not ok:
        raise HTTPException(status_code=404, detail="Org unit not found")
    unit = await get_org_unit(unit_id)
    return JSONResponse(unit)


@router.delete("/api/org/units/{unit_id}")
async def delete_org_unit_endpoint(unit_id: str):
    """Delete an org unit (must have no children)."""
    ok = await delete_org_unit(unit_id)
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete: unit has child units. Move or delete children first.",
        )
    return JSONResponse({"deleted": True})


# ══════════════════════════════════════════════════════════════
# AGENT WORKFORCE
# ══════════════════════════════════════════════════════════════

@router.get("/api/org/agents")
async def list_agents():
    """Return all agent definitions with org columns."""
    agents = await get_all_agent_definitions()
    return JSONResponse(agents)


@router.get("/api/org/agents/chat-enabled")
async def list_chat_enabled_agents():
    """Return agents that are chat-enabled (for the chat selector)."""
    agents = await get_chat_enabled_agents()
    return JSONResponse(agents)


@router.get("/api/org/agents/{agent_id}")
async def get_agent_endpoint(agent_id: str):
    """Return a single agent definition."""
    agent = await get_agent_definition(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return JSONResponse(agent)


@router.post("/api/org/agents")
async def create_agent_endpoint(request: Request):
    """Create a new agent definition."""
    body = await request.json()
    if not body.get("name"):
        raise HTTPException(status_code=400, detail="name is required")
    if not body.get("system_prompt"):
        raise HTTPException(status_code=400, detail="system_prompt is required")

    agent_id = await create_agent_definition(body)

    # Reload the in-memory agent registry
    await load_agents_from_db()

    agent = await get_agent_definition(agent_id)
    return JSONResponse(agent, status_code=201)


@router.put("/api/org/agents/{agent_id}")
async def update_agent_endpoint(agent_id: str, request: Request):
    """Update an agent definition (all fields including org columns)."""
    body = await request.json()
    existing = await get_agent_definition(agent_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Use the existing update function for core fields
    result = await update_agent_definition(
        agent_id,
        name=body.get("name"),
        description=body.get("description"),
        system_prompt=body.get("system_prompt"),
        task=body.get("task"),
        timeout=body.get("timeout"),
        enabled=body.get("enabled"),
        changed_by=body.get("changed_by", "user"),
    )

    # Handle extended org fields via direct SQL update
    from src.database import get_backend
    from datetime import datetime, timezone
    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()

    ext_fields = {
        "org_unit_id": body.get("org_unit_id"),
        "role_title": body.get("role_title"),
        "goals_json": body.get("goals_json"),
        "tools_json": body.get("tools_json"),
        "reports_to_agent_id": body.get("reports_to_agent_id"),
        "avatar_color": body.get("avatar_color"),
        "chat_enabled": body.get("chat_enabled"),
    }
    set_clauses, params = [], []
    for field_name, value in ext_fields.items():
        if value is not None:
            if field_name in ("goals_json", "tools_json") and isinstance(value, list):
                value = json.dumps(value)
            if field_name == "chat_enabled":
                value = 1 if value else 0
            set_clauses.append(f"{field_name} = ?")
            params.append(value)

    if set_clauses:
        set_clauses.append("updated_at = ?")
        params.append(now)
        params.append(agent_id)
        await backend.execute_write(
            f"UPDATE agent_definitions SET {', '.join(set_clauses)} WHERE id = ?",
            tuple(params),
        )

    # Reload the in-memory agent registry
    await load_agents_from_db()

    updated = await get_agent_definition(agent_id)
    return JSONResponse(updated)


@router.delete("/api/org/agents/{agent_id}")
async def delete_agent_endpoint(agent_id: str):
    """Delete an agent definition."""
    ok = await delete_agent_definition(agent_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Agent not found")
    # Reload registry
    await load_agents_from_db()
    return JSONResponse({"deleted": True})


@router.patch("/api/org/agents/{agent_id}/toggle")
async def toggle_agent_endpoint(agent_id: str, request: Request):
    """Toggle an agent's enabled status."""
    body = await request.json()
    enabled = body.get("enabled")
    if enabled is None:
        raise HTTPException(status_code=400, detail="enabled is required")
    result = await update_agent_definition(agent_id, enabled=enabled)
    if not result:
        raise HTTPException(status_code=404, detail="Agent not found")
    await load_agents_from_db()
    return JSONResponse(result)


# ══════════════════════════════════════════════════════════════
# TOOLS — available tool listing for agent builder
# ══════════════════════════════════════════════════════════════

@router.get("/api/org/tools")
async def list_available_tools():
    """Return all available tools grouped by category for the agent builder."""
    from src.tools import get_all_tools

    all_tools = get_all_tools()

    # The tools are returned in lifecycle order by get_all_tools().
    # We know the exact order from tools/__init__.py:
    _ordered_categories = [
        # (start_index, count, category_name)
        (0, 6, "Service Governance"),
        (6, 4, "Standards & Compliance"),
        (10, 5, "Template Catalog"),
        (15, 4, "Code Generation"),
        (19, 2, "Architecture & Design"),
        (21, 2, "Cost & Validation"),
        (23, 5, "Deployment"),
        (28, 1, "Analytics"),
        (29, 3, "Org Intelligence"),
        (32, 1, "Output"),
        (33, 1, "Publishing"),
    ]

    grouped: dict[str, list[dict]] = {}
    for start, count, cat in _ordered_categories:
        items = []
        for i in range(start, min(start + count, len(all_tools))):
            tool = all_tools[i]
            name = getattr(tool, "name", None) or getattr(tool, "__name__", None) or str(tool)
            doc = getattr(tool, "description", None) or getattr(tool, "__doc__", "") or ""
            first_line = doc.strip().split("\n")[0][:100] if doc.strip() else name
            items.append({"name": name, "description": first_line})
        if items:
            grouped[cat] = items

    # Catch any tools beyond the mapped range
    mapped_count = sum(c for _, c, _ in _ordered_categories)
    if len(all_tools) > mapped_count:
        extra = []
        for tool in all_tools[mapped_count:]:
            name = getattr(tool, "name", None) or getattr(tool, "__name__", None) or str(tool)
            doc = getattr(tool, "description", None) or getattr(tool, "__doc__", "") or ""
            first_line = doc.strip().split("\n")[0][:100] if doc.strip() else name
            extra.append({"name": name, "description": first_line})
        if extra:
            grouped["Other"] = extra

    return JSONResponse(grouped)
