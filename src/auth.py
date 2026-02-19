"""
InfraForge — Entra ID (Azure AD) Authentication Module

Provides MSAL-based authentication for the web UI.
Users sign in with their corporate M365 accounts, and InfraForge extracts
identity context (name, email, department, groups, cost center) to enrich
the agent's understanding of who is making infrastructure requests.

This enables:
- Auto-tagging resources with owner / cost center
- Role-based template filtering
- Approval routing to the right manager
- Usage analytics by team / department

Sessions are persisted in Fabric SQL Database so users survive server restarts.
"""

import os
import time
import secrets
from dataclasses import dataclass, field
from typing import Optional

import msal

from src.config import (
    ENTRA_CLIENT_ID,
    ENTRA_TENANT_ID,
    ENTRA_CLIENT_SECRET,
    ENTRA_REDIRECT_URI,
    ENTRA_AUTHORITY,
    ENTRA_SCOPES,
)


@dataclass
class UserContext:
    """Represents an authenticated user's organizational context.

    This context is injected into the agent's system prompt so InfraForge
    can personalize responses, auto-tag resources, and enforce role-based policies.
    """

    user_id: str = ""
    display_name: str = ""
    email: str = ""
    job_title: str = ""
    department: str = ""
    cost_center: str = ""
    manager: str = ""
    groups: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)

    # Derived from group membership
    team: str = ""
    is_platform_team: bool = False
    is_admin: bool = False

    def to_prompt_context(self) -> str:
        """Format user context for injection into the agent system prompt.

        This string gets appended to the system message so InfraForge knows
        who it's talking to and can make intelligent decisions about tagging,
        policy enforcement, and template filtering.
        """
        lines = [
            "\n## Authenticated User Context",
            f"- **Name**: {self.display_name}",
            f"- **Email**: {self.email}",
        ]
        if self.job_title:
            lines.append(f"- **Role**: {self.job_title}")
        if self.department:
            lines.append(f"- **Department**: {self.department}")
        if self.cost_center:
            lines.append(f"- **Cost Center**: {self.cost_center}")
        if self.team:
            lines.append(f"- **Team**: {self.team}")
        if self.manager:
            lines.append(f"- **Manager**: {self.manager}")
        if self.groups:
            lines.append(f"- **Groups**: {', '.join(self.groups[:10])}")
        if self.is_platform_team:
            lines.append("- **Access Level**: Platform Team (full catalog access, can register templates)")
        elif self.is_admin:
            lines.append("- **Access Level**: Admin (full access)")
        else:
            lines.append("- **Access Level**: Standard (can use approved templates, request new infrastructure)")

        lines.extend([
            "",
            "When generating infrastructure, automatically apply:",
            f'- `owner` tag → `"{self.email}"`',
            f'- `costCenter` tag → `"{self.cost_center or "TBD"}"`',
            f'- `department` tag → `"{self.department or "TBD"}"`',
            f'- `requestedBy` tag → `"{self.display_name}"`',
        ])

        return "\n".join(lines)


# ── In-memory auth flow cache (short-lived, OK to lose on restart) ────
# Auth flows last only seconds while the user is redirected to Entra ID.
# Sessions themselves are persisted in the database.
_auth_flows: dict[str, dict] = {}


def _get_msal_app() -> msal.ConfidentialClientApplication:
    """Create an MSAL confidential client app."""
    return msal.ConfidentialClientApplication(
        client_id=ENTRA_CLIENT_ID,
        client_credential=ENTRA_CLIENT_SECRET,
        authority=ENTRA_AUTHORITY,
    )


def create_auth_url(state: Optional[str] = None) -> tuple[str, str]:
    """Generate an Entra ID login URL.

    Returns:
        Tuple of (auth_url, flow_id) — the flow_id is used to complete
        the auth code exchange after redirect.
    """
    app = _get_msal_app()
    flow_id = secrets.token_urlsafe(32)

    flow = app.initiate_auth_code_flow(
        scopes=ENTRA_SCOPES,
        redirect_uri=ENTRA_REDIRECT_URI,
        state=state or flow_id,
    )

    _auth_flows[flow_id] = flow
    return flow.get("auth_uri", ""), flow_id


