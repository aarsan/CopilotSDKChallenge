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
    grouped: dict[str, list[dict]] = {}

    for tool in all_tools:
        # Extract tool metadata
        name = getattr(tool, "__name__", str(tool))
        doc = getattr(tool, "__doc__", "") or ""
        first_line = doc.strip().split("\n")[0] if doc.strip() else name

        # Categorize based on module
        module = getattr(tool, "__module__", "")
        if "governance" in module or "policy" in module or "compliance" in module:
            cat = "Governance & Compliance"
        elif "catalog" in module:
            cat = "Template Catalog"
        elif "deploy" in module or "arm" in module:
            cat = "Deployment"
        elif "cost" in module:
            cat = "Cost & Validation"
        elif "diagram" in module or "design" in module:
            cat = "Architecture & Design"
        elif "github" in module or "devops" in module:
            cat = "CI/CD & Publishing"
        elif "bicep" in module or "terraform" in module:
            cat = "Code Generation"
        elif "workiq" in module:
            cat = "Org Intelligence"
        elif "save" in module:
            cat = "Output"
        elif "service" in module:
            cat = "Service Catalog"
        else:
            cat = "General"

        grouped.setdefault(cat, []).append({"name": name, "description": first_line})

    return JSONResponse(grouped)
