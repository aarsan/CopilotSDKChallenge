"""
InfraForge Pipeline Runner — DB-driven workflow execution engine.

Extracts the common patterns across InfraForge's multi-step pipelines
(service onboarding, template composition, template deployment) into
reusable components.  Workflows are defined in the database
(``orchestration_processes`` + ``process_steps``) and driven by this
engine at runtime.

Three key abstractions
======================

PipelineRunner
    Reads process definitions from the DB, resolves each step's registered
    handler, manages step routing (on_success / on_failure), and yields
    NDJSON progress events to the caller.

PipelineContext
    Mutable shared state that flows through every step: the current
    template, version info, model routing, healing state, accumulated
    artifacts, and cleanup tracking.

HealingLoop
    The retry-with-LLM-healing pattern used across all validation
    phases.  Given a list of *check functions*, it runs them in order.
    If any check raises ``StepFailure(healable=True)``, the loop calls
    the registered heal function, updates the template, and restarts
    from the first check.  A shared attempt counter prevents infinite
    retries.

Separation of concerns
======================

* **WHAT** to do → stored in the DB (process steps, ordering, routing)
* **HOW** to do it → registered Python handlers (the actual logic)
* **WHEN** to heal → the runner + HealingLoop manage retry budgets
* **WHEN** to clean up → registered finalizers run in ``finally``

This lets platform engineers edit workflow definitions (reorder steps,
change routing, adjust retry limits) without touching handler code,
while keeping the execution logic testable and type-safe.

Usage sketch::

    runner = PipelineRunner()

    @runner.step("generate_arm")
    async def generate_arm(ctx, step):
        ctx.template = await generate(...)
        yield emit("progress", "generated", "Done", ctx.progress(1.0))

    @runner.step("validate_arm_deploy")
    async def validate(ctx, step):
        loop = HealingLoop(ctx)
        async for line in loop.run([static_check, what_if, deploy]):
            yield line

    # In the endpoint:
    ctx = PipelineContext("service_onboarding", service_id="...")
    async for line in runner.execute(ctx):
        yield line
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    Optional,
)

logger = logging.getLogger("infraforge.pipeline")

# ── Active pipeline registry (for abort signaling) ──────────
# run_id → PipelineContext for all currently executing pipelines.
# Entries are added when execute() starts and removed in its finally block.
_active_pipelines: dict[str, "PipelineContext"] = {}


# ══════════════════════════════════════════════════════════════
# NDJSON EVENT HELPER
# ══════════════════════════════════════════════════════════════

def emit(
    type: str,
    phase: str,
    detail: str,
    progress: float = 0.0,
    **extra: Any,
) -> str:
    """Create an NDJSON event line compatible with InfraForge's event protocol.

    Every event has ``type``, ``phase``, ``detail``, and ``progress``.
    Additional fields (``step``, ``result``, ``meta``, etc.) can be
    passed as keyword arguments.

    Returns a JSON string terminated by ``\\n`` — ready to yield from an
    async generator powering a ``StreamingResponse``.
    """
    d: dict[str, Any] = {
        "type": type,
        "phase": phase,
        "detail": detail,
        "progress": round(progress, 4),
    }
    d.update(extra)
    return json.dumps(d) + "\n"


# ══════════════════════════════════════════════════════════════
# EXCEPTIONS
# ══════════════════════════════════════════════════════════════

class StepFailure(Exception):
    """Raised by step handlers to signal a failure.

    Parameters
    ----------
    error : str
        Human-readable description of what went wrong.
    healable : bool
        If ``True``, the runner may attempt LLM-based healing and retry.
    phase : str
        Optional sub-phase label (e.g. ``"static_policy"``, ``"what_if"``).
    event_type : str
        The NDJSON event type to emit (default ``"error"``).  Use
        ``"policy_blocked"`` for policy violations so the frontend can
        render guidance instead of a hard failure.
    actions : list[dict] | None
        Action buttons for the user (e.g. ``[{"id": "retry", "label":
        "Retry Pipeline", "style": "primary"}]``).  When present, the
        runner emits an ``action_required`` event instead of a bare
        ``error`` so the frontend can render interactive choices.
    failure_context : dict | None
        Opaque context dict the frontend sends back to the resolution
        endpoint so the backend can resume/retry with full state.
    """

    def __init__(
        self,
        error: str,
        *,
        healable: bool = True,
        phase: str = "",
        event_type: str = "error",
        actions: list[dict] | None = None,
        failure_context: dict | None = None,
    ):
        super().__init__(error)
        self.error = error
        self.healable = healable
        self.phase = phase
        self.event_type = event_type
        self.actions = actions
        self.failure_context = failure_context or {}


class PipelineAbort(Exception):
    """Raised to abort the entire pipeline immediately."""
    pass


# ══════════════════════════════════════════════════════════════
# ACTION-REQUIRED HELPERS
# ══════════════════════════════════════════════════════════════

_RETRY_ACTION: dict = {
    "id": "retry", "label": "Retry Pipeline",
    "description": "Re-run the pipeline from the beginning",
    "style": "primary",
}
_END_ACTION: dict = {
    "id": "end_pipeline", "label": "End Pipeline",
    "description": "Stop and review manually",
    "style": "danger",
}


def _default_actions() -> list[dict]:
    """Sensible default action buttons for any pipeline failure."""
    return [_RETRY_ACTION, _END_ACTION]


def _categorize_failure(error: str, event_type: str = "error") -> str:
    """Classify a failure for frontend display."""
    low = error.lower()
    if event_type == "policy_blocked":
        return "policy_blocked"
    if "quota" in low or "capacity" in low:
        return "quota_exceeded"
    if "not found" in low or "not available" in low:
        return "setup_broken"
    if "dependency" in low or "dependent" in low:
        return "dependency_failed"
    if "test" in low and "fail" in low:
        return "test_failure"
    return "exhausted_heals"


def _build_action_required_event(
    ctx: "PipelineContext",
    step_name: str,
    error: str,
    *,
    actions: list[dict] | None = None,
    failure_context: dict | None = None,
    event_type: str = "error",
    progress: float = 1.0,
    **extra: Any,
) -> str:
    """Build an ``action_required`` NDJSON event.

    Merges caller-supplied context with standard pipeline state so the
    resolution endpoint can restart the pipeline.
    """
    ctx_payload = {
        "service_id": ctx.service_id,
        "template_id": ctx.template_id,
        "run_id": ctx.run_id,
        "process_id": ctx.process_id,
        "region": ctx.region,
        "rg_name": ctx.rg_name,
        "step": step_name,
        "error": error[:500],
    }
    if failure_context:
        ctx_payload.update(failure_context)

    return emit(
        "action_required", step_name, error,
        progress=ctx.progress(progress) if ctx.total_steps else progress,
        failure_category=_categorize_failure(error, event_type),
        service_id=ctx.service_id,
        pipeline=ctx.process_id,
        actions=actions or _default_actions(),
        context=ctx_payload,
        **extra,
    )


# ══════════════════════════════════════════════════════════════
# STEP DEFINITION (parsed from DB)
# ══════════════════════════════════════════════════════════════

@dataclass
class StepDef:
    """A single step loaded from ``process_steps``.

    The runner reads these from the DB and matches each ``action`` to a
    registered Python handler.  The ``on_success`` / ``on_failure``
    fields control what happens next.
    """

    order: int
    name: str
    description: str
    action: str
    config: dict = field(default_factory=dict)
    on_success: str = "next"
    on_failure: str = "abort"

    @property
    def healable(self) -> bool:
        """Whether failures on this step should trigger LLM healing."""
        return self.on_failure in ("heal_and_retry", "retry_with_llm")

    @property
    def max_heal_attempts(self) -> int:
        """Maximum heal attempts from step config (default 5)."""
        return self.config.get(
            "max_heal_attempts",
            self.config.get("max_retries", 5),
        )


# ══════════════════════════════════════════════════════════════
# TYPE ALIASES
# ══════════════════════════════════════════════════════════════

# Step handler: async generator that yields NDJSON lines.
# Receives the shared context and the step definition.
# Raises StepFailure on error.
StepHandler = Callable[["PipelineContext", StepDef], AsyncGenerator[str, None]]

# Check function: used inside HealingLoop.  The ``int`` is the
# 1-based attempt number.  Yields NDJSON lines, raises StepFailure.
CheckFn = Callable[["PipelineContext", int], AsyncGenerator[str, None]]

# Heal function: takes the context + error message, returns
# (fixed_template_json_str, strategy_description).
HealFn = Callable[["PipelineContext", str], Awaitable[tuple[str, str]]]

# Finalizer: cleanup function called when the pipeline ends
# (success, failure, or cancellation).  Must not yield.
FinalizerFn = Callable[["PipelineContext"], Awaitable[None]]


# ══════════════════════════════════════════════════════════════
# PIPELINE CONTEXT
# ══════════════════════════════════════════════════════════════

class PipelineContext:
    """Mutable shared state for a pipeline execution.

    Carries everything that step handlers need: the current template,
    identity info, model routing, healing state, and accumulated
    artifacts from earlier steps.
    """

    def __init__(
        self,
        process_id: str,
        run_id: str = "",
        *,
        service_id: str = "",
        template_id: str = "",
        region: str = "eastus2",
        rg_name: str = "",
        max_heal_attempts: int = 5,
        heal_fn: Optional[HealFn] = None,
        **extra: Any,
    ):
        self.process_id = process_id
        self.run_id = run_id or uuid.uuid4().hex[:8]

        # ── Identity ─────────────────────────────────────────
        self.service_id = service_id
        self.template_id = template_id

        # ── Template state ───────────────────────────────────
        self.template: str = ""             # current ARM template (JSON string)
        self.template_json: dict = {}       # parsed dict (kept in sync by handlers)
        self.template_meta: dict = {}       # metadata extracted from template
        self.generated_policy: Optional[dict] = None

        # ── Version tracking ─────────────────────────────────
        self.version_num: Optional[int] = None
        self.semver: str = ""
        self.gen_source: str = ""           # how the template was produced

        # ── Execution environment ────────────────────────────
        self.region = region
        self.rg_name = rg_name
        self.model_routing: dict = {}

        # ── Healing state ────────────────────────────────────
        self.heal_fn = heal_fn
        self.heal_history: list[dict] = []
        self.heal_attempts: int = 0
        self.max_heal_attempts = max_heal_attempts

        # ── Artifacts ────────────────────────────────────────
        # Keyed outputs from completed steps.  Later steps can read
        # artifacts produced by earlier steps.
        self.artifacts: dict[str, Any] = {}

        # ── Progress tracking ────────────────────────────────
        self.current_step: int = 0
        self.total_steps: int = 0
        self.steps_completed: list[str] = []

        # ── Cleanup tracking ─────────────────────────────────
        self.deployed_rg: Optional[str] = None
        self.deployed_policy_info: Optional[dict] = None

        # ── Abort signal ─────────────────────────────────────
        self._abort_event: asyncio.Event = asyncio.Event()

        # ── Anything else the caller passes ──────────────────
        self.extra: dict[str, Any] = extra

    # ── Helpers ───────────────────────────────────────────────

    @property
    def abort_requested(self) -> bool:
        """Check whether an abort has been signaled for this pipeline."""
        return self._abort_event.is_set()

    def request_abort(self) -> None:
        """Signal this pipeline to stop at the next step boundary."""
        self._abort_event.set()

    def progress(self, local: float) -> float:
        """Scale a step-local progress value (0→1) to global pipeline progress.

        If the pipeline has 5 steps and we are on step 2 (0-indexed),
        ``progress(0.5)`` returns ``0.5`` (step range 0.4–0.6, midpoint).
        """
        if self.total_steps == 0:
            return local
        step_size = 1.0 / self.total_steps
        base = self.current_step * step_size
        return round(base + local * step_size, 4)

    def update_template_meta(self) -> None:
        """Re-parse ``self.template`` and refresh ``template_json`` and
        ``template_meta``.  Call after healing or regeneration.
        """
        try:
            tpl = json.loads(self.template) if isinstance(self.template, str) else self.template
            self.template_json = tpl
            resources = tpl.get("resources", [])
            self.template_meta = {
                "resource_count": len(resources),
                "resource_types": list({r.get("type", "?") for r in resources if isinstance(r, dict)}),
                "size_kb": round(len(self.template) / 1024, 1) if isinstance(self.template, str) else 0,
                "schema": tpl.get("$schema", ""),
                "parameters": list(tpl.get("parameters", {}).keys()),
                "outputs": list(tpl.get("outputs", {}).keys()),
            }
        except (json.JSONDecodeError, TypeError):
            self.template_meta = {}

    # ── Checkpoint serialization ─────────────────────────────

    def to_checkpoint(self) -> dict:
        """Serialize pipeline context to a JSON-safe dict for DB persistence.

        Captures all state needed to resume the pipeline from the next step.
        Non-serializable items (functions, LLM sessions) are excluded —
        they get re-created on resume.
        """
        # Filter artifacts to JSON-serializable values only
        safe_artifacts = {}
        for k, v in self.artifacts.items():
            try:
                json.dumps(v, default=str)
                safe_artifacts[k] = v
            except (TypeError, ValueError, OverflowError):
                pass  # skip non-serializable artifacts

        return {
            "process_id": self.process_id,
            "run_id": self.run_id,
            "service_id": self.service_id,
            "template_id": self.template_id,
            "template": self.template,
            "template_meta": self.template_meta,
            "generated_policy": self.generated_policy,
            "version_num": self.version_num,
            "semver": self.semver,
            "gen_source": self.gen_source,
            "region": self.region,
            "rg_name": self.rg_name,
            "model_routing": self.model_routing,
            "heal_history": self.heal_history,
            "heal_attempts": self.heal_attempts,
            "max_heal_attempts": self.max_heal_attempts,
            "artifacts": safe_artifacts,
            "current_step": self.current_step,
            "total_steps": self.total_steps,
            "steps_completed": self.steps_completed,
            "deployed_rg": self.deployed_rg,
            "deployed_policy_info": self.deployed_policy_info,
            "extra": {k: v for k, v in self.extra.items()
                      if isinstance(v, (str, int, float, bool, list, dict, type(None)))},
        }

    @classmethod
    def from_checkpoint(cls, data: dict, *, heal_fn: HealFn | None = None) -> "PipelineContext":
        """Reconstruct a PipelineContext from a checkpoint dict.

        Non-serializable items (heal_fn) must be supplied by the caller,
        as they can't be persisted.
        """
        ctx = cls(
            process_id=data.get("process_id", ""),
            run_id=data.get("run_id", ""),
            service_id=data.get("service_id", ""),
            template_id=data.get("template_id", ""),
            region=data.get("region", "eastus2"),
            rg_name=data.get("rg_name", ""),
            max_heal_attempts=data.get("max_heal_attempts", 5),
            heal_fn=heal_fn,
        )

        ctx.template = data.get("template", "")
        ctx.template_meta = data.get("template_meta", {})
        ctx.generated_policy = data.get("generated_policy")
        ctx.version_num = data.get("version_num")
        ctx.semver = data.get("semver", "")
        ctx.gen_source = data.get("gen_source", "")
        ctx.model_routing = data.get("model_routing", {})
        ctx.heal_history = data.get("heal_history", [])
        ctx.heal_attempts = data.get("heal_attempts", 0)
        ctx.artifacts = data.get("artifacts", {})
        ctx.current_step = data.get("current_step", 0)
        ctx.total_steps = data.get("total_steps", 0)
        ctx.steps_completed = data.get("steps_completed", [])
        ctx.deployed_rg = data.get("deployed_rg")
        ctx.deployed_policy_info = data.get("deployed_policy_info")
        ctx.extra = data.get("extra", {})

        # Re-parse template JSON if present
        if ctx.template:
            ctx.update_template_meta()

        return ctx


# ══════════════════════════════════════════════════════════════
# HEALING LOOP
# ══════════════════════════════════════════════════════════════

class HealingLoop:
    """Retry-with-LLM-healing for validation phases.

    Runs a sequence of *check functions* in order.  If any check
    raises ``StepFailure(healable=True)``, the loop calls the heal
    function to produce a corrected template and restarts from the
    first check.  A shared counter ensures we never exceed the
    maximum number of healing attempts.

    This encapsulates the pattern currently duplicated across the
    service onboarding pipeline, template validation endpoint, and
    template deployment endpoint.

    Usage::

        loop = HealingLoop(ctx, max_attempts=5)
        async for line in loop.run([parse_json, static_check, what_if, deploy]):
            yield line
    """

    def __init__(
        self,
        ctx: PipelineContext,
        max_attempts: int = 5,
        heal_fn: Optional[HealFn] = None,
    ):
        self.ctx = ctx
        self.max_attempts = max_attempts
        self.heal_fn = heal_fn or ctx.heal_fn

    async def run(
        self,
        checks: list[CheckFn],
    ) -> AsyncGenerator[str, None]:
        """Execute checks with healing retries.

        Yields NDJSON event lines for progress and healing activity.
        Raises ``StepFailure`` if all heal attempts are exhausted or a
        non-healable error is encountered.
        """
        for attempt in range(1, self.max_attempts + 1):
            is_last = attempt == self.max_attempts
            att_base = (attempt - 1) / self.max_attempts

            yield emit(
                "iteration_start", "validation",
                f"Validation pass {attempt}/{self.max_attempts}",
                progress=self.ctx.progress(att_base),
                step=attempt,
                attempt=attempt,
                max_attempts=self.max_attempts,
            )

            restarted = False
            for check in checks:
                try:
                    async for line in check(self.ctx, attempt):
                        yield line
                except StepFailure as e:
                    # ── Non-healable or last attempt → propagate ──
                    if not e.healable or is_last or not self.heal_fn:
                        raise

                    # ── Heal and restart ──
                    self.ctx.heal_attempts += 1
                    yield emit(
                        "healing", "fixing_template",
                        f"Issue in {e.phase or 'validation'}: {e.error[:200]} "
                        f"— healing (attempt {self.ctx.heal_attempts})…",
                        progress=self.ctx.progress(att_base + 0.02),
                        step=attempt,
                    )

                    new_template, strategy = await self.heal_fn(self.ctx, e.error)
                    self.ctx.template = new_template
                    self.ctx.update_template_meta()
                    self.ctx.heal_history.append({
                        "step": len(self.ctx.heal_history) + 1,
                        "phase": e.phase,
                        "error": e.error[:500],
                        "strategy": strategy,
                        "attempt": attempt,
                    })

                    yield emit(
                        "healing_done", "template_fixed",
                        f"Strategy: {strategy[:300]} — retrying…",
                        progress=self.ctx.progress(att_base + 0.03),
                        step=attempt,
                    )
                    restarted = True
                    break  # break inner check loop → restart from first check

            if not restarted:
                return  # all checks passed on this attempt

        # All attempts exhausted (shouldn't normally reach here — last
        # attempt raises in the except block above).
        raise StepFailure(
            "All healing attempts exhausted",
            healable=False,
            phase="validation",
        )


# ══════════════════════════════════════════════════════════════
# PIPELINE RUNNER
# ══════════════════════════════════════════════════════════════

class PipelineRunner:
    """Orchestrates pipeline execution from DB process definitions.

    The runner loads step definitions from the ``orchestration_processes``
    and ``process_steps`` tables, then calls the registered Python
    handler for each step's ``action``.  Routing between steps is
    controlled by the DB-defined ``on_success`` and ``on_failure``
    fields.

    Handlers are registered with the ``@runner.step("action")``
    decorator or the ``register_handler()`` method.  A healing function
    (for LLM-based template fixing) is registered with ``@runner.healer``
    or ``set_healer()``.  Cleanup functions are registered with
    ``@runner.finalizer`` or ``add_finalizer()``.

    Example::

        runner = PipelineRunner()

        @runner.step("create_service_entry")
        async def create_entry(ctx, step):
            await upsert_service(ctx.service_id, ...)
            yield emit("progress", "created", "Service registered")

        @runner.healer
        async def heal(ctx, error):
            fixed, strategy = await llm_fix(ctx.template, error)
            return fixed, strategy

        @runner.finalizer
        async def cleanup(ctx):
            if ctx.deployed_rg:
                await delete_rg(ctx.deployed_rg)

        # Execute:
        ctx = PipelineContext("service_onboarding", service_id="...")
        async for line in runner.execute(ctx):
            yield line
    """

    def __init__(self):
        self._handlers: dict[str, StepHandler] = {}
        self._heal_fn: Optional[HealFn] = None
        self._finalizers: list[FinalizerFn] = []

    # ── Registration API ─────────────────────────────────────

    def step(self, action: str):
        """Decorator: register a handler for a step action.

        Usage::

            @runner.step("generate_arm")
            async def generate(ctx, step):
                ...
                yield emit(...)
        """
        def decorator(fn: StepHandler) -> StepHandler:
            self._handlers[action] = fn
            return fn
        return decorator

    def register_handler(self, action: str, handler: StepHandler):
        """Imperatively register a step handler."""
        self._handlers[action] = handler

    def healer(self, fn: HealFn) -> HealFn:
        """Decorator: register the healing function."""
        self._heal_fn = fn
        return fn

    def set_healer(self, fn: HealFn):
        """Imperatively register the healing function."""
        self._heal_fn = fn

    def finalizer(self, fn: FinalizerFn) -> FinalizerFn:
        """Decorator: register a cleanup function."""
        self._finalizers.append(fn)
        return fn

    def add_finalizer(self, fn: FinalizerFn):
        """Imperatively register a cleanup function."""
        self._finalizers.append(fn)

    # ── Introspection ────────────────────────────────────────

    @property
    def registered_actions(self) -> set[str]:
        """Return the set of action names that have registered handlers."""
        return set(self._handlers.keys())

    def has_handler(self, action: str) -> bool:
        """Check whether a handler is registered for the given action."""
        return action in self._handlers

    # ── Step loading ─────────────────────────────────────────

    async def _load_steps(self, process_id: str) -> list[StepDef]:
        """Load and parse step definitions from the database."""
        from src.database import get_process

        proc = await get_process(process_id)
        if not proc:
            raise PipelineAbort(
                f"Process '{process_id}' not found in the database"
            )

        steps: list[StepDef] = []
        for raw in proc.get("steps", []):
            config = raw.get("config_json", "{}")
            if isinstance(config, str):
                try:
                    config = json.loads(config)
                except json.JSONDecodeError:
                    config = {}

            steps.append(StepDef(
                order=raw["step_order"],
                name=raw["name"],
                description=raw.get("description", ""),
                action=raw["action"],
                config=config,
                on_success=raw.get("on_success", "next"),
                on_failure=raw.get("on_failure", "abort"),
            ))

        steps.sort(key=lambda s: s.order)
        return steps

    # ── Routing ──────────────────────────────────────────────

    @staticmethod
    def _resolve_target(
        target: str,
        steps: list[StepDef],
        current_idx: int,
    ) -> Optional[int]:
        """Resolve a routing target string to a step index.

        Returns ``None`` for ``"done"`` (pipeline complete), the next
        index for ``"next"``, or the index of the matching step for
        ``"step_N"`` targets.
        """
        if target == "done":
            return None
        if target == "next":
            return current_idx + 1
        if target.startswith("step_"):
            try:
                target_order = int(target.split("_")[1])
                for i, s in enumerate(steps):
                    if s.order == target_order:
                        return i
            except (ValueError, IndexError):
                pass
            logger.warning(
                f"Could not resolve routing target '{target}', "
                f"falling back to next step"
            )
            return current_idx + 1
        # Unknown → next
        return current_idx + 1

    # ── Execution ────────────────────────────────────────────

    async def _checkpoint_step(
        self,
        ctx: PipelineContext,
        step_name: str,
        step_index: int,
        step_start: float,
        status: str = "completed",
    ) -> None:
        """Persist a checkpoint after a step completes (best-effort)."""
        try:
            import time as _time
            from src.database import save_pipeline_checkpoint, save_pipeline_context

            duration = _time.monotonic() - step_start
            ctx_data = ctx.to_checkpoint()

            await save_pipeline_checkpoint(
                run_id=ctx.run_id,
                step_name=step_name,
                step_index=step_index,
                status=status,
                artifacts_json=json.dumps(ctx_data.get("artifacts", {}), default=str),
                duration_secs=round(duration, 2),
            )
            await save_pipeline_context(
                run_id=ctx.run_id,
                last_completed_step=step_index,
                context_json=json.dumps(ctx_data, default=str),
            )
        except Exception as e:
            logger.debug(f"Checkpoint save failed (non-fatal): {e}")

    async def execute(
        self,
        ctx: PipelineContext,
        *,
        resume_from_step: int | None = None,
    ) -> AsyncGenerator[str, None]:
        """Execute the pipeline, yielding NDJSON event lines.

        Loads the step definitions for ``ctx.process_id`` from the DB,
        calls each step's registered handler, and manages routing,
        healing, and cleanup.

        After each step completes, a checkpoint is persisted to the DB
        so the pipeline can be resumed from that point if the server
        restarts.

        Parameters
        ----------
        resume_from_step : int | None
            If set, skip steps before this index and resume execution.
            The caller is responsible for reconstructing ``ctx`` from
            checkpoint data before calling this.

        The caller wraps this in a ``StreamingResponse``::

            return StreamingResponse(
                runner.execute(ctx),
                media_type="application/x-ndjson",
            )
        """
        import time as _time

        # Inject the healer into the context if not already set
        if self._heal_fn and not ctx.heal_fn:
            ctx.heal_fn = self._heal_fn

        try:
            steps = await self._load_steps(ctx.process_id)
        except PipelineAbort as e:
            yield _build_action_required_event(
                ctx, "pipeline_init", str(e),
                event_type="error", progress=0.0,
            )
            return

        ctx.total_steps = len(steps)

        is_resume = resume_from_step is not None
        start_label = "Resuming" if is_resume else "Starting"
        resume_detail = ""
        if is_resume and resume_from_step < len(steps):
            resume_detail = f" from step {resume_from_step + 1}: {steps[resume_from_step].name}"

        # Register in active pipeline registry for abort signaling
        _active_pipelines[ctx.run_id] = ctx

        yield emit(
            "progress", "pipeline_start",
            f"{start_label} pipeline '{ctx.process_id}'{resume_detail} "
            f"— {len(steps)} step(s)",
            progress=0.0,
            resumed=is_resume,
            run_id=ctx.run_id,
        )

        try:
            step_idx = resume_from_step if is_resume else 0
            while step_idx < len(steps):
                step = steps[step_idx]
                ctx.current_step = step_idx
                step_start = _time.monotonic()

                handler = self._handlers.get(step.action)
                if not handler:
                    msg = f"No handler registered for action '{step.action}'"
                    yield emit(
                        "warning", step.name, msg,
                        progress=ctx.progress(0),
                    )
                    if step.on_failure == "abort":
                        yield _build_action_required_event(
                            ctx, step.name, msg,
                        )
                        return
                    step_idx += 1
                    continue

                # Log step start
                logger.info(
                    f"[Pipeline:{ctx.process_id}] Step {step.order}: "
                    f"{step.name} (action={step.action})"
                    f"{' [RESUMED]' if is_resume and step_idx == resume_from_step else ''}"
                )

                try:
                    # ── Check for user-initiated abort ────────
                    if ctx.abort_requested:
                        logger.info(f"[Pipeline:{ctx.process_id}] Abort requested before step '{step.name}'")
                        yield emit(
                            "aborted", step.name,
                            "Pipeline stopped by user",
                            progress=ctx.progress(0),
                        )
                        return

                    async for line in handler(ctx, step):
                        yield line

                    # ── Step succeeded ────────────────────────
                    ctx.steps_completed.append(step.name)

                    # Persist checkpoint (best-effort, non-blocking)
                    await self._checkpoint_step(ctx, step.name, step_idx, step_start)

                    next_idx = self._resolve_target(
                        step.on_success, steps, step_idx,
                    )
                    if next_idx is None:
                        return  # "done"
                    step_idx = next_idx

                except StepFailure as e:
                    logger.warning(
                        f"[Pipeline:{ctx.process_id}] Step '{step.name}' "
                        f"failed: {e.error[:200]} (healable={e.healable}, "
                        f"on_failure={step.on_failure})"
                    )

                    target = step.on_failure

                    # Attempt healing for healable failures
                    if (
                        e.healable
                        and target in ("heal_and_retry", "retry_with_llm")
                        and ctx.heal_fn
                        and ctx.heal_attempts < ctx.max_heal_attempts
                    ):
                        ctx.heal_attempts += 1
                        yield emit(
                            "healing", step.name,
                            f"Step '{step.name}' failed — healing template "
                            f"(attempt {ctx.heal_attempts}/{ctx.max_heal_attempts})",
                            progress=ctx.progress(0.5),
                        )
                        new_template, strategy = await ctx.heal_fn(ctx, e.error)
                        ctx.template = new_template
                        ctx.update_template_meta()
                        ctx.heal_history.append({
                            "step": len(ctx.heal_history) + 1,
                            "phase": e.phase or step.name,
                            "error": e.error[:500],
                            "strategy": strategy,
                        })
                        yield emit(
                            "healing_done", step.name,
                            f"Strategy: {strategy[:300]} — retrying",
                            progress=ctx.progress(0.6),
                        )
                        continue  # retry this step

                    # Non-healable or exhausted — route on_failure
                    if target in ("abort", "mark_failed"):
                        # Save checkpoint before aborting so resume is possible
                        await self._checkpoint_step(
                            ctx, step.name, step_idx, step_start, status="failed"
                        )
                        yield _build_action_required_event(
                            ctx, step.name, e.error,
                            actions=e.actions,
                            failure_context=e.failure_context,
                            event_type=e.event_type,
                        )
                        return

                    if target == "report_gap":
                        yield emit(
                            "warning", step.name,
                            f"Gap reported: {e.error[:300]}",
                            progress=ctx.progress(1.0),
                        )
                        step_idx += 1
                    elif target in ("skip", "next"):
                        yield emit(
                            "progress", step.name,
                            f"Skipped: {e.error[:200]}",
                            progress=ctx.progress(1.0),
                        )
                        step_idx += 1
                    else:
                        next_idx = self._resolve_target(
                            target, steps, step_idx,
                        )
                        if next_idx is None:
                            return
                        step_idx = next_idx

                except PipelineAbort as e:
                    yield _build_action_required_event(
                        ctx, step.name, str(e),
                    )
                    return

                except Exception as e:
                    logger.error(
                        f"[Pipeline:{ctx.process_id}] Unexpected error "
                        f"in step '{step.name}': {e}",
                        exc_info=True,
                    )
                    yield _build_action_required_event(
                        ctx, step.name,
                        f"Internal error: {str(e)[:300]}",
                    )
                    if step.on_failure == "abort":
                        return
                    step_idx += 1

            # All steps completed
            yield emit(
                "done", "pipeline_complete",
                f"Pipeline '{ctx.process_id}' completed successfully "
                f"— {len(ctx.steps_completed)} step(s) executed",
                progress=1.0,
            )

        except (GeneratorExit, asyncio.CancelledError):
            logger.warning(
                f"[Pipeline:{ctx.process_id}] Cancelled"
            )
            # Cannot yield from inside GeneratorExit handler

        finally:
            _active_pipelines.pop(ctx.run_id, None)
            for fn in self._finalizers:
                try:
                    await fn(ctx)
                except Exception as e:
                    logger.debug(f"Finalizer error: {e}")
