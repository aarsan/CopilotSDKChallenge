"""
InfraForge — Microsoft Work IQ MCP Client

Bridges the Node.js Work IQ MCP server with InfraForge's Python backend.
Uses asyncio to call `workiq ask -q "..."` for simple natural language queries.

The Work IQ CLI uses interactive browser-based auth. After first-time setup
(`workiq accept-eula`), tokens are cached and subsequent calls work server-side.
"""

import asyncio
import logging
from typing import Optional

from src.config import WORKIQ_ENABLED, WORKIQ_TIMEOUT

logger = logging.getLogger("infraforge.workiq")


class WorkIQClient:
    """Client for querying Microsoft Work IQ."""

    def __init__(self):
        self._available: Optional[bool] = None

    async def is_available(self) -> bool:
        """Check if Work IQ CLI is available and authenticated."""
        if self._available is not None:
            return self._available
        if not WORKIQ_ENABLED:
            self._available = False
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                "npx",
                "-y",
                "@microsoft/workiq",
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.wait(), timeout=15)
            self._available = proc.returncode == 0
        except Exception as e:
            logger.debug(f"Work IQ not available: {e}")
            self._available = False
        return self._available

    async def ask(self, query: str) -> Optional[str]:
        """Query Work IQ with a natural language question.

        Returns the response text, or None if Work IQ is unavailable or errors.
        """
        if not await self.is_available():
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                "npx",
                "-y",
                "@microsoft/workiq",
                "ask",
                "-q",
                query,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=WORKIQ_TIMEOUT
            )
            if proc.returncode == 0:
                return stdout.decode("utf-8").strip()
            else:
                logger.warning(f"Work IQ query failed: {stderr.decode()[:200]}")
                return None
        except asyncio.TimeoutError:
            logger.warning(
                f"Work IQ query timed out after {WORKIQ_TIMEOUT}s: {query[:50]}"
            )
            return None
        except Exception as e:
            logger.error(f"Work IQ query error: {e}")
            return None

    async def search_documents(self, topic: str) -> Optional[str]:
        """Search for M365 documents related to a topic."""
        return await self.ask(
            f"Find SharePoint and OneDrive documents related to: {topic}"
        )

    async def find_experts(self, domain: str) -> Optional[str]:
        """Find people with expertise in a specific domain."""
        return await self.ask(
            f"Who are the subject matter experts or people who have "
            f"worked on or discussed: {domain}"
        )

    async def search_meetings(self, topic: str) -> Optional[str]:
        """Search meeting notes and calendar events related to a topic."""
        return await self.ask(
            f"Find meetings, meeting notes, and calendar events about: {topic}"
        )

    async def search_communications(self, topic: str) -> Optional[str]:
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
