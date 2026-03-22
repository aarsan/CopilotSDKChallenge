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
from copilot.types import PermissionRequest, PermissionRequestResult

from src.model_router import Task, get_model_for_task  # re-export

logger = logging.getLogger("infraforge.copilot_helpers")


def approve_all(request: PermissionRequest, context: dict[str, str]) -> PermissionRequestResult:
    """Permission handler that approves every request."""
    return PermissionRequestResult(kind="approved")


_db_loaded = False  # True once we've loaded counters from DB


# ══════════════════════════════════════════════════════════════
# AGENT ACTIVITY TRACKER — in-memory ring buffer of SDK calls
# ══════════════════════════════════════════════════════════════

_ACTIVITY_MAX = 500  # keep last N invocations

_activity_log: deque[dict] = deque(maxlen=_ACTIVITY_MAX)
_activity_lock = Lock()
_activity_counters: dict[str, dict] = {}  # agent_name → {calls, errors, total_ms, scores …}

# Threshold: when an agent accumulates this many unresolved misses of
# the same type, auto-generate a prompt improvement suggestion.
_MISS_IMPROVEMENT_THRESHOLD = 5

# Recalculate scores every N calls per agent.
_SCORE_RECALC_INTERVAL = 10


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
    should_recalc = False
    with _activity_lock:
        _activity_log.append(entry)
        if agent_name not in _activity_counters:
            _activity_counters[agent_name] = {
                "calls": 0, "errors": 0, "total_ms": 0.0,
                "last_called": None, "last_model": None,
                "total_misses": 0,
                "performance_score": 50, "reliability_score": 50,
                "speed_score": 50, "quality_score": 50,
            }
        c = _activity_counters[agent_name]
        c["calls"] += 1
        c["total_ms"] += duration_ms
        c["last_called"] = entry["timestamp"]
        c["last_model"] = model
        if status == "error":
            c["errors"] += 1
        should_recalc = (c["calls"] % _SCORE_RECALC_INTERVAL == 0)

    # Fire-and-forget DB persistence
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_persist_activity(agent_name, entry))
        if should_recalc:
            loop.create_task(_async_recalculate_scores(agent_name))
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
                    "total_misses": row.get("total_misses", 0),
                    "performance_score": row.get("performance_score", 50),
                    "reliability_score": row.get("reliability_score", 50),
                    "speed_score": row.get("speed_score", 50),
                    "quality_score": row.get("quality_score", 50),
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


# ══════════════════════════════════════════════════════════════
# AGENT MISS RECORDING
# ══════════════════════════════════════════════════════════════

async def record_agent_miss(
    agent_name: str,
    miss_type: str,
    *,
    context_summary: str = "",
    error_detail: str = "",
    input_preview: str = "",
    output_preview: str = "",
    pipeline_phase: str | None = None,
) -> None:
    """Record an agent miss (automatic or manual) and check improvement threshold."""
    # Update in-memory counter
    with _activity_lock:
        c = _activity_counters.get(agent_name)
        if c:
            c["total_misses"] = c.get("total_misses", 0) + 1

    # Persist to database
    try:
        from src.database import insert_agent_miss
        await insert_agent_miss(
            agent_name, miss_type,
            context_summary=context_summary,
            error_detail=error_detail,
            input_preview=input_preview,
            output_preview=output_preview,
            pipeline_phase=pipeline_phase,
        )
    except Exception as exc:
        logger.debug(f"Failed to persist agent miss: {exc}")

    # Check if improvement threshold is met
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_check_improvement_threshold(agent_name, miss_type))
    except RuntimeError:
        pass


async def _check_improvement_threshold(agent_name: str, miss_type: str) -> None:
    """If an agent has ≥N unresolved misses of the same type, generate improvement."""
    try:
        from src.database import get_agent_misses, get_prompt_improvements

        # Count unresolved misses of this type
        misses = await get_agent_misses(agent_name=agent_name, resolved=False, limit=200)
        same_type = [m for m in misses if m.get("miss_type") == miss_type]

        if len(same_type) < _MISS_IMPROVEMENT_THRESHOLD:
            return

        # Check if there's already a pending improvement for this agent
        pending = await get_prompt_improvements(agent_name=agent_name, status="pending")
        if pending:
            return

        # Generate improvement suggestion
        await generate_prompt_improvement(agent_name, same_type)
    except Exception as exc:
        logger.debug(f"Improvement threshold check failed: {exc}")


