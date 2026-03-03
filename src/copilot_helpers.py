"""Copilot SDK helpers — thin wrappers for common one-shot patterns.

Uses the SDK's ``send_and_wait()`` for clean idle-detection instead of
manual event-loop boilerplate (asyncio.Event + unsub dance).

Re-exports ``get_model_for_task`` and ``Task`` for convenience so callers
can import everything SDK-related from one place.
"""

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Callable, Optional

from copilot import CopilotClient

from src.model_router import Task, get_model_for_task  # re-export

logger = logging.getLogger(__name__)

_db_loaded = False  # True once we've loaded counters from DB


# ══════════════════════════════════════════════════════════════
# AGENT ACTIVITY TRACKER — in-memory ring buffer of SDK calls
# ══════════════════════════════════════════════════════════════

_ACTIVITY_MAX = 500  # keep last N invocations

_activity_log: deque[dict] = deque(maxlen=_ACTIVITY_MAX)
_activity_lock = Lock()
_activity_counters: dict[str, dict] = {}  # agent_name → {calls, errors, total_ms}


def _record_activity(
    *,
    agent_name: str,
    model: str,
    status: str,
    duration_ms: float,
    prompt_len: int,
    response_len: int,
    error: str | None = None,
) -> None:
    """Record a Copilot SDK invocation for the observability dashboard."""
    entry = {
        "agent": agent_name,
        "model": model,
        "status": status,
        "duration_ms": round(duration_ms, 1),
        "prompt_len": prompt_len,
        "response_len": response_len,
        "error": error,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with _activity_lock:
        _activity_log.append(entry)
        if agent_name not in _activity_counters:
            _activity_counters[agent_name] = {
                "calls": 0, "errors": 0, "total_ms": 0.0,
                "last_called": None, "last_model": None,
            }
        c = _activity_counters[agent_name]
        c["calls"] += 1
        c["total_ms"] += duration_ms
        c["last_called"] = entry["timestamp"]
        c["last_model"] = model
        if status == "error":
            c["errors"] += 1

    # Fire-and-forget DB persistence
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_persist_activity(agent_name, entry))
    except RuntimeError:
        pass  # no event loop — CLI mode, skip DB persistence


async def _persist_activity(agent_name: str, entry: dict) -> None:
    """Persist agent counter + activity log row to the database."""
    try:
        from src.database import get_backend
        b = await get_backend()
        ts = entry["timestamp"]
        model = entry["model"]
        status = entry["status"]
        dur = entry["duration_ms"]
        err_flag = 1 if status == "error" else 0

        # Upsert counter row
        await b.execute_write(
            """MERGE agent_counters AS tgt
            USING (SELECT ? AS agent_name) AS src ON tgt.agent_name = src.agent_name
            WHEN MATCHED THEN UPDATE SET
                calls = tgt.calls + 1,
                errors = tgt.errors + ?,
                total_ms = tgt.total_ms + ?,
                last_called = ?,
                last_model = ?
            WHEN NOT MATCHED THEN INSERT
                (agent_name, calls, errors, total_ms, last_called, last_model)
                VALUES (?, 1, ?, ?, ?, ?);""",
            (agent_name, err_flag, dur, ts, model,
             agent_name, err_flag, dur, ts, model),
        )

        # Insert activity log row (keep last 500 in DB)
        await b.execute_write(
            """INSERT INTO agent_activity_log
                (agent_name, model, status, duration_ms, prompt_len, response_len, error_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent_name, model, status, dur,
             entry["prompt_len"], entry["response_len"],
             entry.get("error"), ts),
        )

        # Trim old rows (keep last 500)
        await b.execute_write(
            """DELETE FROM agent_activity_log WHERE id NOT IN
            (SELECT TOP 500 id FROM agent_activity_log ORDER BY id DESC)""",
            (),
        )
    except Exception as exc:
        logger.debug(f"Failed to persist agent activity: {exc}")


async def load_agent_counters_from_db() -> None:
    """Load persisted agent counters from the database on startup.

    Merges DB-stored counters into the in-memory ring buffer so that
    agent usage survives server restarts.
    """
    global _db_loaded
    if _db_loaded:
        return
    try:
        from src.database import get_backend
        b = await get_backend()

        # Load counters
        rows = await b.execute("SELECT * FROM agent_counters", ())
        with _activity_lock:
            for row in rows:
                name = row["agent_name"]
                _activity_counters[name] = {
                    "calls": row.get("calls", 0),
                    "errors": row.get("errors", 0),
                    "total_ms": row.get("total_ms", 0.0),
                    "last_called": row.get("last_called"),
                    "last_model": row.get("last_model"),
                }

        # Load recent activity log
        log_rows = await b.execute(
            "SELECT TOP 200 * FROM agent_activity_log ORDER BY id DESC", ()
        )
        with _activity_lock:
            for row in reversed(log_rows):  # oldest first into deque
                _activity_log.append({
                    "agent": row["agent_name"],
                    "model": row.get("model", ""),
                    "status": row.get("status", "ok"),
                    "duration_ms": row.get("duration_ms", 0),
                    "prompt_len": row.get("prompt_len", 0),
                    "response_len": row.get("response_len", 0),
                    "error": row.get("error_text"),
                    "timestamp": row.get("created_at", ""),
                })

        _db_loaded = True
        logger.info(f"Loaded {len(rows)} agent counter(s) and {len(log_rows)} activity log entries from DB")
    except Exception as exc:
        logger.warning(f"Could not load agent counters from DB: {exc}")


def get_agent_activity(limit: int = 100) -> list[dict]:
    """Return recent agent activity entries (newest first)."""
    with _activity_lock:
        items = list(_activity_log)
    items.reverse()
    return items[:limit]


def get_agent_counters() -> dict[str, dict]:
    """Return cumulative per-agent counters since server start."""
    with _activity_lock:
        return {k: dict(v) for k, v in _activity_counters.items()}


async def copilot_send(
    client: CopilotClient,
    *,
    model: str,
    system_prompt: str,
    prompt: str,
    timeout: float = 60.0,
    on_event: Optional[Callable] = None,
    agent_name: str = "unknown",
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
        agent_name:    Name of the agent making this call (for activity tracking).

    Returns:
        The assistant's response text (stripped).  Empty string if no response.

    Raises:
        asyncio.TimeoutError: If the timeout is exceeded.
        Exception: On session-level errors.
    """
    t0 = time.perf_counter()
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
        response = ((result.data.content or "") if result else "").strip()
        _record_activity(
            agent_name=agent_name, model=model, status="ok",
            duration_ms=(time.perf_counter() - t0) * 1000,
            prompt_len=len(prompt), response_len=len(response),
        )
        return response
    except Exception as exc:
        _record_activity(
            agent_name=agent_name, model=model, status="error",
            duration_ms=(time.perf_counter() - t0) * 1000,
            prompt_len=len(prompt), response_len=0,
            error=str(exc)[:500],
        )
        raise
    finally:
        if unsub:
            unsub()
        try:
            await session.destroy()
        except Exception:
            pass
