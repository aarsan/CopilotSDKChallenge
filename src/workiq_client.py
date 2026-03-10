"""
InfraForge — Microsoft Work IQ MCP Client

Bridges the Node.js Work IQ MCP server with InfraForge's Python backend.
Uses asyncio to call `workiq ask -q "..."` for simple natural language queries.

The Work IQ CLI uses interactive browser-based auth. After first-time setup
(`workiq accept-eula`), tokens are cached and subsequent calls work server-side.
"""

import asyncio
import logging
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Optional

from src.config import WORKIQ_ENABLED, WORKIQ_TIMEOUT

_IS_WINDOWS = sys.platform == "win32"

logger = logging.getLogger("infraforge.workiq")

# Re-check availability every 60s so that auth changes are picked up
_AVAILABILITY_TTL = 60


@dataclass
class WorkIQResult:
    """Result of a Work IQ query — either success with text or failure with reason."""
    ok: bool
    text: Optional[str] = None
    error: Optional[str] = None


class WorkIQClient:
    """Client for querying Microsoft Work IQ."""

    def __init__(self):
        self._available: Optional[bool] = None
        self._checked_at: float = 0.0
        self._last_check_error: Optional[str] = None
        self._npx_path: Optional[str] = None

    def _resolve_npx(self) -> Optional[str]:
        """Resolve the full path to npx. Cached after first lookup."""
        if self._npx_path is None:
            self._npx_path = shutil.which("npx") or ""
        return self._npx_path or None

    async def _run(self, *args: str, timeout: float = 15) -> tuple[int, bytes, bytes]:
        """Run a CLI command, handling Windows .cmd scripts correctly.

        On Windows, npx is a .cmd batch file which create_subprocess_exec
        cannot launch directly — FileNotFoundError. We use
        create_subprocess_shell instead.
        """
        npx = self._resolve_npx()
        if not npx:
            raise FileNotFoundError("npx not found in PATH")
        if _IS_WINDOWS:
            # Shell execution for .cmd/.ps1 wrappers on Windows
            cmd = f'"{npx}" ' + " ".join(f'"{a}"' for a in args)
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                npx, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout, stderr

    async def is_available(self) -> bool:
        """Check if Work IQ CLI is available and authenticated."""
        if not WORKIQ_ENABLED:
            self._last_check_error = "Work IQ is disabled (WORKIQ_ENABLED=false)"
            return False
        # Re-check if we haven't checked yet, or if the cached value is
        # negative and the TTL has expired (allows recovery after auth).
        now = time.monotonic()
        if self._available is not None and (
            self._available or (now - self._checked_at < _AVAILABILITY_TTL)
        ):
            return self._available
        try:
            returncode, stdout, stderr = await self._run(
                "-y", "@microsoft/workiq", "--version", timeout=15
            )
            self._available = returncode == 0
            if not self._available:
                err = stderr.decode("utf-8", errors="replace").strip()
                self._last_check_error = err or f"CLI exited with code {returncode}"
            else:
                self._last_check_error = None
        except FileNotFoundError:
            logger.debug("Work IQ not available: npx not found in PATH")
            self._available = False
            self._last_check_error = "npx not found in PATH. Install Node.js 18+ and ensure npx is on your PATH."
        except asyncio.TimeoutError:
            logger.debug("Work IQ availability check timed out")
            self._available = False
            self._last_check_error = "Work IQ CLI version check timed out (15s)"
        except Exception as e:
            logger.debug(f"Work IQ not available: {e}")
            self._available = False
            self._last_check_error = str(e)
        self._checked_at = now
        return self._available

    def get_last_error(self) -> Optional[str]:
        """Return the last error from availability check or query."""
        return self._last_check_error

    async def ask(self, query: str) -> WorkIQResult:
        """Query Work IQ with a natural language question.

        Returns a WorkIQResult with either the response text or the error reason.
        """
        if not await self.is_available():
            return WorkIQResult(ok=False, error=self._last_check_error or "Work IQ CLI is not available")
        try:
            returncode, stdout, stderr = await self._run(
                "-y", "@microsoft/workiq", "ask", "-q", query,
                timeout=WORKIQ_TIMEOUT,
            )
            if returncode == 0:
                return WorkIQResult(ok=True, text=stdout.decode("utf-8").strip())
            else:
                err = stderr.decode("utf-8", errors="replace").strip()
                # Check for common permission / auth patterns
                err_lower = err.lower()
                if any(kw in err_lower for kw in ("permission", "unauthorized", "forbidden", "consent", "access denied", "403")):
                    reason = f"Permission error from Work IQ CLI: {err[:300]}"
                elif any(kw in err_lower for kw in ("login", "authenticate", "token", "sign in", "auth")):
                    reason = f"Authentication required: {err[:300]}. Run `npx -y @microsoft/workiq accept-eula` to authenticate."
                else:
                    reason = f"Work IQ CLI error (exit {returncode}): {err[:300]}"
                logger.warning(f"Work IQ query failed: {reason}")
                return WorkIQResult(ok=False, error=reason)
        except asyncio.TimeoutError:
            msg = f"Work IQ query timed out after {WORKIQ_TIMEOUT}s"
            logger.warning(f"{msg}: {query[:50]}")
            return WorkIQResult(ok=False, error=msg)
        except Exception as e:
            logger.error(f"Work IQ query error: {e}")
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