# ══════════════════════════════════════════════════════════════
# PERFORMANCE SCORE ENGINE
# ══════════════════════════════════════════════════════════════

def _compute_scores(agent_name: str) -> dict[str, int]:
    """Compute performance scores for an agent from in-memory counters.

    Returns dict with performance_score, reliability_score, speed_score, quality_score.
    Weights: reliability=40%, quality=30%, speed=20%, volume=10%.
    """
    with _activity_lock:
        c = _activity_counters.get(agent_name)
        if not c or c.get("calls", 0) == 0:
            return {"performance_score": 50, "reliability_score": 50,
                    "speed_score": 50, "quality_score": 50}
        calls = c["calls"] or 0
        errors = c.get("errors") or 0
        total_ms = c.get("total_ms") or 0.0
        total_misses = c.get("total_misses") or 0

    # Reliability score (0-100): penalizes error rate
    error_rate = errors / max(calls, 1)
    reliability = int(100 * (1 - error_rate))

    # Speed score (0-100): relative to a 60s baseline timeout
    avg_ms = total_ms / max(calls, 1)
    timeout_ms = 60_000  # baseline 60s
    speed = int(max(0, min(100, 100 * (1 - avg_ms / timeout_ms))))

    # Quality score (0-100): inverse miss rate, boosted by positive feedback
    miss_rate = total_misses / max(calls, 1)
    quality = int(max(0, min(100, 100 * (1 - miss_rate * 5))))  # 20% miss rate → 0 quality

    # Volume bonus (0-100): logarithmic scale, 100 calls = 100%
    import math
    volume = int(min(100, math.log10(max(calls, 1)) / 2 * 100))

    # Composite
    composite = int(
        reliability * 0.40
        + quality * 0.30
        + speed * 0.20
        + volume * 0.10
    )
    composite = max(0, min(100, composite))

    return {
        "performance_score": composite,
        "reliability_score": reliability,
        "speed_score": speed,
        "quality_score": quality,
    }


async def _async_recalculate_scores(agent_name: str) -> None:
    """Recalculate and persist scores for a single agent (fire-and-forget)."""
    try:
        scores = _compute_scores(agent_name)
        # Update in-memory
        with _activity_lock:
            c = _activity_counters.get(agent_name)
            if c:
                c.update(scores)

        # Persist
        from src.database import update_agent_scores
        await update_agent_scores(agent_name, **scores)
    except Exception as exc:
        logger.debug(f"Score recalculation failed for {agent_name}: {exc}")


async def recalculate_all_agent_scores() -> dict[str, dict]:
    """Recalculate scores for every known agent. Returns {agent: scores}."""
    result: dict[str, dict] = {}
    with _activity_lock:
        agents = list(_activity_counters.keys())
    for name in agents:
        scores = _compute_scores(name)
        with _activity_lock:
            c = _activity_counters.get(name)
            if c:
                c.update(scores)
        result[name] = scores
        try:
            from src.database import update_agent_scores
            await update_agent_scores(name, **scores)
        except Exception:
            pass
    return result


# ══════════════════════════════════════════════════════════════
# PROMPT IMPROVEMENT GENERATOR
# ══════════════════════════════════════════════════════════════

