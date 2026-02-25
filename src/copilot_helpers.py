"""Copilot SDK helpers — thin wrappers for common one-shot patterns.

Uses the SDK's ``send_and_wait()`` for clean idle-detection instead of
manual event-loop boilerplate (asyncio.Event + unsub dance).

Re-exports ``get_model_for_task`` and ``Task`` for convenience so callers
can import everything SDK-related from one place.
"""

import logging
from typing import Callable, Optional

from copilot import CopilotClient

from src.model_router import Task, get_model_for_task  # re-export

logger = logging.getLogger(__name__)


async def copilot_send(
    client: CopilotClient,
    *,
    model: str,
    system_prompt: str,
    prompt: str,
    timeout: float = 60.0,
    on_event: Optional[Callable] = None,
) -> str:
    """One-shot prompt via the Copilot SDK using ``send_and_wait()``.

    Creates a session, optionally registers an event handler (for progress
    reporting or chunk counting), sends the prompt, waits for idle, destroys
    the session, and returns the full response text.

    Args:
        client:        Initialized ``CopilotClient``.
        model:         Model identifier (from ``get_model_for_task``).
        system_prompt: System message for the agent.
        prompt:        User prompt to send.
        timeout:       Max seconds to wait (default 60).
        on_event:      Optional event callback — receives all session events
                       while ``send_and_wait()`` blocks.  Useful for progress
                       reporting or telemetry.

    Returns:
        The assistant's response text (stripped).  Empty string if no response.

    Raises:
        asyncio.TimeoutError: If the timeout is exceeded.
        Exception: On session-level errors.
    """
    session = await client.create_session({
        "model": model,
        "streaming": True,
        "tools": [],
        "system_message": {"content": system_prompt},
    })
    unsub = None
    try:
        if on_event:
            unsub = session.on(on_event)
        result = await session.send_and_wait({"prompt": prompt}, timeout=timeout)
        return ((result.data.content or "") if result else "").strip()
    finally:
        if unsub:
            unsub()
        try:
            await session.destroy()
        except Exception:
            pass
