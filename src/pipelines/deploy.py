"""
Template Deploy Pipeline — migrated from web.py's ``deploy_template`` endpoint.

Exports ``stream_deploy()`` — an async generator that yields NDJSON lines
using the same **phase-based event protocol** as the validation pipeline,
so the frontend's flowchart renderer (``_renderDeployProgress``) can
render deploy runs identically:

  {"phase": "starting",        "resource_group": "...", "region": "..."}
  {"phase": "step",            "detail": "..."}
  {"phase": "error",           "error": "..."}
  {"phase": "healing",         "detail": "...", "error_brief": "..."}
  {"phase": "healed",          "detail": "...", "fix_summary": "..."}
  {"phase": "complete",        "status": "succeeded", ...}

The deploy pipeline follows the deterministic process-as-code pattern:
  1. Sanitize the template (parameter defaults, GUIDs, DNS zones)
  2. What-If validation (catches errors before spending resources)
  3. Deploy to Azure with real-time progress streaming
  4. On failure: surface-heal → deep-heal (for composed templates)
  5. On success: save healed version as new template version
  6. On exhaustion: LLM agent summarizes for the user

The LLM is used for intelligence tasks (healing, analysis) only —
process control is deterministic.

The endpoint still lives in web.py — it just calls this generator now.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from src.pipeline_helpers import (
    ensure_parameter_defaults,
    sanitize_placeholder_guids,
    sanitize_dns_zone_names,
    extract_param_values,
    summarize_fix,
    copilot_heal_template,
    is_transient_error,
)
from src.model_router import Task, get_model_for_task

logger = logging.getLogger("infraforge.pipeline.deploy")

MAX_DEPLOY_HEAL_ATTEMPTS = 5
DEEP_HEAL_THRESHOLD = 3


# ══════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════

async def stream_deploy(
    *,
    template_id: str,
    template_name: str,
    tpl: dict,
    user_params: dict,
    resource_group: str,
    deployment_name: str,
    region: str = "eastus2",
    is_blueprint: bool = False,
    service_ids: list[str] | None = None,
    target_ver: int | None = None,
    deploy_semver: str = "",
) -> AsyncGenerator[str, None]:
    """Run the full deploy pipeline with self-healing.

    Yields NDJSON event lines compatible with the existing frontend.
    """
    from src.tools.deploy_engine import execute_deployment, run_what_if
    from src.database import (
        create_template_version,
        update_template_version_status,
    )

    heal_history: list[dict] = []

    # ── STEP 1: SANITIZE ─────────────────────────────────────
    yield json.dumps({
        "phase": "starting",
        "resource_group": resource_group,
        "region": region,
        "is_blueprint": is_blueprint,
        "detail": f"Deploying **{template_name}** to `{resource_group}`…",
    }) + "\n"

    current_template_json = ensure_parameter_defaults(json.dumps(tpl, indent=2))
    current_template_json = sanitize_placeholder_guids(current_template_json)
    current_template_json = sanitize_dns_zone_names(current_template_json)
    current_template = json.loads(current_template_json)

    final_params = extract_param_values(current_template)
    final_params.update({k: v for k, v in user_params.items() if v is not None})

    for attempt in range(1, MAX_DEPLOY_HEAL_ATTEMPTS + 1):
        is_last = attempt == MAX_DEPLOY_HEAL_ATTEMPTS
        att_base = (attempt - 1) / MAX_DEPLOY_HEAL_ATTEMPTS

        # Emit a step node for each attempt
        yield json.dumps({
            "phase": "step",
            "detail": f"Deploying to Azure (attempt {attempt}/{MAX_DEPLOY_HEAL_ATTEMPTS})…" if attempt > 1 else "Deploying to Azure…",
            "context": "retry" if attempt > 1 else "",
            "progress": att_base,
        }) + "\n"

        # ── STEP 2: WHAT-IF VALIDATION ────────────────────
        yield json.dumps({
            "phase": "progress",
            "detail": "Let me check with Azure if this template will work (running What-If)…",
            "progress": att_base + 0.03 / MAX_DEPLOY_HEAL_ATTEMPTS,
        }) + "\n"

        try:
            wif = await run_what_if(
                resource_group=resource_group,
                template=current_template,
                parameters=final_params,
                region=region,
            )
        except Exception as e:
            wif = {"status": "error", "errors": [str(e)]}

        if wif.get("status") != "success":
            what_if_errors = "; ".join(
                str(e) for e in wif.get("errors", [])
            ) or "Unknown What-If error"

            if is_transient_error(what_if_errors):
                yield json.dumps({
                    "phase": "progress",
                    "detail": "Azure is having a moment — I'll wait a bit and try again…",
                    "progress": att_base + 0.05 / MAX_DEPLOY_HEAL_ATTEMPTS,
                }) + "\n"
                await asyncio.sleep(10)
                continue

            # Template error — heal it
            yield json.dumps({
                "phase": "healing",
                "detail": f"Azure rejected the template — analyzing error and fixing (attempt {attempt}/{MAX_DEPLOY_HEAL_ATTEMPTS})…",
                "error_brief": what_if_errors[:200],
                "error_summary": what_if_errors[:500],
                "repeated_error": attempt > 1,
                "what_was_tried": [h["fix_summary"] for h in heal_history],
            }) + "\n"

            healed = await _run_heal_step(
                current_template, what_if_errors, heal_history,
                attempt, is_blueprint, service_ids or [], template_id, region,
            )
            if healed:
                current_template = healed["template"]
                final_params = extract_param_values(current_template)
                final_params.update({k: v for k, v in user_params.items() if v is not None})

                yield json.dumps({
                    "phase": "healed",
                    "detail": f"Got it — {healed['fix_summary']}",
                    "fix_summary": healed["fix_summary"],
                    "deep_healed": healed.get("deep", False),
                    "error_brief": what_if_errors[:200],
                }) + "\n"

                heal_history.append({
                    "step": len(heal_history) + 1,
                    "phase": "what_if",
                    "error": what_if_errors[:500],
                    "fix_summary": healed["fix_summary"],
                    "deep": healed.get("deep", False),
                    "attempt": attempt,
                })
            else:
                heal_history.append({
                    "step": len(heal_history) + 1,
                    "phase": "what_if",
                    "error": what_if_errors[:500],
                    "fix_summary": "Heal failed",
                    "attempt": attempt,
                })
                if is_last:
                    break
                yield json.dumps({
                    "phase": "progress",
                    "detail": "Couldn't fix the What-If error this time — trying a different angle…",
                }) + "\n"
            continue

        # What-If passed!
        change_summary = ", ".join(
            f"{v} {k}" for k, v in wif.get("change_counts", {}).items()
        )
        yield json.dumps({
            "phase": "progress",
            "detail": f"✅ Template looks good — {change_summary or 'Azure accepted it'}",
            "progress": att_base + 0.08 / MAX_DEPLOY_HEAL_ATTEMPTS,
        }) + "\n"

        # ── STEP 3: DEPLOY ────────────────────────────────
        deploy_name_i = (
            deployment_name if attempt == 1
            else f"{deployment_name}-r{attempt}"
        )

        progress_queue: asyncio.Queue = asyncio.Queue()

        async def _on_progress(event):
            await progress_queue.put(event)

        deploy_task = asyncio.create_task(
            execute_deployment(
                resource_group=resource_group,
                template=current_template,
                parameters=final_params,
                region=region,
                deployment_name=deploy_name_i,
                initiated_by="web-ui",
                on_progress=_on_progress,
                template_id=template_id,
                template_name=template_name,
                template_version=target_ver or 0,
                template_semver=deploy_semver,
            )
        )

        # Stream progress in real-time
        while not deploy_task.done():
            try:
                event = await asyncio.wait_for(
                    progress_queue.get(), timeout=2.0
                )
                phase = event.get("phase", "")
                if phase not in ("error",):
                    yield json.dumps({
                        "phase": "progress",
                        "detail": event.get("detail", ""),
                        "progress": att_base + (
                            event.get("progress", 0) * 0.8
                        ) / MAX_DEPLOY_HEAL_ATTEMPTS,
                    }) + "\n"
            except asyncio.TimeoutError:
                continue

        # Drain remaining
        while not progress_queue.empty():
            event = progress_queue.get_nowait()
            if event.get("phase") not in ("error",):
                yield json.dumps({
                    "phase": "progress",
                    "detail": event.get("detail", ""),
                    "progress": att_base + (
                        event.get("progress", 0) * 0.8
                    ) / MAX_DEPLOY_HEAL_ATTEMPTS,
                }) + "\n"

        try:
            result = deploy_task.result()
        except Exception as exc:
            result = {"status": "failed", "error": str(exc)}

        # ── SUCCESS ──
        if result.get("status") == "succeeded":
            if attempt > 1:
                try:
                    fixed_json = json.dumps(current_template, indent=2)
                    new_ver = await create_template_version(
                        template_id,
                        arm_template=fixed_json,
                        changelog=(
                            f"Auto-healed during deployment "
                            f"(iteration {attempt}): "
                            f"{heal_history[-1]['fix_summary'][:200]}"
                        ),
                        change_type="patch",
                        created_by="deployment-agent",
                    )
                    await update_template_version_status(
                        template_id, new_ver["version"], "approved",
                    )
                    logger.info(
                        f"Deploy pipeline saved healed template "
                        f"as version {new_ver['version']}"
                    )
                    yield json.dumps({
                        "phase": "progress",
                        "detail": f"💾 Saved the fixed template as version {new_ver['version']}.",
                    }) + "\n"
                except Exception as e:
                    logger.warning(
                        f"Failed to save healed template version: {e}"
                    )

            issues_resolved = len(heal_history)
            yield json.dumps({
                "phase": "deploy_succeeded",
                "detail": "Deployment complete",
                "provisioned_resources": result.get("provisioned_resources", []),
                "issues_resolved": issues_resolved if issues_resolved > 0 else 0,
            }) + "\n"

            yield json.dumps({
                "phase": "complete",
                "status": "succeeded",
                "step": attempt,
                "deployment_id": result.get("deployment_id"),
                "provisioned_resources": result.get(
                    "provisioned_resources", []
                ),
                "outputs": result.get("outputs", {}),
                "healed": attempt > 1,
                "issues_resolved": issues_resolved if issues_resolved > 0 else 0,
                "heal_history": heal_history,
            }) + "\n"
            return

        # ── DEPLOY FAILED → HEAL ──
        deploy_error = result.get("error") or "Unknown deployment error"

        # Try to get operation-level details
        try:
            from src.tools.deploy_engine import (
                _get_resource_client,
                _get_deployment_operation_errors,
            )
            _rc = _get_resource_client()
            _lp = asyncio.get_event_loop()
            op_errors = await _get_deployment_operation_errors(
                _rc, _lp, resource_group, deploy_name_i
            )
            if op_errors:
                deploy_error = f"{deploy_error} | Operation errors: {op_errors}"
        except Exception:
            pass

        if is_transient_error(deploy_error):
            yield json.dumps({
                "phase": "progress",
                "detail": "Azure is being flaky right now — waiting a moment before trying again…",
                "progress": att_base + 0.15 / MAX_DEPLOY_HEAL_ATTEMPTS,
            }) + "\n"
            await asyncio.sleep(10)
            continue

        if is_last:
            break

        yield json.dumps({
            "phase": "healing",
            "detail": f"The deployment hit an error — analyzing and fixing (attempt {attempt}/{MAX_DEPLOY_HEAL_ATTEMPTS})…",
            "error_brief": deploy_error[:200],
            "error_summary": deploy_error[:500],
            "repeated_error": attempt > 1,
            "what_was_tried": [h["fix_summary"] for h in heal_history],
        }) + "\n"

        healed = await _run_heal_step(
            current_template, deploy_error, heal_history,
            attempt, is_blueprint, service_ids or [], template_id, region,
        )
        if healed:
            current_template = healed["template"]
            final_params = extract_param_values(current_template)
            final_params.update({k: v for k, v in user_params.items() if v is not None})

            yield json.dumps({
                "phase": "healed",
                "detail": f"Got it — {healed['fix_summary']}",
                "fix_summary": healed["fix_summary"],
                "deep_healed": healed.get("deep", False),
                "error_brief": deploy_error[:200],
            }) + "\n"
            if healed.get("deep"):
                yield json.dumps({
                    "phase": "progress",
                    "detail": (
                        f"Had to dig deeper — the real issue was in the "
                        f"`{healed.get('culprit', '?')}` template. Fixed it, "
                        f"verified it on its own, and rebuilt the parent."
                    ),
                }) + "\n"

            heal_history.append({
                "step": len(heal_history) + 1,
                "phase": "deploy",
                "error": deploy_error[:500],
                "fix_summary": healed["fix_summary"],
                "deep": healed.get("deep", False),
                "attempt": attempt,
            })
        else:
            heal_history.append({
                "step": len(heal_history) + 1,
                "phase": "deploy",
                "error": deploy_error[:500],
                "fix_summary": "Heal failed",
                "attempt": attempt,
            })
            yield json.dumps({
                "phase": "progress",
                "detail": "Couldn't fix this particular error — trying a different approach…",
            }) + "\n"

    # ── STEP 6: EXHAUSTED → LLM summarizes ───────────────
    last_error = (
        heal_history[-1]["error"] if heal_history
        else "Unknown error"
    )

    yield json.dumps({
        "phase": "progress",
        "detail": (
            f"Tried {len(heal_history)} fix{'es' if len(heal_history) != 1 else ''} "
            f"but the issue persists. Analyzing…"
        ),
    }) + "\n"

    analysis = await _get_deploy_agent_analysis(
        last_error, template_name, resource_group, region,
        heal_history=heal_history,
    )

    yield json.dumps({
        "phase": "complete",
        "status": "needs_work",
        "step": len(heal_history),
        "deployment_id": deployment_name,
        "heal_history": heal_history,
        "analysis": analysis,
    }) + "\n"


# ══════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════

async def _run_heal_step(
    current_template: dict,
    error_msg: str,
    heal_history: list[dict],
    attempt: int,
    is_blueprint: bool,
    service_ids: list[str],
    template_id: str,
    region: str,
) -> dict | None:
    """Run one heal iteration: surface heal, or deep heal if threshold met.

    Returns:
        {"template": dict, "fix_summary": str, "deep": bool, "culprit": str}
        or None if healing failed.
    """
    surface_attempts = sum(
        1 for h in heal_history if not h.get("deep", False)
    )
    should_deep_heal = (
        is_blueprint
        and len(service_ids) > 0
        and surface_attempts >= DEEP_HEAL_THRESHOLD
    )

    if should_deep_heal:
        logger.info(
            f"Deploy pipeline: escalating to deep heal "
            f"(attempt {attempt}, {surface_attempts} surface heals exhausted)"
        )
        try:
            deep_events: list[dict] = []

            async def _capture_deep_event(evt):
                deep_events.append(evt)

            from src.web import _deep_heal_composed_template
            fixed = await _deep_heal_composed_template(
                template_id=template_id,
                service_ids=service_ids,
                error_msg=error_msg,
                current_template=current_template,
                region=region,
                on_event=_capture_deep_event,
            )
            if fixed:
                culprit = "unknown"
                for evt in deep_events:
                    if evt.get("culprit_service"):
                        culprit = evt["culprit_service"]
                        break

                fix_summary = (
                    f"Deep heal: fixed {culprit}, validated standalone, "
                    f"recomposed parent template"
                )
                return {
                    "template": fixed,
                    "fix_summary": fix_summary,
                    "deep": True,
                    "culprit": culprit,
                }
        except Exception as e:
            logger.error(f"Deep heal failed: {e}")

    # ── Surface heal: LLM fixes the ARM JSON directly ──
    try:
        pre_fix = json.dumps(current_template, indent=2)
        current_params = extract_param_values(current_template)
        fixed_content = await copilot_heal_template(
            content=pre_fix,
            error=error_msg,
            previous_attempts=heal_history,
            parameters=current_params,
        )
        fixed_template = json.loads(fixed_content)
        fix_summary = summarize_fix(pre_fix, fixed_content)
        return {
            "template": fixed_template,
            "fix_summary": fix_summary,
            "deep": False,
        }
    except Exception as e:
        logger.error(f"Surface heal failed: {e}")
        return None


async def _get_deploy_agent_analysis(
    error: str,
    template_name: str,
    resource_group: str,
    region: str,
    heal_history: list[dict] | None = None,
) -> str:
    """Ask the LLM to interpret a deployment failure after exhausting heals."""
    from src.agents import DEPLOY_FAILURE_ANALYST
    from src.web import ensure_copilot_client

    attempts = len(heal_history) if heal_history else 0
    history_text = ""
    if heal_history:
        history_text = "\n**Pipeline history:**\n"
        for h in heal_history:
            phase = h.get("phase", "deploy")
            history_text += (
                f"- Iteration {h.get('attempt', h['step'])} ({phase}): {h['error'][:150]}… "
                f"→ {h['fix_summary']}\n"
            )

    try:
        client = await ensure_copilot_client()
        if not client:
            return _fallback_deploy_analysis(error, heal_history)

        from src.copilot_helpers import copilot_send

        prompt = (
            f"A deployment of **{template_name}** to resource group "
            f"`{resource_group}` in **{region}** failed after "
            f"{attempts} pipeline iteration(s).\n\n"
            f"**Final Azure error:**\n```\n{error[:500]}\n```\n"
            f"{history_text}\n"
            f"Explain what happened and what to do next."
        )

        result = await copilot_send(
            client,
            model=get_model_for_task(Task.VALIDATION_ANALYSIS),
            system_prompt=DEPLOY_FAILURE_ANALYST.system_prompt.format(attempts=attempts),
            prompt=prompt,
            timeout=30,
            agent_name="DEPLOY_FAILURE_ANALYST",
        )
        return result or _fallback_deploy_analysis(error, heal_history)

    except Exception as e:
        logger.error(f"Deploy agent analysis failed: {e}")
        return _fallback_deploy_analysis(error, heal_history)


def _fallback_deploy_analysis(
    error: str,
    heal_history: list[dict] | None = None,
) -> str:
    """Structured message when the LLM agent isn't available."""
    attempts = len(heal_history) if heal_history else 0
    history_text = ""
    if heal_history:
        history_text = "\n\n**What the pipeline tried:**\n"
        for h in heal_history:
            history_text += f"- Iteration {h.get('attempt', h['step'])}: {h['fix_summary']}\n"

    return (
        f"The deployment pipeline tried {attempts} iteration(s) but couldn't "
        f"resolve the issue.\n\n"
        f"**Last error:**\n> {error[:300]}\n"
        f"{history_text}\n"
        f"**Suggested next steps:** Re-run validation to diagnose and fix "
        f"the underlying issue with the full healing pipeline."
    )
