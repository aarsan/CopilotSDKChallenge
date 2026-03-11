"""
InfraForge — Microsoft Work IQ MCP Client

Connects to the Work IQ MCP server over stdio using the Python MCP SDK.
The MCP server exposes an `ask_work_iq` tool that queries M365 data
(emails, meetings, documents, Teams messages, people) via natural language.

Requires `@microsoft/workiq` installed globally (`npm install -g @microsoft/workiq`)
and EULA accepted (`workiq accept-eula`).
"""

import asyncio
import json
import logging
import shutil
import time
from dataclasses import dataclass
from typing import Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.config import WORKIQ_ENABLED, WORKIQ_TIMEOUT

logger = logging.getLogger("infraforge.workiq")

# Re-check availability every 60s so that auth changes are picked up
_AVAILABILITY_TTL = 60


@dataclass
class WorkIQResult:
    """Result of a Work IQ query — either success with text or failure with reason."""
    ok: bool
    text: Optional[str] = None
    error: Optional[str] = None


def _resolve_workiq_command() -> tuple[str, list[str]]:
    """Resolve the Work IQ MCP server command.

    Prefers globally installed `workiq` binary. Falls back to `npx`.
    Returns (command, args) for StdioServerParameters.
    """
    if shutil.which("workiq"):
        return ("workiq", ["mcp"])
    if shutil.which("npx"):
        return ("npx", ["-y", "@microsoft/workiq", "mcp"])
    raise FileNotFoundError(
        "workiq not found. Run: npm install -g @microsoft/workiq"
    )


class WorkIQClient:
    """MCP client for querying Microsoft Work IQ."""

    def __init__(self):
        self._available: Optional[bool] = None
        self._checked_at: float = 0.0
        self._last_check_error: Optional[str] = None
        self._session: Optional[ClientSession] = None
        self._cm_stack = None  # context manager stack for cleanup

    async def _ensure_session(self) -> ClientSession:
        """Lazily connect to the Work IQ MCP server.

        Keeps a persistent stdio session alive so we don't pay startup
        cost on every query.
        """
        if self._session is not None:
            return self._session

        cmd, args = _resolve_workiq_command()
        server_params = StdioServerParameters(command=cmd, args=args)

        # stdio_client and ClientSession are async context managers.
        # We enter them manually and store for cleanup on shutdown.
        self._stdio_cm = stdio_client(server_params)
        read, write = await self._stdio_cm.__aenter__()

        self._session_cm = ClientSession(read, write)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()

        logger.info("Work IQ MCP session established")
        return self._session

    async def close(self):
        """Shut down the MCP session (call on server shutdown)."""
        if self._session is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception:
                pass
            try:
                await self._stdio_cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None
            logger.info("Work IQ MCP session closed")

    async def is_available(self) -> bool:
        """Check if Work IQ MCP server is available."""
        if not WORKIQ_ENABLED:
            self._last_check_error = "Work IQ is disabled (WORKIQ_ENABLED=false)"
            return False

        now = time.monotonic()
        if self._available is not None and (
            self._available or (now - self._checked_at < _AVAILABILITY_TTL)
        ):
            return self._available

        try:
            session = await asyncio.wait_for(self._ensure_session(), timeout=20)
            tools = await asyncio.wait_for(session.list_tools(), timeout=10)
            tool_names = [t.name for t in tools.tools]
            if "ask_work_iq" in tool_names:
                self._available = True
                self._last_check_error = None
            else:
                self._available = False
                self._last_check_error = f"Work IQ MCP server missing ask_work_iq tool. Found: {tool_names}"
        except FileNotFoundError:
            self._available = False
            self._last_check_error = "workiq not found. Run: npm install -g @microsoft/workiq"
        except asyncio.TimeoutError:
            self._available = False
            self._last_check_error = "Work IQ MCP server startup timed out"
        except Exception as e:
            self._available = False
            self._last_check_error = f"Work IQ MCP connection failed: {e}"
            # Reset session so next attempt reconnects
            self._session = None
        self._checked_at = now
        return self._available

    def get_last_error(self) -> Optional[str]:
        """Return the last error from availability check or query."""
        return self._last_check_error

    async def ask(self, query: str) -> WorkIQResult:
        """Query Work IQ with a natural language question via MCP."""
        if not await self.is_available():
            return WorkIQResult(
                ok=False,
                error=self._last_check_error or "Work IQ is not available",
            )
        try:
            session = self._session
            result = await asyncio.wait_for(
                session.call_tool("ask_work_iq", {"question": query}),
                timeout=WORKIQ_TIMEOUT,
            )
            if result.isError:
                err_text = ""
                for item in result.content:
                    if hasattr(item, "text"):
                        err_text += item.text
                err_lower = err_text.lower()
                if any(kw in err_lower for kw in ("permission", "unauthorized", "forbidden", "consent", "access denied", "403")):
                    reason = f"Permission error: {err_text[:300]}"
                elif any(kw in err_lower for kw in ("login", "authenticate", "token", "sign in", "auth", "eula")):
                    reason = f"Authentication required: {err_text[:300]}. Run: workiq accept-eula"
                else:
                    reason = f"Work IQ error: {err_text[:300]}"
                logger.warning(f"Work IQ query failed: {reason}")
                return WorkIQResult(ok=False, error=reason)

            # Extract text from response content
            text_parts = []
            for item in result.content:
                if hasattr(item, "text"):
                    raw = item.text
                    # Work IQ MCP returns JSON with a "response" field
                    try:
                        parsed = json.loads(raw)
                        text_parts.append(parsed.get("response", raw))
                    except (json.JSONDecodeError, TypeError):
                        text_parts.append(raw)
            return WorkIQResult(ok=True, text="\n".join(text_parts))

        except asyncio.TimeoutError:
            msg = f"Work IQ query timed out after {WORKIQ_TIMEOUT}s"
            logger.warning(f"{msg}: {query[:80]}")
            return WorkIQResult(ok=False, error=msg)
        except Exception as e:
            logger.error(f"Work IQ MCP call failed: {e}")
            # Reset session to force reconnect on next attempt
            self._session = None
            return WorkIQResult(ok=False, error=str(e))

    async def search_documents(self, topic: str) -> WorkIQResult:
        """Search for M365 documents related to a topic."""
        return await self.ask(
            f"Find SharePoint and OneDrive documents related to: {topic}"
        )

    async def find_experts(self, domain: str) -> WorkIQResult:
        """Find people with expertise in a specific domain."""
        return await self.ask(
            f"Who are the subject matter experts or people who have "
            f"worked on or discussed: {domain}"
        )

    async def search_meetings(self, topic: str) -> WorkIQResult:
        """Search meeting notes and calendar events related to a topic."""
        return await self.ask(
            f"Find meetings, meeting notes, and calendar events about: {topic}"
        )

    async def search_communications(self, topic: str) -> WorkIQResult:
        """Search emails and Teams messages about a topic."""
        return await self.ask(f"Find emails and Teams messages discussing: {topic}")


# Module-level singleton
_workiq_client: Optional[WorkIQClient] = None


def get_workiq_client() -> WorkIQClient:
    """Get or create the Work IQ client singleton."""
    global _workiq_client
    if _workiq_client is None:
        _workiq_client = WorkIQClient()
    return _workiq_client
