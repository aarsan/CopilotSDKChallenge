"""
Template Onboarding Pipeline — full lifecycle validation of catalog templates.

Registers step handlers on a ``PipelineRunner`` that drives the end-to-end
template fix-and-validate flow:

   1. initialize              — load template, conflict check, pipeline run, model routing
   2. recompose               — (blueprint) recompose from pinned services; (standalone) verify
   3. structural_test         — 7-category structural test suite
   4. auto_heal_structural    — LLM fix for structural failures (CODE_FIXING)
   5. pre_validate            — ARM reference + expression syntax validation & auto-fix
   6. check_availability      — quota check, region selection / fallback
   7. arm_deploy              — deploy to temp RG with HealingLoop (up to 5×)
   8. infra_testing           — AI-generated Python smoke tests (CODE_GENERATION)
   9. cleanup                 — delete temp RG + deployment artifacts
  10. promote_template        — save validated version, semver bump, mark validated

The endpoint lives in web.py — it delegates to ``runner.execute(ctx)``.

LLM usage:
  Step 4  — Task.CODE_FIXING  (claude-sonnet-4)   structural heal
  Step 7  — Task.CODE_FIXING  (claude-sonnet-4)   surface heal
            Task.PLANNING     (o3-mini)            deep heal root-cause (blueprints)
  Step 8  — Task.CODE_GENERATION (claude-sonnet-4) infra test generation + analysis
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid

from src.pipeline import (
    PipelineRunner,
    PipelineContext,
    HealingLoop,
    StepDef,
    StepFailure,
    emit,
)
from src.pipeline_helpers import (
    ensure_parameter_defaults,
    sanitize_placeholder_guids,
    sanitize_dns_zone_names,
    extract_param_values,
    summarize_fix,
    brief_azure_error,
    copilot_heal_template,
    build_final_params,
    is_transient_error,
    is_quota_or_capacity_error,
    find_available_regions,
    validate_arm_references,
    validate_arm_expression_syntax,
    cleanup_rg,
)
from src.model_router import Task, get_model_for_task, get_model_display, get_task_reason

logger = logging.getLogger("infraforge.pipeline.template_onboarding")

# ── The runner instance ──────────────────────────────────────
runner = PipelineRunner()


# ══════════════════════════════════════════════════════════════
# HEALER — shared by HealingLoop in step 7
# ══════════════════════════════════════════════════════════════

@runner.healer
async def _heal_template(ctx: PipelineContext, error: str) -> tuple[str, str]:
    """Surface-heal the current ARM template via LLM."""
    tpl = ctx.template if isinstance(ctx.template, str) else json.dumps(ctx.template, indent=2)
    fixed_json = await copilot_heal_template(
        content=tpl,
        error=error,
        previous_attempts=ctx.heal_history,
        parameters=extract_param_values(
            json.loads(tpl) if isinstance(tpl, str) else tpl
        ),
    )
    strategy = summarize_fix(tpl, fixed_json)
    return fixed_json, strategy


# ══════════════════════════════════════════════════════════════
# STEP 1: INITIALIZE
# ══════════════════════════════════════════════════════════════

@runner.step("initialize_template")
async def step_initialize(ctx: PipelineContext, step: StepDef):
    """Load template, conflict guard, create pipeline run, configure model routing."""
    from src.database import (
        get_template_by_id,
        get_template_versions,
        get_template_version,
        create_pipeline_run,
        delete_template_versions_by_status,
        get_latest_semver,
    )

    template_id = ctx.template_id

    # Load the template
    tmpl = await get_template_by_id(template_id)
    if not tmpl:
        raise StepFailure(f"Template '{template_id}' not found", healable=False)

    ctx.extra["tmpl"] = tmpl
    ctx.extra["template_name"] = tmpl.get("name", template_id)
    ctx.extra["is_blueprint"] = bool(tmpl.get("is_blueprint"))

    # Parse service IDs
    svc_ids_raw = tmpl.get("service_ids") or tmpl.get("service_ids_json") or []
    if isinstance(svc_ids_raw, str):
        try:
            svc_ids = json.loads(svc_ids_raw)
        except Exception:
            svc_ids = []
    else:
        svc_ids = list(svc_ids_raw) if svc_ids_raw else []
    ctx.extra["svc_ids"] = svc_ids

    # Record pipeline run
    await create_pipeline_run(
        ctx.run_id, template_id, "template_validation",
        created_by="fix-and-validate",
    )

    # Build model routing summary
    routing = {
        "code_fixing":     {"model": get_model_for_task(Task.CODE_FIXING),     "display": get_model_display(Task.CODE_FIXING),     "reason": get_task_reason(Task.CODE_FIXING)},
        "code_generation": {"model": get_model_for_task(Task.CODE_GENERATION), "display": get_model_display(Task.CODE_GENERATION), "reason": get_task_reason(Task.CODE_GENERATION)},
        "planning":        {"model": get_model_for_task(Task.PLANNING),        "display": get_model_display(Task.PLANNING),        "reason": get_task_reason(Task.PLANNING)},
    }
    ctx.model_routing = routing

    yield emit(
        "progress", "init_model",
        "Model routing configured — each pipeline phase uses the optimal model",
        ctx.progress(0.3), model_routing=routing,
    )

    # Cleanup stale drafts/failed
    cleaned = await delete_template_versions_by_status(template_id, ["draft", "failed"])
    if cleaned:
        yield emit(
            "progress", "cleanup_drafts",
            f"Cleaned up {cleaned} stale draft/failed version(s)",
            ctx.progress(0.5),
        )

    # Pipeline overview
    yield emit(
        "progress", "pipeline_overview",
        f"Pipeline: Fix & Validate '{ctx.extra['template_name']}'",
        ctx.progress(0.7),
        steps=[
            "Load template and configure models",
            "Recompose from services" if ctx.extra["is_blueprint"] else "Verify template content",
            "Run structural test suite (7 categories)",
            "Auto-heal structural failures (if any)",
            "Pre-validate ARM references and expressions",
            "Check Azure quota and select deployment region",
            "Deploy to temp RG with self-healing loop",
            "Run AI-generated infrastructure smoke tests",
            "Clean up validation resources",
            "Promote validated template version",
        ],
    )

    yield emit("progress", "init_complete", "Initialization complete", ctx.progress(1.0))


# ══════════════════════════════════════════════════════════════
# STEP 2: RECOMPOSE (blueprint) / VERIFY (standalone)
# ══════════════════════════════════════════════════════════════

@runner.step("recompose_template")
async def step_recompose(ctx: PipelineContext, step: StepDef):
    """Blueprint: recompose from pinned service versions. Standalone: load & verify."""
    from src.database import (
        get_template_by_id, get_template_versions, get_template_version,
        get_latest_semver,
    )

    template_id = ctx.template_id
    tmpl = ctx.extra["tmpl"]
    is_blueprint = ctx.extra["is_blueprint"]
    svc_ids = ctx.extra["svc_ids"]

    if is_blueprint and svc_ids:
        yield emit(
            "progress", "recompose_start",
            "Composite template — rebuilding from pinned service template versions…",
            ctx.progress(0.1),
        )

        # Import the recompose helper from web.py
        from src.web import _recompose_with_pinned

        try:
            result = await _recompose_with_pinned(
                template_id,
                version_overrides=None,
                ignore_existing_pins=False,
                changelog="Fix & Validate: recomposed from pinned services",
                change_type="major",
                created_by="fix-and-validate",
            )
            test_results = result.get("test_results", {})
            ver = result.get("version", {})
            ctx.version_num = ver.get("version") if isinstance(ver, dict) else None
            ctx.semver = ver.get("semver", "") if isinstance(ver, dict) else ""

            # Refresh tmpl after recompose
            tmpl = await get_template_by_id(template_id)
            ctx.extra["tmpl"] = tmpl

            yield emit(
                "progress", "recompose_done",
                f"Rebuilt from {len(result.get('services_recomposed', []))} services "
                f"— {test_results.get('passed', 0)}/{test_results.get('total', 0)} structural tests pass",
                ctx.progress(0.8),
                resource_count=result.get("resource_count", 0),
                parameter_count=result.get("parameter_count", 0),
            )
        except Exception as e:
            yield emit(
                "warning", "recompose_error",
                f"Recompose failed: {e}. Falling back to current template content.",
                ctx.progress(0.5),
            )
    else:
        yield emit(
            "progress", "verify_start",
            "Standalone template — verifying content is loaded…",
            ctx.progress(0.3),
        )

    # Load the latest version's ARM content
    versions = await get_template_versions(template_id)
    if versions:
        latest = versions[0]
        ver_obj = await get_template_version(template_id, latest["version"])
        arm_content = ver_obj.get("arm_template", "") if ver_obj else ""
        ctx.version_num = latest["version"]
        ctx.semver = ver_obj.get("semver", "") if ver_obj else ""
    else:
        arm_content = tmpl.get("content", "")

    if not arm_content:
        raise StepFailure(
            "No ARM template content found — cannot validate",
            healable=False,
        )

    ctx.template = arm_content
    ctx.update_template_meta()

    yield emit(
        "progress", "template_loaded",
        f"Template loaded: {ctx.template_meta.get('resource_count', 0)} resources, "
        f"{ctx.template_meta.get('size_kb', 0)} KB",
        ctx.progress(1.0),
    )


# ══════════════════════════════════════════════════════════════
# STEP 3: STRUCTURAL TEST
# ══════════════════════════════════════════════════════════════

@runner.step("structural_test")
async def step_structural_test(ctx: PipelineContext, step: StepDef):
    """Run the 7-category structural test suite."""
    from src.web import _run_structural_tests, _update_test_status

    svc_ids = ctx.extra.get("svc_ids", [])
    expected = svc_ids if ctx.extra.get("is_blueprint") and svc_ids else None

    test_results = _run_structural_tests(ctx.template, expected_service_ids=expected)
    ctx.artifacts["structural_tests"] = test_results

    if ctx.version_num is not None:
        await _update_test_status(ctx.template_id, ctx.version_num, test_results)

    passed = test_results.get("passed", 0)
    total = test_results.get("total", 0)
    failed = test_results.get("failed", 0)

    if test_results.get("all_passed"):
        yield emit(
            "progress", "structural_pass",
            f"All {total} structural tests passed",
            ctx.progress(1.0),
            tests=test_results.get("tests", []),
        )
    else:
        # Store failures for step 4 to heal
        failures = [t for t in test_results.get("tests", []) if not t.get("passed")]
        ctx.artifacts["structural_failures"] = failures

        yield emit(
            "progress", "structural_issues",
            f"{passed}/{total} structural tests passed — {failed} issue(s) need fixing",
            ctx.progress(1.0),
            tests=test_results.get("tests", []),
        )


# ══════════════════════════════════════════════════════════════
# STEP 4: AUTO-HEAL STRUCTURAL (conditional)
# ══════════════════════════════════════════════════════════════

@runner.step("auto_heal_structural")
async def step_auto_heal_structural(ctx: PipelineContext, step: StepDef):
    """Fix structural failures from step 3 using LLM, then re-test."""
    from src.web import _run_structural_tests, _update_test_status
    from src.database import (
        upsert_template, create_template_version,
        update_template_version_status,
    )

    failures = ctx.artifacts.get("structural_failures", [])
    if not failures:
        yield emit(
            "progress", "heal_skip",
            "No structural issues — skipping auto-heal",
            ctx.progress(1.0),
        )
        return

    # Build error description from failures
    failed_tests = [
        f"- {t['name']}: {t.get('message', 'failed')}" for t in failures
    ]
    error_description = "Structural test failures:\n" + "\n".join(failed_tests)

    yield emit(
        "progress", "heal_start",
        f"Fixing {len(failures)} structural issue(s)…",
        ctx.progress(0.2),
    )

    # Try LLM healing
    fixed_arm = None
    try:
        fixed_arm = await copilot_heal_template(
            content=ctx.template,
            error=error_description,
            previous_attempts=[],
        )
    except Exception as e:
        logger.warning(f"LLM structural heal failed: {e}")

    if not fixed_arm:
        # Heuristic fallback: fix common issues
        try:
            tpl = json.loads(ctx.template) if isinstance(ctx.template, str) else ctx.template
            changed = False
            if "$schema" not in tpl:
                tpl["$schema"] = "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#"
                changed = True
            if "contentVersion" not in tpl:
                tpl["contentVersion"] = "1.0.0.0"
                changed = True
            TAG_SET = {
                "environment": "[parameters('environment')]",
                "owner": "[parameters('ownerEmail')]",
                "costCenter": "[parameters('costCenter')]",
                "project": "[parameters('projectName')]",
                "managedBy": "InfraForge",
            }
            for res in tpl.get("resources", []):
                if isinstance(res, dict):
                    if "tags" not in res or not isinstance(res.get("tags"), dict):
                        res["tags"] = dict(TAG_SET)
                        changed = True
                    else:
                        for tk, tv in TAG_SET.items():
                            if tk not in res["tags"]:
                                res["tags"][tk] = tv
                                changed = True
            if changed:
                fixed_arm = json.dumps(tpl, indent=2)
        except Exception:
            pass

    if not fixed_arm:
        raise StepFailure(
            "Could not fix structural issues automatically",
            healable=False,
        )

    ctx.template = fixed_arm
    ctx.update_template_meta()

    # Re-test
    svc_ids = ctx.extra.get("svc_ids", [])
    expected = svc_ids if ctx.extra.get("is_blueprint") and svc_ids else None
    retest = _run_structural_tests(fixed_arm, expected_service_ids=expected)
    ctx.artifacts["structural_tests"] = retest

    # Save healed version
    tmpl = ctx.extra["tmpl"]
    tmpl["content"] = fixed_arm
    await upsert_template(tmpl)
    new_ver = await create_template_version(
        ctx.template_id, fixed_arm,
        changelog="Auto-healed: fixed structural test failures",
        change_type="patch",
        created_by="auto-healer",
    )
    ctx.version_num = new_ver.get("version")
    ctx.semver = new_ver.get("semver", "")

    if ctx.version_num is not None:
        status = "passed" if retest.get("all_passed") else "failed"
        await update_template_version_status(
            ctx.template_id, ctx.version_num, status, retest,
        )

    passed = retest.get("passed", 0)
    total = retest.get("total", 0)

    yield emit(
        "progress", "heal_complete",
        f"Structural heal applied — {passed}/{total} tests now pass",
        ctx.progress(1.0),
        all_passed=retest.get("all_passed", False),
    )


# ══════════════════════════════════════════════════════════════
# STEP 5: PRE-VALIDATE (ARM references + expression syntax)
# ══════════════════════════════════════════════════════════════

@runner.step("pre_validate_arm")
async def step_pre_validate(ctx: PipelineContext, step: StepDef):
    """Validate ARM references and expression syntax; auto-fix missing refs."""
    tpl = json.loads(ctx.template) if isinstance(ctx.template, str) else ctx.template

    # Check for missing variable/parameter references
    ref_errors = validate_arm_references(tpl)
    if ref_errors:
        yield emit(
            "progress", "ref_fix",
            f"Found {len(ref_errors)} reference issue(s) — auto-fixing…",
            ctx.progress(0.3),
            issues=ref_errors[:10],
        )
        for err in ref_errors:
            if "Missing variable" in err:
                vname = err.split("'")[1]
                tpl_str = json.dumps(tpl)
                tpl_str = tpl_str.replace(
                    f"[variables('{vname}')]",
                    f"[parameters('{vname}')]",
                )
                tpl_str = tpl_str.replace(
                    f"variables('{vname}')",
                    f"parameters('{vname}')",
                )
                tpl = json.loads(tpl_str)
                tpl.setdefault("parameters", {})[vname] = {
                    "type": "string",
                    "defaultValue": f"infraforge-{vname[:20]}",
                    "metadata": {"description": f"Auto-fixed: was undefined variable '{vname}'"},
                }
            elif "Missing parameter" in err:
                pname = err.split("'")[1]
                tpl.setdefault("parameters", {})[pname] = {
                    "type": "string",
                    "defaultValue": f"infraforge-{pname[:20]}",
                    "metadata": {"description": f"Auto-added: {pname}"},
                }

    # Check ARM expression syntax
    syntax_errors = validate_arm_expression_syntax(tpl)
    if syntax_errors:
        yield emit(
            "progress", "syntax_issues",
            f"Found {len(syntax_errors)} ARM expression syntax issue(s) — attempting fix…",
            ctx.progress(0.5),
            errors=syntax_errors[:10],
        )
        # Try LLM fix for syntax errors
        try:
            error_msg = "; ".join(syntax_errors)
            fixed_json = await copilot_heal_template(
                content=json.dumps(tpl, indent=2),
                error=error_msg,
                previous_attempts=[],
                parameters=extract_param_values(tpl),
            )
            tpl = json.loads(fixed_json)
            # Re-check
            remaining = validate_arm_expression_syntax(tpl)
            if remaining:
                raise StepFailure(
                    f"ARM expression syntax errors remain after healing: {'; '.join(remaining[:3])}",
                    healable=False,
                )
        except StepFailure:
            raise
        except Exception as e:
            raise StepFailure(
                f"ARM expression validation failed and auto-healer could not fix it: {e}",
                healable=False,
            )

    ctx.template = json.dumps(tpl, indent=2)
    ctx.update_template_meta()

    yield emit(
        "progress", "pre_validate_done",
        "ARM references and expression syntax validated",
        ctx.progress(1.0),
    )


# ══════════════════════════════════════════════════════════════
# STEP 6: CHECK AVAILABILITY (quota + region)
# ══════════════════════════════════════════════════════════════

@runner.step("check_availability")
async def step_check_availability(ctx: PipelineContext, step: StepDef):
    """Check Azure quota in the target region; switch if needed."""
    region = ctx.region
    tried_regions: set[str] = {region}

    primary, alts = await find_available_regions(region)
    if primary["ok"]:
        yield emit(
            "progress", "quota_ok",
            f"Region {region} has available capacity",
            ctx.progress(1.0),
        )
        return

    # Try fallback regions
    alts = [a for a in alts if a["region"] not in tried_regions]
    if alts:
        old_region = region
        ctx.region = alts[0]["region"]
        tried_regions.add(ctx.region)

        yield emit(
            "progress", "region_fallback",
            f"Quota exceeded in {old_region} "
            f"({primary['used']}/{primary['limit']} cores) "
            f"— switching to {ctx.region}",
            ctx.progress(0.8),
            old_region=old_region,
            new_region=ctx.region,
        )
    else:
        raise StepFailure(
            f"Subscription quota exceeded in {region} "
            f"({primary['used']}/{primary['limit']} cores) "
            f"and no fallback regions have capacity",
            healable=False,
            phase="quota_exceeded",
            actions=[
                {"id": "retry", "label": "Retry Pipeline",
                 "description": "Re-run — quota may have freed up",
                 "style": "primary"},
                {"id": "end_pipeline", "label": "End Pipeline",
                 "description": "Stop and request a quota increase",
                 "style": "danger"},
            ],
        )

    yield emit(
        "progress", "availability_done",
        f"Deployment will target region: {ctx.region}",
        ctx.progress(1.0),
    )


# ══════════════════════════════════════════════════════════════
# STEP 7: ARM DEPLOY (HealingLoop with up to 5 attempts)
# ══════════════════════════════════════════════════════════════

@runner.step("arm_deploy_template")
async def step_arm_deploy(ctx: PipelineContext, step: StepDef):
    """Deploy to a temp RG with the HealingLoop (up to 5 surface-heal attempts).

    For blueprints, triggers deep healing if surface heals are exhausted.
    """
    from src.tools.deploy_engine import execute_deployment, run_what_if
    from src.database import (
        create_template_version,
        update_template_validation_status,
    )

    MAX_HEAL = step.max_heal_attempts
    is_blueprint = ctx.extra.get("is_blueprint", False)
    svc_ids = ctx.extra.get("svc_ids", [])
    template_id = ctx.template_id
    template_name = ctx.extra.get("template_name", template_id)

    # Build deployment identifiers
    rg_name = ctx.rg_name or f"infraforge-val-{uuid.uuid4().hex[:8]}"
    ctx.rg_name = rg_name
    deployment_name = f"infraforge-val-{uuid.uuid4().hex[:8]}"

    # Sanitize the template
    tpl_json = ensure_parameter_defaults(ctx.template)
    tpl_json = sanitize_placeholder_guids(tpl_json)
    tpl_json = sanitize_dns_zone_names(tpl_json)
    current_tpl = json.loads(tpl_json)
    current_params = extract_param_values(current_tpl)

    # Apply user params
    user_params = ctx.extra.get("user_params", {})
    current_params.update({k: v for k, v in user_params.items() if v is not None})

    heal_history: list[dict] = []
    deep_healed = False
    DEEP_HEAL_AFTER = 2

    yield emit(
        "progress", "deploy_start",
        f"Deploying '{template_name}' to temp RG '{rg_name}'…",
        ctx.progress(0.05),
        resource_group=rg_name,
        region=ctx.region,
    )

    for attempt in range(1, MAX_HEAL + 1):
        is_last = attempt == MAX_HEAL
        att_base = (attempt - 1) / MAX_HEAL

        if attempt > 1:
            deployment_name = f"infraforge-val-{uuid.uuid4().hex[:8]}"

        yield emit(
            "iteration_start", "arm_deploy",
            f"Deploy attempt {attempt}/{MAX_HEAL}" if attempt > 1
            else "Deploying to Azure…",
            ctx.progress(att_base),
            step=attempt,
            attempt=attempt,
            max_attempts=MAX_HEAL,
        )

        # ── Local ARM expression check ──
        syntax_errors = validate_arm_expression_syntax(current_tpl)
        if syntax_errors:
            error_msg = "; ".join(syntax_errors)
            if is_last:
                raise StepFailure(
                    f"ARM expression validation failed: {error_msg[:300]}",
                    healable=False,
                    phase="local_expression_validation",
                )
            yield emit(
                "healing", "local_fix",
                "ARM expression issue detected — fixing…",
                ctx.progress(att_base + 0.01),
                error_summary=error_msg[:500],
            )
            try:
                fixed_json = await copilot_heal_template(
                    content=json.dumps(current_tpl, indent=2),
                    error=error_msg,
                    previous_attempts=heal_history,
                    parameters=extract_param_values(current_tpl),
                )
                fix_summary = summarize_fix(json.dumps(current_tpl, indent=2), fixed_json)
                heal_history.append({
                    "step": len(heal_history) + 1,
                    "phase": "local_expression_validation",
                    "error": error_msg[:500],
                    "fix_summary": fix_summary,
                })
                current_tpl = json.loads(fixed_json)
                current_params = build_final_params(current_tpl, user_params)
                yield emit(
                    "healing_done", "local_fixed",
                    f"Fixed: {fix_summary}",
                    ctx.progress(att_base + 0.02),
                )
                continue
            except Exception as heal_err:
                raise StepFailure(
                    f"ARM expression fix failed: {heal_err}",
                    healable=False,
                    phase="local_expression_validation",
                )

        # ── What-If validation ──
        yield emit(
            "progress", "what_if",
            "Running ARM What-If (dry-run)…",
            ctx.progress(att_base + 0.03),
        )
        try:
            wif = await run_what_if(
                resource_group=rg_name,
                template=current_tpl,
                parameters=current_params,
                region=ctx.region,
            )
        except Exception as e:
            wif = {"status": "error", "errors": [str(e)]}

        if wif.get("status") != "success":
            what_if_errors = "; ".join(
                str(e) for e in wif.get("errors", [])
            ) or "Unknown What-If error"

            if is_transient_error(what_if_errors):
                yield emit(
                    "progress", "transient_wait",
                    "Azure transient error — retrying…",
                    ctx.progress(att_base + 0.05),
                )
                await asyncio.sleep(10)
                continue

            if is_quota_or_capacity_error(what_if_errors):
                primary, alts = await find_available_regions(ctx.region, force_fallback=True)
                tried = ctx.extra.setdefault("tried_regions", {ctx.region})
                alts = [a for a in alts if a["region"] not in tried]
                if alts:
                    old_region = ctx.region
                    ctx.region = alts[0]["region"]
                    tried.add(ctx.region)
                    current_params["location"] = ctx.region
                    yield emit(
                        "progress", "region_fallback",
                        f"Capacity exceeded in {old_region} — switching to {ctx.region}",
                        ctx.progress(att_base + 0.05),
                    )
                    continue
                raise StepFailure(
                    f"Quota exceeded in all tried regions. Error: {brief_azure_error(what_if_errors)}",
                    healable=False,
                    phase="quota_exceeded",
                )

            # Template error — heal
            if is_last:
                raise StepFailure(
                    f"What-If validation failed after {MAX_HEAL} attempts: {what_if_errors[:300]}",
                    healable=False,
                    phase="what_if",
                )

            yield emit(
                "healing", "what_if_fix",
                f"What-If rejected — healing (attempt {attempt}/{MAX_HEAL})…",
                ctx.progress(att_base + 0.05),
                error_brief=what_if_errors[:200],
            )

            # Deep heal for blueprints after threshold
            if is_blueprint and svc_ids and attempt >= DEEP_HEAL_AFTER and not deep_healed:
                yield emit(
                    "progress", "deep_heal_trigger",
                    "Multiple heal attempts failed — trying deep heal on component services…",
                    ctx.progress(att_base + 0.06),
                )
                from src.web import _deep_heal_composed_template

                async def _on_deep_event(evt):
                    pass  # Deep heal events are logged, not streamed in pipeline

                result = await _deep_heal_composed_template(
                    template_id, svc_ids, what_if_errors, current_tpl,
                    region=ctx.region,
                    on_event=_on_deep_event,
                )
                if result:
                    current_tpl = result
                    current_params = extract_param_values(current_tpl)
                    current_params.update({k: v for k, v in user_params.items() if v is not None})
                    deep_healed = True
                    yield emit(
                        "healing_done", "deep_healed",
                        "Deep heal succeeded — rebuilt from fixed service components",
                        ctx.progress(att_base + 0.08),
                    )
                    continue

            # Surface heal
            try:
                fixed_json = await copilot_heal_template(
                    content=json.dumps(current_tpl, indent=2),
                    error=what_if_errors,
                    previous_attempts=heal_history,
                    parameters=extract_param_values(current_tpl),
                )
                fix_summary = summarize_fix(json.dumps(current_tpl, indent=2), fixed_json)
                heal_history.append({
                    "step": len(heal_history) + 1,
                    "phase": "what_if",
                    "error": what_if_errors[:500],
                    "fix_summary": fix_summary,
                })
                current_tpl = json.loads(fixed_json)
                current_params = extract_param_values(current_tpl)
                current_params.update({k: v for k, v in user_params.items() if v is not None})
                yield emit(
                    "healing_done", "what_if_fixed",
                    f"Fix applied: {fix_summary}",
                    ctx.progress(att_base + 0.08),
                )
                continue
            except Exception:
                heal_history.append({
                    "step": len(heal_history) + 1,
                    "phase": "what_if",
                    "error": what_if_errors[:500],
                    "fix_summary": "Heal failed",
                })
                if is_last:
                    raise StepFailure(
                        f"What-If failed and healing exhausted: {what_if_errors[:300]}",
                        healable=False,
                        phase="what_if",
                    )
                continue

        # What-If passed — deploy
        change_summary = ", ".join(
            f"{v} {k}" for k, v in wif.get("change_counts", {}).items()
        )
        yield emit(
            "progress", "what_if_pass",
            f"What-If passed — {change_summary or 'template accepted'}",
            ctx.progress(att_base + 0.1),
        )

        # ── Real deployment ──
        yield emit(
            "progress", "deploying",
            "Deploying resources to Azure…",
            ctx.progress(att_base + 0.15),
        )

        events: list[dict] = []

        async def _on_progress(event):
            events.append(event)

        try:
            deploy_result = await execute_deployment(
                resource_group=rg_name,
                template=current_tpl,
                parameters=current_params,
                region=ctx.region,
                deployment_name=deployment_name,
                initiated_by="template-validation",
                on_progress=_on_progress,
            )
            deploy_status = deploy_result.get("status", "failed")
        except Exception as e:
            deploy_status = "failed"
            deploy_result = {"error": str(e)}

        if deploy_status == "succeeded":
            ctx.deployed_rg = rg_name
            ctx.template = json.dumps(current_tpl, indent=2)
            ctx.update_template_meta()
            ctx.heal_history = heal_history

            # Store deploy artifacts for later steps
            ctx.artifacts["deploy_result"] = deploy_result
            ctx.artifacts["resource_details"] = deploy_result.get("provisioned_resources", [])
            ctx.artifacts["issues_resolved"] = len(heal_history)
            ctx.artifacts["current_tpl"] = current_tpl

            yield emit(
                "progress", "deploy_success",
                f"Deployment succeeded — {len(ctx.artifacts['resource_details'])} resource(s) provisioned",
                ctx.progress(att_base + 0.3),
                resources=ctx.artifacts["resource_details"],
            )
            return  # Step complete

        # Deploy failed — heal and retry
        deploy_error = deploy_result.get("error", "Unknown deployment error")

        if is_transient_error(str(deploy_error)):
            yield emit(
                "progress", "transient_wait",
                "Transient Azure error — retrying…",
                ctx.progress(att_base + 0.2),
            )
            await asyncio.sleep(10)
            continue

        if is_last:
            raise StepFailure(
                f"Deployment failed after {MAX_HEAL} attempts: {str(deploy_error)[:300]}",
                healable=False,
                phase="deploy",
            )

        yield emit(
            "healing", "deploy_fix",
            f"Deployment failed — healing (attempt {attempt}/{MAX_HEAL})…",
            ctx.progress(att_base + 0.2),
            error_brief=str(deploy_error)[:200],
        )

        try:
            fixed_json = await copilot_heal_template(
                content=json.dumps(current_tpl, indent=2),
                error=str(deploy_error),
                previous_attempts=heal_history,
                parameters=extract_param_values(current_tpl),
            )
            fix_summary = summarize_fix(json.dumps(current_tpl, indent=2), fixed_json)
            heal_history.append({
                "step": len(heal_history) + 1,
                "phase": "deploy",
                "error": str(deploy_error)[:500],
                "fix_summary": fix_summary,
            })
            current_tpl = json.loads(fixed_json)
            current_params = extract_param_values(current_tpl)
            current_params.update({k: v for k, v in user_params.items() if v is not None})
            yield emit(
                "healing_done", "deploy_fixed",
                f"Fix applied: {fix_summary}",
                ctx.progress(att_base + 0.25),
            )
        except Exception:
            heal_history.append({
                "step": len(heal_history) + 1,
                "phase": "deploy",
                "error": str(deploy_error)[:500],
                "fix_summary": "Heal failed",
            })

    # Should not reach here — last attempt raises above
    raise StepFailure(
        "All deployment attempts exhausted",
        healable=False,
        phase="deploy",
    )


# ══════════════════════════════════════════════════════════════
# STEP 8: INFRA TESTING
# ══════════════════════════════════════════════════════════════

@runner.step("infra_testing_template")
async def step_infra_testing(ctx: PipelineContext, step: StepDef):
    """Run AI-generated infrastructure smoke tests against live resources."""
    from src.pipelines.testing import stream_infra_testing

    resource_details = ctx.artifacts.get("resource_details", [])
    current_tpl = ctx.artifacts.get("current_tpl", {})
    if not current_tpl:
        try:
            current_tpl = json.loads(ctx.template) if isinstance(ctx.template, str) else ctx.template
        except Exception:
            current_tpl = {}

    if not resource_details:
        yield emit(
            "progress", "testing_skip",
            "No deployed resources found — skipping infrastructure tests",
            ctx.progress(1.0),
        )
        return

    yield emit(
        "progress", "testing_start",
        f"Running smoke tests against {len(resource_details)} deployed resource(s)…",
        ctx.progress(0.1),
    )

    test_passed = 0
    test_failed = 0
    test_skipped = 0

    async for raw_line in stream_infra_testing(
        arm_template=current_tpl,
        resource_group=ctx.rg_name,
        deployed_resources=resource_details,
        region=ctx.region,
        max_retries=2,
    ):
        try:
            evt = json.loads(raw_line.strip())
        except (json.JSONDecodeError, ValueError):
            continue

        phase = evt.get("phase", "")

        if phase == "test_result":
            if evt.get("passed"):
                test_passed += 1
            else:
                test_failed += 1
            yield emit(
                "progress", "test_result",
                evt.get("detail", f"Test: {'PASS' if evt.get('passed') else 'FAIL'}"),
                ctx.progress(0.3 + 0.5 * (test_passed + test_failed) / max(len(resource_details), 1)),
                passed=evt.get("passed"),
                test_name=evt.get("test_name", ""),
            )
        elif phase == "testing_complete":
            test_passed = evt.get("tests_passed", test_passed)
            test_failed = evt.get("tests_failed", test_failed)
        elif phase == "testing_generate":
            yield emit(
                "progress", "testing_generate",
                evt.get("detail", "Generating test scripts…"),
                ctx.progress(0.2),
            )

    ctx.artifacts["test_results"] = {
        "passed": test_passed,
        "failed": test_failed,
        "skipped": test_skipped,
    }

    total = test_passed + test_failed
    if test_failed > 0:
        yield emit(
            "progress", "testing_done",
            f"Infrastructure tests: {test_passed}/{total} passed, {test_failed} failed",
            ctx.progress(1.0),
            test_passed=test_passed,
            test_failed=test_failed,
        )
    else:
        yield emit(
            "progress", "testing_done",
            f"All {test_passed} infrastructure test(s) passed",
            ctx.progress(1.0),
            test_passed=test_passed,
        )


# ══════════════════════════════════════════════════════════════
# STEP 9: CLEANUP
# ══════════════════════════════════════════════════════════════

@runner.step("cleanup_template")
async def step_cleanup(ctx: PipelineContext, step: StepDef):
    """Delete the temporary validation resource group."""
    if not ctx.deployed_rg:
        yield emit(
            "progress", "cleanup_skip",
            "No temp RG to clean up",
            ctx.progress(1.0),
        )
        return

    yield emit(
        "progress", "cleanup_start",
        f"Cleaning up validation RG '{ctx.deployed_rg}'…",
        ctx.progress(0.3),
    )

    await cleanup_rg(ctx.deployed_rg)
    ctx.deployed_rg = None

    yield emit(
        "progress", "cleanup_done",
        "Validation resources cleaned up",
        ctx.progress(1.0),
    )


# ══════════════════════════════════════════════════════════════
# STEP 10: PROMOTE TEMPLATE
# ══════════════════════════════════════════════════════════════

@runner.step("promote_template")
async def step_promote(ctx: PipelineContext, step: StepDef):
    """Save the validated version, update status to 'validated'."""
    from src.database import (
        create_template_version,
        update_template_validation_status,
        complete_pipeline_run,
        get_backend,
        get_latest_semver,
        upsert_template,
    )
    from datetime import datetime, timezone

    template_id = ctx.template_id
    tmpl = ctx.extra.get("tmpl", {})
    issues_resolved = ctx.artifacts.get("issues_resolved", 0)
    test_results = ctx.artifacts.get("test_results", {})
    deploy_result = ctx.artifacts.get("deploy_result", {})

    # Save the validated template content
    if ctx.template and ctx.version_num:
        validation_summary = {
            "validation_passed": True,
            "deploy_status": "succeeded",
            "issues_resolved": issues_resolved,
            "heal_history": ctx.heal_history,
            "test_results": test_results,
            "resources_provisioned": len(ctx.artifacts.get("resource_details", [])),
        }
        await update_template_validation_status(
            template_id, ctx.version_num, "validated", validation_summary,
        )

    # Update catalog_templates status
    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()
    await backend.execute_write(
        "UPDATE catalog_templates SET status = ?, content = ?, updated_at = ? WHERE id = ?",
        ("validated", ctx.template, now, template_id),
    )

    # Complete pipeline run
    await complete_pipeline_run(
        ctx.run_id, "completed",
        version_num=ctx.version_num,
        semver=ctx.semver,
        summary={
            "template_name": ctx.extra.get("template_name", template_id),
            "issues_resolved": issues_resolved,
            "resources_provisioned": len(ctx.artifacts.get("resource_details", [])),
            "test_passed": test_results.get("passed", 0),
            "test_failed": test_results.get("failed", 0),
        },
        heal_count=issues_resolved,
    )

    yield emit(
        "done", "promote_complete",
        f"Template '{ctx.extra.get('template_name', template_id)}' validated and promoted "
        f"(v{ctx.semver or ctx.version_num}) — "
        f"{issues_resolved} issue(s) resolved, "
        f"{len(ctx.artifacts.get('resource_details', []))} resource(s) provisioned",
        ctx.progress(1.0),
        version=ctx.version_num,
        semver=ctx.semver,
        issues_resolved=issues_resolved,
        status="validated",
    )


# ══════════════════════════════════════════════════════════════
# FINALIZER — cleanup on abort/cancel
# ══════════════════════════════════════════════════════════════

@runner.finalizer
async def _cleanup_on_exit(ctx: PipelineContext):
    """Ensure temp RG is deleted even if the pipeline aborts."""
    if ctx.deployed_rg:
        try:
            await cleanup_rg(ctx.deployed_rg)
        except Exception as e:
            logger.debug(f"Finalizer cleanup failed (non-fatal): {e}")
