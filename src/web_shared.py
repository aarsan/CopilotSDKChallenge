"""
InfraForge — Shared Web State

Module-level singletons and helpers shared across all FastAPI routers.
Keeps web.py and every router DRY — import from here instead of
duplicating state or passing it through dependency injection.
"""

import asyncio
import logging
from typing import Optional

from copilot import CopilotClient
from src.config import COPILOT_LOG_LEVEL

logger = logging.getLogger("infraforge.web")

# ── Copilot SDK singleton ────────────────────────────────────
copilot_client: Optional[CopilotClient] = None
_copilot_init_lock = asyncio.Lock()


async def ensure_copilot_client() -> Optional[CopilotClient]:
    """Lazily initialize the Copilot SDK client on first use."""
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


# ── Active sessions ──────────────────────────────────────────
# session_token → { copilot_session, user_context }
active_sessions: dict[str, dict] = {}

# ── Active validation job tracker (in-memory) ────────────────
# service_id → { status, service_name, started_at, updated_at, phase, step,
#                progress, events: [dict], error?, rg_name? }
_active_validations: dict[str, dict] = {}


def _user_context_to_dict(user) -> dict:
    """Convert a UserContext into a dict for DB persistence."""
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