async def generate_prompt_improvement(agent_name: str, misses: list[dict]) -> None:
    """Use LLM to analyze miss patterns and suggest a prompt improvement."""
    try:
        from src.agents import AGENTS, LLM_REASONER
        from src.database import insert_prompt_improvement

        spec = AGENTS.get(agent_name)
        if not spec:
            return

        # Summarize miss patterns
        miss_types: dict[str, int] = {}
        error_samples: list[str] = []
        for m in misses[:20]:
            mt = m.get("miss_type", "unknown")
            miss_types[mt] = miss_types.get(mt, 0) + 1
            detail = m.get("error_detail", "")
            if detail and len(error_samples) < 5:
                error_samples.append(detail[:300])

        pattern_summary = ", ".join(f"{k}: {v}x" for k, v in miss_types.items())
        errors_text = "\n".join(f"- {e}" for e in error_samples) if error_samples else "No specific errors"

        prompt = (
            f"You are an AI agent prompt engineer. Analyze the following failure patterns "
            f"for the agent '{agent_name}' and suggest specific improvements to its system prompt.\n\n"
            f"AGENT PURPOSE: {spec.description}\n\n"
            f"CURRENT SYSTEM PROMPT (abbreviated):\n{spec.system_prompt[:3000]}\n\n"
            f"FAILURE PATTERNS ({len(misses)} total misses):\n"
            f"Types: {pattern_summary}\n\n"
            f"SAMPLE ERRORS:\n{errors_text}\n\n"
            f"Respond with EXACTLY this JSON format (no markdown fences):\n"
            f'{{"suggested_patch": "<specific text to ADD to the system prompt to prevent these failures>", '
            f'"reasoning": "<why this change will help>"}}'
        )

        # Import ensure_copilot_client from web
        from src.web import ensure_copilot_client
        from src.model_router import get_model_for_task
        client = await ensure_copilot_client()

        response = await copilot_send(
            client,
            model=get_model_for_task(LLM_REASONER.task),
            system_prompt="You are an expert prompt engineer. Return ONLY valid JSON.",
            prompt=prompt,
            timeout=60,
            agent_name="LLM_REASONER",
        )

        # Try to parse JSON response
        import json
        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            # Try to extract JSON from response
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(response[start:end])
            else:
                return

        suggested = data.get("suggested_patch", "")
        reasoning = data.get("reasoning", "")
        if not suggested:
            return

        await insert_prompt_improvement(
            agent_name=agent_name,
            miss_pattern=pattern_summary,
            miss_count=len(misses),
            suggested_patch=suggested,
            reasoning=reasoning,
        )
        logger.info(f"Generated prompt improvement suggestion for {agent_name} ({len(misses)} misses)")

    except Exception as exc:
        logger.debug(f"Prompt improvement generation failed for {agent_name}: {exc}")


async def apply_prompt_improvement(improvement_id: int, reviewed_by: str = "admin") -> bool:
    """Approve and apply a prompt improvement: append patch to agent's system prompt."""
    try:
        from src.database import (
            get_prompt_improvements, update_prompt_improvement,
            update_agent_definition, resolve_agent_miss, get_agent_misses,
        )

        improvements = await get_prompt_improvements()
        imp = next((i for i in improvements if i.get("id") == improvement_id), None)
        if not imp:
            return False

        agent_name = imp["agent_name"]
        patch = imp.get("suggested_patch", "")
        if not patch:
            return False

        # Get current prompt from agent spec
        from src.agents import AGENTS
        spec = AGENTS.get(agent_name)
        if not spec:
            return False

        # Append the improvement patch to the system prompt
        new_prompt = spec.system_prompt + f"\n\n## LEARNED RULE (auto-generated)\n{patch}"

        # Update agent definition (persists + versions)
        await update_agent_definition(
            agent_name,
            system_prompt=new_prompt,
            changed_by=f"improvement_{improvement_id}_by_{reviewed_by}",
        )

        # Also update the in-memory spec
        spec.system_prompt = new_prompt

        # Mark improvement as applied
        await update_prompt_improvement(improvement_id, "applied", reviewed_by)

        # Resolve related unresolved misses
        misses = await get_agent_misses(agent_name=agent_name, resolved=False, limit=100)
        for m in misses:
            await resolve_agent_miss(
                m["id"],
                resolution_note=f"Addressed by prompt improvement #{improvement_id}",
            )

        logger.info(f"Applied prompt improvement #{improvement_id} to {agent_name}")
        return True
    except Exception as exc:
        logger.error(f"Failed to apply prompt improvement: {exc}")
        return False


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
    # Wrap entire session lifecycle in asyncio.wait_for as a hard backstop.
    # The SDK timeout on send_and_wait is the primary timeout, but
    # create_session() itself has no timeout and can hang indefinitely.
    try:
        session = await asyncio.wait_for(
            client.create_session({
                "model": model,
                "streaming": True,
                "tools": [],
                "system_message": {"content": system_prompt},
                "on_permission_request": approve_all,
            }),
            timeout=min(timeout, 30.0),  # session creation should be fast
        )
    except asyncio.TimeoutError:
        _record_activity(
            agent_name=agent_name, model=model, status="error",
            duration_ms=(time.perf_counter() - t0) * 1000,
            prompt_len=len(prompt), response_len=0,
            error="Session creation timed out",
        )
        raise
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
