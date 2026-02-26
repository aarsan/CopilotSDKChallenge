"""
Template Validation Pipeline — migrated from web.py's ``validate_template`` endpoint.

Exports ``stream_validation()`` — an async generator that yields NDJSON lines
compatible with the existing frontend event protocol:

  {"phase": "starting",  "detail": "...", "deployment_name": ...}
  {"phase": "step",      "step": N, "detail": "..."}
  {"phase": "healing",   "detail": "...", "error_summary": ...}
  {"phase": "healed",    "detail": "...", "fix_summary": ...}
  {"phase": "complete",  "status": "succeeded|failed", ...}
  {"phase": "cleanup",   ...}
  {"phase": "cleanup_done|cleanup_warning", ...}

Deep-healing events for blueprints are forwarded from
``_deep_heal_composed_template`` transparently.

The endpoint still lives in web.py — it just calls this generator now.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

from src.pipeline_helpers import (
    brief_azure_error,
    summarize_fix,
    extract_param_values,
    copilot_heal_template,
    build_final_params,
)

logger = logging.getLogger("infraforge.pipeline.validation")


# ══════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════

async def stream_validation(
    *,
    template_id: str,
    template_name: str,
    version_num: int,
    tpl: dict,
    final_params: dict,
    user_params: dict,
    rg_name: str,
    deployment_name: str,
    region: str = "eastus2",
    is_blueprint: bool = False,
    svc_ids: list[str] | None = None,
) -> AsyncGenerator[str, None]:
    """Run the full validation pipeline with self-healing.

    Yields NDJSON event lines compatible with the existing frontend.
    """
    from src.tools.deploy_engine import execute_deployment

    MAX_HEAL = 5
    DEEP_HEAL_AFTER = 2
    heal_history: list[dict] = []
    current_tpl = tpl
    current_params = dict(final_params)
    current_deploy_name = deployment_name
    deep_healed = False
    final_tpl = None
    final_status = "failed"

    yield json.dumps({
        "phase": "starting",
        "detail": f"Alright, let me spin up a temporary environment to test '{template_name}'…",
        "deployment_name": deployment_name,
        "resource_group": rg_name,
        "region": region,
        "is_blueprint": is_blueprint,
        "mode": "validation",
    }) + "\n"

    for attempt in range(1, MAX_HEAL + 1):
        is_last = attempt == MAX_HEAL
        events: list[dict] = []

        async def _on_progress(event):
            events.append(event)

        if attempt > 1:
            current_deploy_name = f"infraforge-val-{uuid.uuid4().hex[:8]}"

        # ── Step detail ──
        if attempt == 1:
            step_detail = "Deploying your template to Azure — let's see how it goes…"
            step_context = "initial"
        elif deep_healed and attempt == (heal_history[-1]["step"] + 1 if heal_history else attempt):
            step_detail = "I've rebuilt the template with fixed service components — verifying the result…"
            step_context = "verify_deep_heal"
        else:
            last_fix = heal_history[-1]["fix_summary"] if heal_history else "adjustments"
            step_detail = f"Applied fix ({last_fix}) — deploying the updated template…"
            step_context = "retry"

        yield json.dumps({
            "phase": "step",
            "step": attempt,
            "detail": step_detail,
            "context": step_context,
        }) + "\n"

        try:
            result = await execute_deployment(
                resource_group=rg_name,
                template=current_tpl,
                parameters=current_params,
                region=region,
                deployment_name=current_deploy_name,
                initiated_by="validation",
                on_progress=_on_progress,
                template_id=template_id,
                template_name=template_name,
            )
        except Exception as exc:
            result = {"status": "failed", "error": str(exc)}

        for ev in events:
            yield json.dumps(ev) + "\n"

        status = result.get("status", "unknown")

        # ── SUCCESS ──
        if status == "succeeded":
            final_tpl = current_tpl
            final_status = "validated"
            issues_resolved = len(heal_history)
            provisioned = result.get("provisioned_resources", [])

            yield json.dumps({
                "phase": "deploy_succeeded",
                "status": "succeeded",
                "issues_resolved": issues_resolved,
                "deployment_id": result.get("deployment_id"),
                "provisioned_resources": provisioned,
                "outputs": result.get("outputs", {}),
                "healed": issues_resolved > 0,
                "deep_healed": deep_healed,
            }) + "\n"

            # ── Infrastructure Testing ──
            # Enumerate resources with full properties for test generation
            resource_details = []
            try:
                from src.tools.deploy_engine import _get_resource_client
                rc = _get_resource_client()
                loop = asyncio.get_event_loop()
                live_resources = await loop.run_in_executor(
                    None, lambda: list(rc.resources.list_by_resource_group(rg_name))
                )
                for r in live_resources:
                    detail = {
                        "id": r.id, "name": r.name, "type": r.type,
                        "location": r.location,
                        "tags": dict(r.tags) if r.tags else {},
                    }
                    try:
                        full = await loop.run_in_executor(
                            None,
                            lambda r=r: rc.resources.get_by_id(r.id, api_version="2023-07-01"),
                        )
                        if full.properties:
                            detail["properties"] = full.properties
                    except Exception:
                        pass
                    resource_details.append(detail)
            except Exception as e:
                logger.warning(f"Resource enumeration failed (non-fatal): {e}")
                # Fall back to basic provisioned list
                resource_details = provisioned

            # Run the infrastructure testing pipeline
            testing_passed = True
            test_feedback = None
            try:
                from src.pipelines.testing import stream_infra_testing
                async for test_line in stream_infra_testing(
                    arm_template=current_tpl,
                    resource_group=rg_name,
                    deployed_resources=resource_details,
                    region=region,
                ):
                    yield test_line
                    # Check if testing produced a fix_template feedback
                    try:
                        evt = json.loads(test_line)
                        if evt.get("phase") == "testing_complete" and evt.get("status") == "failed":
                            testing_passed = False
                        if evt.get("phase") == "testing_feedback" and evt.get("action") == "fix_template":
                            test_feedback = evt
                    except (json.JSONDecodeError, KeyError):
                        pass
            except Exception as e:
                logger.warning(f"Infrastructure testing error (non-fatal): {e}")
                yield json.dumps({
                    "phase": "testing_complete",
                    "status": "skipped",
                    "detail": f"Testing pipeline error: {e}",
                    "tests_passed": 0,
                    "tests_failed": 0,
                }) + "\n"

            # If tests failed and analysis says fix_template, feed back
            # for another heal attempt (if we have attempts left)
            if test_feedback and not is_last:
                test_error = test_feedback.get("fix_guidance", "Infrastructure tests failed")
                yield json.dumps({
                    "phase": "healing",
                    "detail": f"Infrastructure tests found issues — adjusting the template: {test_error[:300]}",
                    "error_summary": test_error[:500],
                }) + "\n"

                pre_fix = json.dumps(current_tpl, indent=2) if isinstance(current_tpl, dict) else str(current_tpl)
                try:
                    from src.pipeline_helpers import copilot_heal_template, extract_param_values
                    _heal_params = extract_param_values(
                        current_tpl if isinstance(current_tpl, dict) else json.loads(pre_fix)
                    )
                    fixed_json = await copilot_heal_template(
                        content=pre_fix,
                        error=f"Infrastructure tests failed: {test_error}",
                        previous_attempts=heal_history,
                        parameters=_heal_params,
                    )
                    from src.pipeline_helpers import summarize_fix, build_final_params
                    fix_summary = summarize_fix(pre_fix, fixed_json)
                    heal_history.append({
                        "step": len(heal_history) + 1,
                        "phase": "infra_testing",
                        "error": test_error[:500],
                        "fix_summary": fix_summary,
                    })
                    current_tpl = json.loads(fixed_json)
                    current_params = build_final_params(current_tpl, user_params)
                    final_status = "failed"  # Will retry

                    yield json.dumps({
                        "phase": "healed",
                        "detail": f"Template adjusted based on test feedback: {fix_summary}",
                        "fix_summary": fix_summary,
                    }) + "\n"
                    continue  # Retry the deploy + test loop
                except Exception as heal_err:
                    logger.warning(f"Test-feedback healing failed: {heal_err}")
                    # Fall through to complete

            # Emit final completion
            yield json.dumps({
                "phase": "complete",
                "status": "succeeded" if testing_passed else "tested_with_issues",
                "issues_resolved": issues_resolved,
                "deployment_id": result.get("deployment_id"),
                "provisioned_resources": provisioned,
                "outputs": result.get("outputs", {}),
                "healed": issues_resolved > 0,
                "deep_healed": deep_healed,
                "testing_passed": testing_passed,
            }) + "\n"
            break

        # ── FAILURE ──
        error_msg = result.get("error") or result.get("detail") or "Unknown deployment error"

        if is_last:
            yield json.dumps({
                "phase": "complete",
                "status": "failed",
                "issues_resolved": len(heal_history),
                "deployment_id": result.get("deployment_id"),
                "error": error_msg,
                "detail": "I've tried everything I can think of, but this one's beyond what I can auto-fix. You may need to review the template manually.",
                "heal_history": [
                    {"error": h["error"][:200], "fix_summary": h["fix_summary"]}
                    for h in heal_history
                ],
            }) + "\n"
            break

        # ── DEEP HEALING (for blueprints) ──
        if is_blueprint and svc_ids and attempt >= DEEP_HEAL_AFTER and not deep_healed:
            yield json.dumps({
                "phase": "deep_heal_trigger",
                "detail": (
                    "Hmm, simple fixes aren't cutting it. Let me dig deeper — "
                    "I'll look at the individual service templates to find the root cause…"
                ),
                "service_ids": svc_ids,
            }) + "\n"

            deep_events: list[dict] = []

            async def _on_deep_event(evt):
                deep_events.append(evt)

            try:
                from src.web import _deep_heal_composed_template
                fixed_composed = await _deep_heal_composed_template(
                    template_id=template_id,
                    service_ids=svc_ids,
                    error_msg=error_msg,
                    current_template=current_tpl,
                    region=region,
                    on_event=_on_deep_event,
                )
            except Exception as dh_err:
                fixed_composed = None
                deep_events.append({
                    "phase": "deep_heal_fail",
                    "detail": f"Deep healing error: {dh_err}",
                })

            for de in deep_events:
                yield json.dumps(de) + "\n"

            if fixed_composed:
                deep_healed = True
                current_tpl = fixed_composed
                current_params = build_final_params(current_tpl, user_params)

                heal_history.append({
                    "step": len(heal_history) + 1,
                    "phase": "deep_heal",
                    "error": error_msg[:500],
                    "fix_summary": "Deep analysis: fixed underlying service templates and recomposed",
                })

                yield json.dumps({
                    "phase": "healed",
                    "detail": "I've rebuilt the template with the fixed services — let me verify it works now…",
                    "fix_summary": "Deep analysis: fixed underlying service templates and recomposed",
                    "deep_healed": True,
                }) + "\n"
                continue

            yield json.dumps({
                "phase": "deep_heal_fallback",
                "detail": "The deep fix didn't pan out — let me try another approach…",
            }) + "\n"

        # ── SHALLOW HEAL ──
        _err_code_match = re.search(r'\(([A-Za-z]+)\)', error_msg)
        _err_code = _err_code_match.group(1) if _err_code_match else None
        _prev_err_codes = []
        for _h in heal_history:
            _m = re.search(r'\(([A-Za-z]+)\)', _h.get("error", ""))
            if _m:
                _prev_err_codes.append(_m.group(1))
        _same_error_count = _prev_err_codes.count(_err_code) if _err_code else 0

        _error_brief = brief_azure_error(error_msg)
        _what_was_tried = [h["fix_summary"] for h in heal_history] if heal_history else []

        if _same_error_count >= 2:
            yield json.dumps({
                "phase": "healing",
                "detail": f"This '{_err_code}' error keeps recurring ({_same_error_count + 1} times). The previous approaches didn't resolve it — trying a fundamentally different strategy…",
                "error_summary": error_msg[:300],
                "error_brief": _error_brief,
                "what_was_tried": _what_was_tried,
                "repeated_error": True,
                "error_code": _err_code,
                "occurrence": _same_error_count + 1,
            }) + "\n"
        else:
            yield json.dumps({
                "phase": "healing",
                "detail": f"{_error_brief}. Analyzing the root cause and adjusting the template…",
                "error_summary": error_msg[:300],
                "error_brief": _error_brief,
                "what_was_tried": _what_was_tried,
            }) + "\n"

        pre_fix = json.dumps(current_tpl, indent=2) if isinstance(current_tpl, dict) else str(current_tpl)
        try:
            _heal_params = extract_param_values(
                current_tpl if isinstance(current_tpl, dict) else json.loads(pre_fix)
            )
            fixed_json = await copilot_heal_template(
                content=pre_fix,
                error=error_msg,
                previous_attempts=heal_history,
                parameters=_heal_params,
            )
            fixed_tpl = json.loads(fixed_json)
        except Exception as heal_err:
            yield json.dumps({
                "phase": "complete",
                "status": "failed",
                "error": error_msg,
                "detail": f"I wasn't able to figure out a fix for this one. The error is a bit tricky: {heal_err}",
            }) + "\n"
            final_status = "failed"
            break

        fix_summary = summarize_fix(pre_fix, fixed_json)
        heal_history.append({
            "step": len(heal_history) + 1,
            "phase": "deploy",
            "error": error_msg[:500],
            "fix_summary": fix_summary,
        })

        current_tpl = fixed_tpl
        current_params = build_final_params(current_tpl, user_params)

        yield json.dumps({
            "phase": "healed",
            "detail": f"Got it — {fix_summary}",
            "fix_summary": fix_summary,
            "error_brief": _error_brief,
        }) + "\n"

    # ── Post-loop: update DB status and save healed template ──
    yield json.dumps({
        "phase": "cleanup",
        "detail": "Cleaning up — removing the temporary resource group…",
    }) + "\n"

    from src.database import update_template_validation_status

    validation_results = {
        "resource_group": rg_name,
        "region": region,
        "parameters_used": final_params,
        "validation_passed": final_status == "validated",
        "heal_history": heal_history,
        "deep_healed": deep_healed,
    }
    await update_template_validation_status(
        template_id, version_num, final_status, validation_results
    )

    # Save healed template back
    if final_status == "validated" and final_tpl and (heal_history or deep_healed):
        fixed_content = json.dumps(final_tpl, indent=2)
        from src.database import get_backend as _get_hb
        _hb = await _get_hb()
        await _hb.execute_write(
            """UPDATE template_versions
               SET arm_template = ?
               WHERE template_id = ? AND version = ?""",
            (fixed_content, template_id, version_num),
        )
        await _hb.execute_write(
            """UPDATE catalog_templates
               SET content = ?, updated_at = ?
               WHERE id = ?""",
            (fixed_content, datetime.now(timezone.utc).isoformat(), template_id),
        )

    # Sync parent template status
    from src.database import get_backend as _get_val_backend
    _vb = await _get_val_backend()
    await _vb.execute_write(
        "UPDATE catalog_templates SET status = ?, updated_at = ? WHERE id = ?",
        (final_status, datetime.now(timezone.utc).isoformat(), template_id),
    )

    # Cleanup RG (fire-and-forget)
    try:
        from src.tools.deploy_engine import _get_resource_client
        client = _get_resource_client()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: client.resource_groups.begin_delete(rg_name)
        )
        yield json.dumps({
            "phase": "cleanup_done",
            "detail": "All cleaned up — temporary resources are being removed.",
        }) + "\n"
    except Exception:
        yield json.dumps({
            "phase": "cleanup_warning",
            "detail": f"Heads up — I couldn't clean up the temp resource group automatically. You may want to delete '{rg_name}' manually.",
        }) + "\n"