def complete_auth(flow_id: str, auth_response: dict) -> Optional[str]:
    """Complete the auth code flow and create a session.

    Args:
        flow_id: The flow ID from create_auth_url
        auth_response: The query parameters from the redirect

    Returns:
        Session token if successful, None otherwise.
        The session is persisted to the database asynchronously —
        call persist_session() after this returns.
    """
    flow = _auth_flows.pop(flow_id, None)
    if not flow:
        return None

    app = _get_msal_app()
    result = app.acquire_token_by_auth_code_flow(flow, auth_response)

    if "access_token" not in result:
        return None

    # Extract user info from the ID token claims
    claims = result.get("id_token_claims", {})
    session_token = secrets.token_urlsafe(48)
    user_context = _build_user_context(claims, result.get("access_token"))

    # Store temporarily in memory — the caller (web.py) will persist to DB
    _pending_sessions[session_token] = {
        "access_token": result["access_token"],
        "claims": claims,
        "user_context": user_context,
        "created_at": time.time(),
    }

    return session_token


# Temporary store for sessions between complete_auth() and persist_session()
_pending_sessions: dict[str, dict] = {}


def get_pending_session(session_token: str) -> Optional[dict]:
    """Pop a pending session (used by web.py to persist to DB)."""
    return _pending_sessions.pop(session_token, None)


async def get_user_context(session_token: str) -> Optional[UserContext]:
    """Retrieve the user context for a valid session from the database."""
    from src.database import get_session

    session = await get_session(session_token)
    if not session:
        return None

    return UserContext(
        user_id=session["user_id"],
        display_name=session["display_name"],
        email=session["email"],
        job_title=session["job_title"],
        department=session["department"],
        cost_center=session["cost_center"],
        manager=session["manager"],
        groups=session["groups"],
        roles=session["roles"],
        team=session["team"],
        is_platform_team=session["is_platform_team"],
        is_admin=session["is_admin"],
    )


async def invalidate_session(session_token: str) -> None:
    """Log out — remove the session from the database."""
    from src.database import delete_session

    await delete_session(session_token)


def _build_user_context(claims: dict, access_token: Optional[str] = None) -> UserContext:
    """Build a UserContext from Entra ID token claims.

    In a full implementation, this would also call the Microsoft Graph API
    to fetch group membership, manager info, and custom extension attributes
    like cost center. For now, we extract what's available in the token claims.
    """
    # Standard claims from the ID token
    ctx = UserContext(
        user_id=claims.get("oid", claims.get("sub", "")),
        display_name=claims.get("name", "Unknown User"),
        email=claims.get("preferred_username", claims.get("email", "")),
        job_title=claims.get("jobTitle", ""),
        department=claims.get("department", ""),
    )

    # Groups come from the "groups" claim if configured in the app registration
    raw_groups = claims.get("groups", [])
    if isinstance(raw_groups, list):
        ctx.groups = raw_groups

    # Roles come from app role assignments
    raw_roles = claims.get("roles", [])
    if isinstance(raw_roles, list):
        ctx.roles = raw_roles

    # Derive team and access level from groups/roles
    ctx.is_platform_team = "PlatformTeam" in ctx.roles or "PlatformTeam" in ctx.groups
    ctx.is_admin = "InfraForge.Admin" in ctx.roles or ctx.is_platform_team

    # Try to extract cost center from custom claims (configured in Entra ID)
    ctx.cost_center = claims.get("extension_costCenter", claims.get("costCenter", ""))

    return ctx


# ── Demo / Development Mode ─────────────────────────────────
# When Entra ID isn't configured, provide a demo user context
# so the web UI works without requiring a real tenant.


def create_demo_session() -> tuple[str, UserContext]:
    """Create a demo session with a sample user for development/demo purposes.

    Returns (session_token, user_context). The caller must persist the session
    to the database by calling save_session() from src.database.
    """
    demo_user = UserContext(
        user_id="demo-user-001",
        display_name="Alex Johnson",
        email="alex.johnson@contoso.com",
        job_title="Senior Cloud Engineer",
        department="Engineering",
        cost_center="CC-ENG-4200",
        manager="Jordan Smith",
        groups=["Engineering", "Cloud-Platform", "Azure-Contributors"],
        roles=["Developer"],
        team="Cloud Platform",
        is_platform_team=False,
        is_admin=False,
    )

    session_token = secrets.token_urlsafe(48)

    # Store temporarily so web.py can persist to DB (same flow as real auth)
    _pending_sessions[session_token] = {
        "access_token": "demo-token",
        "claims": {},
        "user_context": demo_user,
        "created_at": time.time(),
    }

    return session_token, demo_user


def is_auth_configured() -> bool:
    """Check if Entra ID authentication is properly configured."""
    return bool(ENTRA_CLIENT_ID and ENTRA_TENANT_ID and ENTRA_CLIENT_SECRET)
