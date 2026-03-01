"""
Service Onboarding Pipeline — migrated from web.py's ``stream_onboarding()``.

Registers step handlers on a ``PipelineRunner`` that drives the end-to-end
service onboarding flow:

  1. initialize         — model routing, cleanup stale drafts
  2. analyze_standards  — fetch org standards
  3. plan_architecture  — LLM planning call
  4. generate_arm       — ARM template generation (skeleton or LLM)
  5. generate_policy    — Azure Policy generation
  6. governance_review  — CISO + CTO structured review gate
  7. validate_arm_deploy — HealingLoop with all checks
  8. infra_testing      — AI-generated infrastructure smoke tests
  9. deploy_policy      — deploy Azure Policy to Azure
 10. cleanup            — delete temp RG + policy
 11. promote_service    — mark approved, set active version

The endpoint still lives in web.py — it just delegates to
``runner.execute(ctx)`` now.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

from src.pipeline import (
    PipelineRunner,
    PipelineContext,
    HealingLoop,
    StepDef,
    StepFailure,
    PipelineAbort,
    emit,
)
from src.pipeline_helpers import (
    ensure_parameter_defaults,
    sanitize_placeholder_guids,
    sanitize_dns_zone_names,
    sanitize_template,
    inject_standard_tags,
    stamp_template_metadata,
    version_to_semver,
    extract_param_values,
    extract_meta,
    summarize_fix,
    friendly_error,
    brief_azure_error,
    get_resource_type_hints,
    test_policy_compliance,
    copilot_fix_two_phase,
    cleanup_rg,
    guard_locations,
    is_transient_error,
    build_final_params,
)
from src.model_router import Task, get_model_for_task, get_model_display, get_task_reason

logger = logging.getLogger("infraforge.pipeline.onboarding")

# ── The runner instance ──────────────────────────────────────
runner = PipelineRunner()


# ══════════════════════════════════════════════════════════════
# POLICY HEALING HELPER
# ══════════════════════════════════════════════════════════════

async def _heal_policy(
    policy: dict,
    resources: list[dict],
    violations: list[dict],
    standards_ctx: str,
    previous_attempts: list[dict],
) -> tuple[dict, str]:
    """Fix a generated Azure Policy so it doesn't reject successfully-deployed resources.

    The deployed resources are real and valid — the policy needs to be
    relaxed to match reality while still enforcing meaningful governance.

    Returns ``(fixed_policy_dict, strategy_text)``.
    """
    from src.copilot_helpers import copilot_send
    from src.web import ensure_copilot_client

    attempt_num = len(previous_attempts) + 1
    plan_model = get_model_for_task(Task.PLANNING)
    fix_model = get_model_for_task(Task.POLICY_GENERATION)

    violation_summary = "\n".join(
        f"  - {v['resource_name']} ({v['resource_type']}): {v['reason']}"
        for v in violations
    )
    resource_summary = json.dumps(
        [{"name": r.get("name"), "type": r.get("type"), "location": r.get("location"),
          "tags": r.get("tags", {})} for r in resources[:10]],
        indent=2, default=str,
    )[:4000]

    analysis_prompt = (
        f"An Azure Policy you generated is rejecting resources that DEPLOYED SUCCESSFULLY.\n"
        f"The deployment is valid — the policy is too strict.\n\n"
        f"--- CURRENT POLICY ---\n{json.dumps(policy, indent=2)[:4000]}\n--- END POLICY ---\n\n"
        f"--- VIOLATIONS (resources that failed the policy) ---\n{violation_summary}\n"
        f"--- END VIOLATIONS ---\n\n"
        f"--- ACTUAL DEPLOYED RESOURCES ---\n{resource_summary}\n--- END RESOURCES ---\n\n"
    )
    if standards_ctx:
        analysis_prompt += f"--- ORG STANDARDS TO ENFORCE ---\n{standards_ctx[:2000]}\n--- END STANDARDS ---\n\n"

    if previous_attempts:
        analysis_prompt += "--- PREVIOUS ATTEMPTS ---\n"
        for pa in previous_attempts:
            if pa.get("phase") == "policy_compliance":
                analysis_prompt += f"Attempt {pa.get('step', '?')}: {pa.get('strategy', 'unknown')[:300]} → STILL FAILED\n"
        analysis_prompt += "--- END PREVIOUS ATTEMPTS ---\n\n"

    analysis_prompt += (
        "ROOT CAUSE: Why does the policy reject these valid resources?\n"
        "STRATEGY: What specific conditions need to change?\n\n"
        "RULES:\n"
        "- The deployed resources are CORRECT — don't suggest changing the template\n"
        "- Relax policy conditions that don't apply to this resource type\n"
        "- Keep meaningful governance (tags, location restrictions)\n"
        "- Remove conditions that check for properties the resource type doesn't have\n"
    )

    _client = await ensure_copilot_client()
    if _client is None:
        raise RuntimeError("Copilot SDK not available")

    from src.agents import POLICY_FIXER
    strategy_text = await copilot_send(
        _client, model=plan_model,
        system_prompt=POLICY_FIXER.system_prompt + " Analyze why a policy rejects valid resources and propose specific fixes.",
        prompt=analysis_prompt, timeout=POLICY_FIXER.timeout,
        agent_name=POLICY_FIXER.name,
    )

    fix_prompt = (
        f"Fix this Azure Policy following the strategy below.\n\n"
        f"--- STRATEGY ---\n{strategy_text}\n--- END STRATEGY ---\n\n"
        f"--- CURRENT POLICY ---\n{json.dumps(policy, indent=2)}\n--- END POLICY ---\n\n"
        f"--- DEPLOYED RESOURCES (must pass after fix) ---\n{resource_summary}\n--- END RESOURCES ---\n\n"
        f"Return ONLY the corrected policy JSON — no markdown, no explanation. Start with {{\n"
        f"Keep the same structure: properties.policyRule with if/then.\n"
        f"The 'if' must describe VIOLATIONS (non-compliant state) — if it matches, deny applies.\n"
    )

    fixed_raw = await copilot_send(
        _client, model=fix_model,
        system_prompt=POLICY_FIXER.system_prompt + " Fix the policy JSON so it correctly evaluates the deployed resources.",
        prompt=fix_prompt, timeout=POLICY_FIXER.timeout,
        agent_name=POLICY_FIXER.name,
    )

    # Parse response
    cleaned = fixed_raw.strip()
    fence_match = re.search(r'```(?:json)?\s*\n(.*?)```', cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    if not cleaned.startswith('{'):
        brace_start = cleaned.find('{')
        if brace_start >= 0:
            depth = 0
            for i in range(brace_start, len(cleaned)):
                if cleaned[i] == '{': depth += 1
                elif cleaned[i] == '}':
                    depth -= 1
                    if depth == 0:
                        cleaned = cleaned[brace_start:i + 1]
                        break

    try:
        fixed_policy = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Policy healer returned invalid JSON — keeping original policy")
        return policy, strategy_text

    return fixed_policy, strategy_text


# ══════════════════════════════════════════════════════════════
# TEMPLATE REGENERATION HELPER
# ══════════════════════════════════════════════════════════════

async def _regenerate_template(
    ctx: PipelineContext,
    regen_count: int,
    standards_ctx: str,
    error: str,
    phase: str,
):
    """Re-plan architecture and regenerate ARM template from scratch.

    Called when the healing loop exhausts all attempts without fixing
    structural template issues.  Instead of failing, we go back to the
    PLAN phase with full error context and ask the LLM to take a
    fundamentally different approach.

    This is an async generator — yields NDJSON event lines for the UI.
    On return, ``ctx.template`` and ``ctx.heal_history`` are updated.
    """
    from src.tools.arm_generator import generate_arm_template_with_copilot
    from src.web import ensure_copilot_client
    from src.database import update_service_version_template

    # Clean up any deployed resources from the failed attempt
    if ctx.deployed_rg:
        await cleanup_rg(ctx.rg_name)
        ctx.deployed_rg = None

    total_attempts = len(ctx.heal_history)
    yield emit(
        "regen_start", "replanning",
        f"🔄 Healing exhausted after {total_attempts} attempt(s) — "
        f"re-planning architecture (regen cycle {regen_count})…",
        ctx.progress(0.50),
    )

    # Build error context from heal history
    error_lines = []
    for h in ctx.heal_history:
        error_lines.append(
            f"  Attempt {h['step']} [{h['phase']}]: "
            f"{h.get('error', '')[:200]}\n"
            f"    Strategy tried: {h.get('strategy', 'N/A')[:200]}"
        )
    error_context = "\n".join(error_lines) or "  (no healing attempts recorded)"

    planning_prompt = (
        f"You are RE-PLANNING an ARM template for the Azure resource type "
        f"'{ctx.service_id}'.\n\n"
        f"The PREVIOUS template failed validation after {total_attempts} "
        f"incremental healing attempts.  The patches could not fix the "
        f"underlying structural issues.\n\n"
        f"## Failure History\n{error_context}\n\n"
        f"## Final Error\n{error}\n\n"
        f"## Critical Instruction\n"
        f"Take a FUNDAMENTALLY DIFFERENT approach to the template design.\n"
        f"Do NOT repeat the same patterns that caused failures.  Consider:\n"
        f"- Using template parameters instead of variables where the previous "
        f"template used variables\n"
        f"- Simplifying resource dependency chains\n"
        f"- Different API versions if the previous ones had property issues\n"
        f"- Inline values instead of complex expressions\n"
        f"- Removing unnecessary nested or child resources\n\n"
        f"Produce a structured architecture plan with Resources, Security, "
        f"Parameters, Properties, Standards Compliance, and Validation Criteria."
    )

    if standards_ctx:
        planning_prompt += (
            f"\n\n--- ORGANIZATION STANDARDS (MANDATORY) ---\n"
            f"{standards_ctx}\n--- END STANDARDS ---\n"
        )

    new_plan = await _llm_reason(planning_prompt, task=Task.PLANNING)
    ctx.artifacts["planning_response"] = new_plan

    yield emit(
        "regen_planned", "replanning",
        f"✓ New architecture plan generated ({len(new_plan)} chars)",
        ctx.progress(0.55),
    )

    for line in new_plan.split("\n"):
        stripped = line.strip()
        if stripped:
            yield emit("llm_reasoning", "replanning", stripped, ctx.progress(0.55))

    # ── Generate new ARM template ──────────────────────────
    _gen_model_id = get_model_for_task(Task.CODE_GENERATION)
    _gen_model = get_model_display(Task.CODE_GENERATION)
    yield emit(
        "regen_generating", "regenerating",
        f"⚙️ {_gen_model} generating new ARM template from revised plan…",
        ctx.progress(0.60),
    )

    _client = await ensure_copilot_client()
    if _client is None:
        raise StepFailure(
            "Copilot SDK not available for regeneration",
            healable=False, phase="regen",
        )

    new_template = await generate_arm_template_with_copilot(
        ctx.service_id, ctx.extra["svc"]["name"], _client, _gen_model_id,
        standards_context=standards_ctx,
        planning_context=new_plan,
        region=ctx.region,
    )

    # Post-process the regenerated template
    new_template = sanitize_template(new_template)
    new_template = await inject_standard_tags(new_template, ctx.service_id)
    new_template = stamp_template_metadata(
        new_template, service_id=ctx.service_id,
        version_int=ctx.version_num,
        gen_source=f"copilot-regen-{regen_count}",
        region=ctx.region,
    )

    ctx.template = new_template
    ctx.gen_source = f"Copilot SDK (regen #{regen_count})"
    ctx.heal_history = []  # Reset for new generation

    await update_service_version_template(
        ctx.service_id, ctx.version_num, ctx.template,
        f"copilot-regen-{regen_count}",
    )

    tmpl_meta = extract_meta(ctx.template)
    yield emit(
        "regen_complete", "regenerated",
        f"✓ New template generated — {tmpl_meta['resource_count']} resource(s), "
        f"{tmpl_meta['size_kb']} KB — restarting validation",
        ctx.progress(0.65),
    )


# ══════════════════════════════════════════════════════════════
# STEP HANDLERS
# ══════════════════════════════════════════════════════════════

@runner.step("initialize")
async def step_initialize(ctx: PipelineContext, step: StepDef):
    """Phase 0: model routing table + cleanup stale drafts."""
    from src.database import delete_service_versions_by_status, create_pipeline_run

    # Record this pipeline run for history tracking
    await create_pipeline_run(
        ctx.run_id, ctx.service_id, "onboarding",
        created_by="copilot-sdk",
    )

    # Build per-task model routing summary
    routing = {
        "planning":        {"model": get_model_for_task(Task.PLANNING),           "display": get_model_display(Task.PLANNING),           "reason": get_task_reason(Task.PLANNING)},
        "code_generation": {"model": get_model_for_task(Task.CODE_GENERATION),    "display": get_model_display(Task.CODE_GENERATION),    "reason": get_task_reason(Task.CODE_GENERATION)},
        "code_fixing":     {"model": get_model_for_task(Task.CODE_FIXING),        "display": get_model_display(Task.CODE_FIXING),        "reason": get_task_reason(Task.CODE_FIXING)},
        "policy_gen":      {"model": get_model_for_task(Task.POLICY_GENERATION),  "display": get_model_display(Task.POLICY_GENERATION),  "reason": get_task_reason(Task.POLICY_GENERATION)},
        "analysis":        {"model": get_model_for_task(Task.VALIDATION_ANALYSIS),"display": get_model_display(Task.VALIDATION_ANALYSIS),"reason": get_task_reason(Task.VALIDATION_ANALYSIS)},
    }
    ctx.model_routing = routing

    yield emit(
        "progress", "init_model",
        "🤖 Model routing configured — each pipeline phase uses the optimal model for its task",
        ctx.progress(0.2), model_routing=routing,
    )

    for task_key, info in routing.items():
        yield emit(
            "llm_reasoning", "init_model",
            f"  {task_key}: {info['display']} — {info['reason'][:80]}",
            ctx.progress(0.3),
        )

    # Pipeline overview — tell the user what steps are coming
    svc = ctx.extra.get("svc") or {}
    yield emit(
        "progress", "pipeline_overview",
        f"Pipeline: Onboard {svc.get('name', ctx.service_id)} with full ARM template generation & validation",
        ctx.progress(0.4),
        steps=[
            "Analyze organization standards & governance policies",
            "AI plans cloud architecture for this service",
            "Generate production-ready ARM template",
            "Generate Azure Policy for compliance enforcement",
            "Run static governance policy checks",
            "ARM What-If preview (dry run)",
            "Deploy to isolated validation resource group",
            "Runtime compliance verification with Azure Policy",
            "Run infrastructure smoke tests against live resources",
            "Clean up validation resources",
            "Publish & promote approved version",
        ],
    )

    # Cleanup stale drafts/failed
    cleaned = await delete_service_versions_by_status(ctx.service_id, ["draft", "failed"])
    if cleaned:
        yield emit(
            "progress", "cleanup_drafts",
            f"🧹 Cleaned up {cleaned} stale draft/failed version(s) from previous runs",
            ctx.progress(0.5),
        )

    yield emit("progress", "init_complete", "✓ Initialization complete", ctx.progress(1.0))


@runner.step("analyze_standards")
async def step_analyze_standards(ctx: PipelineContext, step: StepDef):
    """Phase 1: fetch and emit organization standards."""
    from src.standards import get_standards_for_service, build_arm_generation_context, build_policy_generation_context

    # If use_version is set, load the existing draft instead of generating
    use_version = ctx.extra.get("use_version")
    if use_version is not None:
        from src.database import get_service_versions as _get_svc_versions, update_service_version_status

        all_vers = await _get_svc_versions(ctx.service_id)
        draft = next((v for v in all_vers if v.get("version") == use_version), None)
        if not draft:
            raise PipelineAbort(f"Version {use_version} not found for {ctx.service_id}")

        current_template = draft.get("arm_template", "")
        if not current_template:
            raise PipelineAbort(f"Version {use_version} has no ARM template content")

        ctx.template = current_template
        ctx.version_num = use_version
        ctx.semver = draft.get("semver") or f"{use_version}.0.0"
        ctx.gen_source = draft.get("created_by") or "draft"
        ctx.extra["skip_generation"] = True

        await update_service_version_status(ctx.service_id, use_version, "validating")
        ctx.template = await inject_standard_tags(ctx.template, ctx.service_id)
        ctx.update_template_meta()

        yield emit(
            "progress", "use_version",
            f"📋 Using existing draft v{ctx.semver} — skipping generation, proceeding to validation…",
            ctx.progress(0.3),
        )

    yield emit(
        "progress", "standards_analysis",
        f"Fetching organization standards applicable to {ctx.service_id}…",
        ctx.progress(0.1),
    )

    applicable_standards = await get_standards_for_service(ctx.service_id)
    ctx.artifacts["standards_ctx"] = await build_arm_generation_context(ctx.service_id)
    ctx.artifacts["policy_standards_ctx"] = await build_policy_generation_context(ctx.service_id)
    ctx.artifacts["applicable_standards"] = applicable_standards

    if applicable_standards:
        for std in applicable_standards:
            rule = std.get("rule", {})
            sev_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(std.get("severity", ""), "⚪")
            rule_summary = ""
            rt = rule.get("type", "")
            if rt == "property":
                rule_summary = f" → {rule.get('key', '?')} {rule.get('operator', '==')} {json.dumps(rule.get('value', True))}"
            elif rt == "tags":
                rule_summary = f" → require tags: {', '.join(rule.get('required_tags', []))}"
            elif rt == "allowed_values":
                rule_summary = f" → {rule.get('key', '?')} in [{', '.join(str(v) for v in rule.get('values', [])[:5])}]"
            elif rt == "cost_threshold":
                rule_summary = f" → max ${rule.get('max_monthly_usd', 0)}/month"

            yield emit(
                "standard_check", "standards_analysis",
                f"{sev_icon} [{std.get('severity', '?').upper()}] {std['name']}: {std['description']}{rule_summary}",
                ctx.progress(0.5),
                standard={"id": std["id"], "name": std["name"], "severity": std.get("severity"), "category": std.get("category")},
            )

        yield emit(
            "progress", "standards_complete",
            f"✓ {len(applicable_standards)} organization standard(s) apply — these will constrain ARM template generation and policy validation",
            ctx.progress(1.0),
        )
    else:
        yield emit(
            "progress", "standards_complete",
            "No organization standards match this service type — proceeding with default governance rules",
            ctx.progress(1.0),
        )


@runner.step("plan_architecture")
async def step_plan_architecture(ctx: PipelineContext, step: StepDef):
    """Phase 2: LLM planning call."""
    if ctx.extra.get("skip_generation"):
        yield emit("progress", "planning_skip", "Skipping planning — using existing version", ctx.progress(1.0))
        return

    svc = ctx.extra["svc"]
    standards_ctx = ctx.artifacts.get("standards_ctx", "")

    _planning_model = get_model_display(Task.PLANNING)
    yield emit(
        "progress", "planning",
        f"🧠 PLAN phase — {_planning_model} is reasoning about architecture for {ctx.service_id}…",
        ctx.progress(0.1),
    )

    planning_prompt = (
        f"You are planning an ARM template for the Azure resource type '{ctx.service_id}' "
        f"(service: {svc['name']}, category: {svc.get('category', 'general')}).\n\n"
    )

    if '/' in ctx.service_id.split('/')[-1] or ctx.service_id.count('/') >= 3:
        planning_prompt += (
            f"NOTE: '{ctx.service_id}' is a child resource type. The ARM template MUST "
            "include the parent resource(s) it depends on.\n\n"
        )

    if standards_ctx:
        planning_prompt += f"The organization has these mandatory standards:\n{standards_ctx}\n\n"

    planning_prompt += (
        "Produce a structured architecture plan. This plan will be handed to a "
        "separate code generation model, so be specific and concrete.\n\n"
        "## Required Output Sections:\n"
        "1. **Resources**: List every Azure resource to create (type, API version, purpose)\n"
        "2. **Security**: Specific security configs\n"
        "3. **Parameters**: Template parameters to expose\n"
        "4. **Properties**: Critical properties for production readiness\n"
        "5. **Standards Compliance**: How each org standard will be satisfied\n"
        "6. **Validation Criteria**: What should pass for correctness\n\n"
        "Be specific — include actual property names, API versions, and config values."
    )

    try:
        planning_response = await _llm_reason(planning_prompt, task=Task.PLANNING)
    except Exception as e:
        logger.warning(f"Planning phase failed (non-fatal): {e}")
        planning_response = ""

    ctx.artifacts["planning_response"] = planning_response

    for line in planning_response.split("\n"):
        line = line.strip()
        if line:
            yield emit("llm_reasoning", "planning", line, ctx.progress(0.5))

    if not planning_response:
        yield emit("progress", "planning_complete",
                    f"⚠️ Planning phase returned no response — proceeding without plan", ctx.progress(1.0))
    else:
        yield emit("progress", "planning_complete",
                    f"✓ Architecture plan complete ({len(planning_response)} chars)", ctx.progress(1.0))


@runner.step("generate_arm")
async def step_generate_arm(ctx: PipelineContext, step: StepDef):
    """Phase 3: ARM template generation (skeleton or LLM)."""
    if ctx.extra.get("skip_generation"):
        # Init event for existing version
        tmpl_meta = extract_meta(ctx.template)
        svc = ctx.extra["svc"]
        _sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "unknown")[:12] + "…"
        applicable_standards = ctx.artifacts.get("applicable_standards", [])

        yield emit(
            "init", "generated",
            f"✓ Draft ARM template v{ctx.semver} loaded — {tmpl_meta['resource_count']} resource(s), {tmpl_meta['size_kb']} KB",
            ctx.progress(1.0),
            version=ctx.version_num, semver=ctx.semver,
            meta=_build_meta_dict(svc, ctx, tmpl_meta, _sub_id, applicable_standards),
        )
        return

    from src.database import create_service_version, get_backend
    from src.tools.arm_generator import generate_arm_template, has_builtin_skeleton, generate_arm_template_with_copilot
    from src.web import ensure_copilot_client

    svc = ctx.extra["svc"]
    standards_ctx = ctx.artifacts.get("standards_ctx", "")
    planning_response = ctx.artifacts.get("planning_response", "")
    _sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "unknown")[:12] + "…"

    _gen_model = get_model_display(Task.CODE_GENERATION)
    _gen_model_id = get_model_for_task(Task.CODE_GENERATION)

    yield emit(
        "progress", "generating",
        f"⚙️ EXECUTE phase — {_gen_model} is generating ARM template guided by the architecture plan…",
        ctx.progress(0.1),
    )

    if has_builtin_skeleton(ctx.service_id):
        template_dict = generate_arm_template(ctx.service_id)
        ctx.template = json.dumps(template_dict, indent=2)
        ctx.gen_source = "built-in skeleton"
        yield emit("llm_reasoning", "generating",
                    f"📦 Using built-in ARM skeleton for {ctx.service_id}", ctx.progress(0.3))
    else:
        yield emit("llm_reasoning", "generating",
                    f"No built-in skeleton — {_gen_model} generating ARM template…", ctx.progress(0.2))
        try:
            _gen_client = await ensure_copilot_client()
            if _gen_client is None:
                raise RuntimeError("Copilot SDK not available for ARM generation")
            ctx.template = await generate_arm_template_with_copilot(
                ctx.service_id, svc["name"], _gen_client, _gen_model_id,
                standards_context=standards_ctx,
                planning_context=planning_response,
                region=ctx.region,
            )
        except Exception as gen_err:
            from src.database import fail_service_validation
            logger.error(f"ARM generation failed for {ctx.service_id}: {gen_err}", exc_info=True)
            await fail_service_validation(ctx.service_id, f"ARM generation failed: {gen_err}")
            raise PipelineAbort(f"ARM template generation failed: {str(gen_err)[:300]}")
        ctx.gen_source = f"Copilot SDK ({_gen_model})"

    # Validate we have JSON
    if not ctx.template or not ctx.template.strip():
        from src.database import fail_service_validation
        await fail_service_validation(ctx.service_id, "ARM template generation returned empty content")
        raise PipelineAbort("ARM template generation returned empty content")

    try:
        _parsed = json.loads(ctx.template)
    except json.JSONDecodeError as e:
        from src.database import fail_service_validation
        await fail_service_validation(ctx.service_id, f"Generated ARM template is not valid JSON: {e}")
        raise PipelineAbort(f"Generated ARM template is not valid JSON: {e}")

    # Validate the template contains the expected resource type
    _generated_types = [
        r.get("type", "").lower()
        for r in _parsed.get("resources", [])
        if isinstance(r, dict) and r.get("type")
    ]
    _expected = ctx.service_id.lower()
    _parent = "/".join(ctx.service_id.split("/")[:2]).lower() if ctx.service_id.count("/") >= 2 else None
    _type_ok = any(
        _expected in t or (_parent and _parent in t)
        for t in _generated_types
    )
    if not _type_ok and _generated_types:
        _msg = (
            f"ARM template contains wrong resource types: {_generated_types}. "
            f"Expected at least one resource of type '{ctx.service_id}'."
        )
        logger.error(f"Resource type mismatch for {ctx.service_id}: {_msg}")
        yield emit("llm_reasoning", "generating",
                    f"⚠️ {_msg}", ctx.progress(0.5))
        from src.database import fail_service_validation
        await fail_service_validation(ctx.service_id, _msg)
        raise PipelineAbort(_msg)

    # Sanitize + tag injection + metadata stamping
    ctx.template = sanitize_template(ctx.template)
    ctx.template = await inject_standard_tags(ctx.template, ctx.service_id)

    # Strip foreign resources — only keep the service's own resource type.
    # Dependencies are added by the composition layer and test wrapper.
    from src.tools.arm_generator import strip_foreign_resources
    ctx.template = strip_foreign_resources(ctx.template, ctx.service_id)

    # Peek next version number
    _db = await get_backend()
    _vrows = await _db.execute(
        "SELECT MAX(version) as max_ver FROM service_versions WHERE service_id = ?",
        (ctx.service_id,),
    )
    _next_ver = (_vrows[0]["max_ver"] if _vrows and _vrows[0]["max_ver"] else 0) + 1
    ctx.semver = version_to_semver(_next_ver)

    ctx.template = stamp_template_metadata(
        ctx.template, service_id=ctx.service_id,
        version_int=_next_ver, gen_source=ctx.gen_source, region=ctx.region,
    )

    ver = await create_service_version(
        service_id=ctx.service_id, arm_template=ctx.template,
        version=_next_ver, semver=ctx.semver, status="validating",
        changelog=f"Auto-generated via {ctx.gen_source}", created_by=ctx.gen_source,
    )
    ctx.version_num = ver["version"]
    ctx.update_template_meta()

    tmpl_meta = extract_meta(ctx.template)
    applicable_standards = ctx.artifacts.get("applicable_standards", [])

    yield emit(
        "init", "generated",
        f"✓ ARM template v{ctx.semver} generated via {ctx.gen_source} — "
        f"{tmpl_meta['resource_count']} resource(s), {tmpl_meta['size_kb']} KB",
        ctx.progress(1.0),
        version=ctx.version_num, semver=ctx.semver,
        meta=_build_meta_dict(svc, ctx, tmpl_meta, _sub_id, applicable_standards),
    )


@runner.step("generate_policy")
async def step_generate_policy(ctx: PipelineContext, step: StepDef):
    """Phase 3.5: Azure Policy generation."""
    svc = ctx.extra["svc"]
    policy_standards_ctx = ctx.artifacts.get("policy_standards_ctx", "")

    _policy_model = get_model_display(Task.POLICY_GENERATION)
    yield emit(
        "progress", "policy_generation",
        f"🛡️ Generating Azure Policy definition for {svc['name']} using {_policy_model}…",
        ctx.progress(0.1),
    )

    policy_gen_prompt = (
        f"Generate an Azure Policy definition JSON for '{svc['name']}' (type: {ctx.service_id}).\n\n"
    )
    if policy_standards_ctx:
        policy_gen_prompt += f"Organization standards to enforce:\n{policy_standards_ctx}\n\n"

    policy_gen_prompt += (
        "IMPORTANT — Azure Policy semantics for 'deny' effect:\n"
        "The 'if' condition must describe the VIOLATION (non-compliant state).\n"
        "If the 'if' MATCHES, the resource is DENIED.\n\n"
        "DO NOT generate policy conditions for subscription-gated features.\n\n"
        "Structure: top-level allOf with [type-check, anyOf-of-violations].\n"
        "Return ONLY raw JSON — NO markdown, NO explanation. Start with {"
    )

    try:
        policy_raw = await _llm_reason(
            policy_gen_prompt,
            "You are an Azure Policy expert. Return ONLY raw JSON — no markdown, no code fences.",
            task=Task.POLICY_GENERATION,
        )

        cleaned = policy_raw.strip()
        fence_match = re.search(r'```(?:json)?\s*\n(.*?)```', cleaned, re.DOTALL)
        if fence_match:
            cleaned = fence_match.group(1).strip()

        if not cleaned.startswith('{'):
            brace_start = cleaned.find('{')
            if brace_start >= 0:
                depth = 0
                for i in range(brace_start, len(cleaned)):
                    if cleaned[i] == '{': depth += 1
                    elif cleaned[i] == '}':
                        depth -= 1
                        if depth == 0:
                            cleaned = cleaned[brace_start:i + 1]
                            break

        ctx.generated_policy = json.loads(cleaned)
        _policy_size = round(len(cleaned) / 1024, 1)

        _rule = ctx.generated_policy.get("properties", ctx.generated_policy).get("policyRule", {})
        _effect = _rule.get("then", {}).get("effect", "unknown")
        _if_cond = _rule.get("if", {})
        _cond_count = len(_if_cond.get("allOf", _if_cond.get("anyOf", [None])))

        yield emit("llm_reasoning", "policy_generation",
                    f"📋 Policy generated: {_cond_count} condition(s), effect: {_effect}, size: {_policy_size} KB",
                    ctx.progress(0.7))
        yield emit("progress", "policy_generation_complete",
                    "✓ Azure Policy definition generated — will test after deployment", ctx.progress(1.0))

    except (json.JSONDecodeError, Exception) as e:
        logger.warning(f"Policy generation via LLM failed: {e} — using deterministic fallback")
        _violations = [
            {"field": f"tags['{tag}']", "exists": False}
            for tag in ["environment", "owner", "costCenter", "project"]
        ]
        _violations.append({"field": "location", "notIn": ["eastus2", "westus2", "westeurope"]})

        ctx.generated_policy = {
            "properties": {
                "displayName": f"Governance policy for {svc['name']}",
                "policyType": "Custom",
                "mode": "All",
                "policyRule": {
                    "if": {
                        "allOf": [
                            {"field": "type", "equals": ctx.service_id},
                            {"anyOf": _violations},
                        ]
                    },
                    "then": {"effect": "deny"},
                },
            }
        }
        _policy_size = round(len(json.dumps(ctx.generated_policy)) / 1024, 1)
        yield emit("llm_reasoning", "policy_generation",
                    f"📋 LLM failed — deterministic fallback: {len(_violations)} condition(s), effect: deny, size: {_policy_size} KB",
                    ctx.progress(0.7))
        yield emit("progress", "policy_generation_complete",
                    "✓ Fallback Azure Policy generated", ctx.progress(1.0))


@runner.step("governance_review")
async def step_governance_review(ctx: PipelineContext, step: StepDef):
    """Phase 5.5: CISO + CTO governance review gate.

    Runs both reviews in parallel. CISO can block; CTO is advisory.
    Results are persisted to the governance_reviews table.

    If CISO blocks, the template is healed using the CISO findings
    and the review is re-run (up to MAX_GOV_HEAL rounds) before
    giving up.
    """
    from src.governance import run_governance_review, format_review_summary
    from src.database import save_governance_review, update_service_version_template
    from src.web import ensure_copilot_client

    MAX_GOV_HEAL = step.config.get("max_governance_heals", 2)

    yield emit("progress", "governance_review",
               "🏛️ Running governance review — CISO (security) + CTO (architecture)…",
               ctx.progress(0.05))

    # ── Governance exception: skip if user requested exception ──
    if ctx.extra.get("governance_exception"):
        exception_by = ctx.extra.get("governance_exception_by", "user")
        yield emit("progress", "governance_skipped",
                    f"⚡ Governance review bypassed — exception granted by {exception_by}",
                    ctx.progress(1.0),
                    gate_decision="exception", gate_reason=f"Exception granted by {exception_by}")
        ctx.artifacts["governance_result"] = {
            "gate_decision": "exception",
            "gate_reason": f"Exception granted by {exception_by}",
            "reviewed_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        }
        return

    _client = await ensure_copilot_client()
    if _client is None:
        yield emit("progress", "governance_skipped",
                    "⚠️ Copilot SDK not available — skipping governance review",
                    ctx.progress(1.0))
        return

    # Get standards context if available
    standards_ctx = ctx.artifacts.get("standards_ctx", "")
    planning_response = ctx.artifacts.get("planning_response", "")
    version_str = ctx.semver or str(ctx.version_num or "")

    for _gov_attempt in range(1, MAX_GOV_HEAL + 2):  # +2 so last attempt is review-only
        try:
            result = await run_governance_review(
                _client,
                ctx.template,
                service_id=ctx.service_id,
                version=version_str,
                standards_ctx=standards_ctx,
            )

            # Emit individual reviews
            ciso = result["ciso"]
            cto = result["cto"]

            yield emit("progress", "ciso_review",
                        format_review_summary(ciso),
                        ctx.progress(0.4),
                        review=ciso)

            yield emit("progress", "cto_review",
                        format_review_summary(cto),
                        ctx.progress(0.6),
                        review=cto)

            # Persist both reviews
            _save_kwargs = dict(
                semver=ctx.semver,
                pipeline_type="onboarding",
                run_id=ctx.run_id,
                gate_decision=result["gate_decision"],
                gate_reason=result["gate_reason"],
                created_by="pipeline",
            )
            try:
                if ctx.version_num is not None:
                    await save_governance_review(ctx.service_id, ctx.version_num, ciso, **_save_kwargs)
                    await save_governance_review(ctx.service_id, ctx.version_num, cto, **_save_kwargs)
            except Exception as save_err:
                logger.warning("Failed to persist governance reviews: %s", save_err)

            # Gate decision
            gate = result["gate_decision"]
            gate_reason = result["gate_reason"]

            if gate == "blocked":
                ciso_findings = ciso.get("findings", [])
                critical_findings = [f for f in ciso_findings if f.get("severity") in ("critical", "high")]

                if _gov_attempt <= MAX_GOV_HEAL:
                    # ── Heal the template to address CISO findings ──
                    finding_descs = []
                    for f in ciso_findings:
                        sev = f.get("severity", "medium")
                        desc = f.get("description", f.get("finding", str(f)))
                        finding_descs.append(f"[{sev}] {desc}")
                    error_for_healer = (
                        f"CISO governance review BLOCKED this template. Findings:\n"
                        + "\n".join(finding_descs)
                        + f"\n\nCISO summary: {ciso.get('summary', '')}"
                    )

                    yield emit("healing", "fixing_template",
                                f"🛡️ CISO blocked — healing template to address {len(ciso_findings)} finding(s) "
                                f"(attempt {_gov_attempt}/{MAX_GOV_HEAL})…",
                                ctx.progress(0.7), step=_gov_attempt)

                    _pre_fix = ctx.template
                    ctx.template, _strategy = await copilot_fix_two_phase(
                        ctx.template, error_for_healer,
                        standards_ctx, planning_response, ctx.heal_history,
                    )
                    ctx.heal_history.append({
                        "step": len(ctx.heal_history) + 1,
                        "phase": "governance_review",
                        "error": error_for_healer[:500],
                        "fix_summary": summarize_fix(_pre_fix, ctx.template),
                        "strategy": _strategy,
                    })
                    await update_service_version_template(
                        ctx.service_id, ctx.version_num, ctx.template, "copilot-healed",
                    )
                    yield emit("healing_done", "template_fixed",
                                f"Fix applied: {_strategy[:200]} — re-running governance review…",
                                ctx.progress(0.75), step=_gov_attempt)
                    continue  # re-run governance review with healed template

                # Exhausted heal budget — hard block
                yield emit("progress", "governance_blocked",
                            f"🚫 Governance gate: BLOCKED — {gate_reason}",
                            ctx.progress(0.9),
                            gate_decision=gate, gate_reason=gate_reason,
                            findings=ciso_findings,
                            critical_findings=critical_findings,
                            ciso_summary=ciso.get("summary", ""),
                            service_id=ctx.service_id,
                            version=ctx.version_num,
                            semver=ctx.semver)
                yield emit("progress", "governance_complete",
                            f"🚫 Deployment blocked by CISO after {MAX_GOV_HEAL} heal attempt(s). "
                            f"Resolve critical findings before proceeding.",
                            ctx.progress(1.0),
                            gate_decision=gate, gate_reason=gate_reason,
                            ciso_verdict=ciso.get("verdict"), cto_verdict=cto.get("verdict"))
                raise StepFailure(
                    f"CISO blocked deployment: {gate_reason}",
                    healable=False, phase="governance_review",
                )

            elif gate == "conditional":
                yield emit("progress", "governance_complete",
                            f"⚠️ Governance gate: CONDITIONAL — {gate_reason}. Proceeding with noted concerns.",
                            ctx.progress(1.0),
                            gate_decision=gate, gate_reason=gate_reason,
                            ciso_verdict=ciso.get("verdict"), cto_verdict=cto.get("verdict"))
            else:
                yield emit("progress", "governance_complete",
                            f"✅ Governance gate: APPROVED — {gate_reason}",
                            ctx.progress(1.0),
                            gate_decision=gate, gate_reason=gate_reason,
                            ciso_verdict=ciso.get("verdict"), cto_verdict=cto.get("verdict"))

            # Store result for later steps
            ctx.artifacts["governance_result"] = result
            return  # ✅ governance passed (approved or conditional)

        except StepFailure:
            raise
        except Exception as exc:
            logger.error("Governance review failed: %s", exc, exc_info=True)
            yield emit("progress", "governance_complete",
                        f"⚠️ Governance review error: {exc} — proceeding without gate",
                        ctx.progress(1.0),
                        gate_decision="conditional", gate_reason=f"Review error: {exc}")
            return


@runner.step("validate_arm_deploy")
async def step_validate_arm_deploy(ctx: PipelineContext, step: StepDef):
    """Phase 4: Healing loop — static check, what-if, deploy, resource verify, policy test."""
    from src.database import (
        update_service_version_status,
        update_service_version_template,
        fail_service_validation,
        update_service_version_deployment_info,
    )
    from src.standards import get_all_standards
    from src.database import get_governance_policies_as_dict
    from src.tools.static_policy_validator import validate_template, validate_template_against_standards, build_remediation_prompt
    from src.tools.deploy_engine import run_what_if, execute_deployment, _get_resource_client

    MAX_HEAL = step.max_heal_attempts
    tmpl_meta = extract_meta(ctx.template)

    org_standards = await get_all_standards(enabled_only=True)
    gov_policies = await get_governance_policies_as_dict()
    use_standards_driven = len(org_standards) > 0

    standards_ctx = ctx.artifacts.get("standards_ctx", "")
    planning_response = ctx.artifacts.get("planning_response", "")

    MAX_REGEN = step.config.get("max_regen_cycles", 2)
    _regen_count = 0

    # ── Pre-loop structural validation ──
    # Catch missing variable/parameter references before starting the
    # expensive deploy loop.  This is especially important for templates
    # that were LLM-generated and may have stale references.
    from src.pipeline_helpers import validate_arm_references
    try:
        _pre_tmpl = json.loads(ctx.template) if isinstance(ctx.template, str) else ctx.template
        ref_errors = validate_arm_references(_pre_tmpl)
        if ref_errors:
            logger.warning(f"Pre-validation found {len(ref_errors)} reference errors — auto-fixing")
            tpl_str = json.dumps(_pre_tmpl)
            for err in ref_errors:
                if "Missing variable" in err:
                    vname = err.split("'")[1]
                    tpl_str = tpl_str.replace(
                        f"[variables('{vname}')]", f"[parameters('{vname}')]"
                    ).replace(
                        f"variables('{vname}')", f"parameters('{vname}')"
                    )
                    _pre_tmpl = json.loads(tpl_str)
                    _pre_tmpl.setdefault("parameters", {})[vname] = {
                        "type": "string",
                        "defaultValue": f"infraforge-{vname[:20]}",
                        "metadata": {"description": f"Auto-fixed: was undefined variable '{vname}'"},
                    }
                    tpl_str = json.dumps(_pre_tmpl)
                elif "Missing parameter" in err:
                    pname = err.split("'")[1]
                    _pre_tmpl.setdefault("parameters", {})[pname] = {
                        "type": "string",
                        "defaultValue": f"infraforge-{pname[:20]}",
                        "metadata": {"description": f"Auto-added: {pname}"},
                    }
                    tpl_str = json.dumps(_pre_tmpl)
            ctx.template = json.dumps(_pre_tmpl, indent=2)
            tmpl_meta = extract_meta(ctx.template)
            yield emit(
                "pre_validation_fix", "validation",
                f"Found {len(ref_errors)} structural issue(s) — auto-fixed before deployment",
                ctx.progress(0.01),
            )
    except Exception as pv_err:
        logger.warning(f"Pre-validation check error (non-fatal): {pv_err}")

    attempt = 0
    while attempt < MAX_HEAL:
        attempt += 1
        is_last = attempt == MAX_HEAL
        att_base = (attempt - 1) / MAX_HEAL

        if attempt == 1:
            step_desc = f"Validating ARM template v{ctx.semver} ({tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s))"
        else:
            step_desc = f"Verifying corrected template v{ctx.semver} — resolved {len(ctx.heal_history)} issue(s) so far"

        yield emit("iteration_start", "validation", step_desc, ctx.progress(att_base + 0.01), step=attempt)

        # ── Parse JSON ──
        try:
            template_json = json.loads(ctx.template)
        except json.JSONDecodeError as e:
            error_msg = f"ARM template is not valid JSON — line {e.lineno}, col {e.colno}: {e.msg}"
            if is_last:
                if _regen_count < MAX_REGEN - 1:
                    _regen_count += 1
                    async for line in _regenerate_template(ctx, _regen_count, standards_ctx, error_msg, "parsing"):
                        yield line
                    tmpl_meta = extract_meta(ctx.template)
                    attempt = 0
                    continue
                await update_service_version_status(ctx.service_id, ctx.version_num, "failed", validation_result={"error": error_msg})
                await fail_service_validation(ctx.service_id, error_msg)
                raise StepFailure(error_msg, healable=False, phase="parsing")
            yield emit("healing", "fixing_template", f"Template has a JSON syntax issue — auto-healing…", ctx.progress(att_base + 0.02), step=attempt)
            _pre_fix = ctx.template
            ctx.template, _strategy = await copilot_fix_two_phase(ctx.template, error_msg, standards_ctx, planning_response, ctx.heal_history)
            yield emit("llm_reasoning", "strategy", f"Strategy: {_strategy[:300]}", step=attempt)
            ctx.heal_history.append({"step": len(ctx.heal_history) + 1, "phase": "parsing", "error": error_msg, "fix_summary": summarize_fix(_pre_fix, ctx.template), "strategy": _strategy})
            tmpl_meta = extract_meta(ctx.template)
            await update_service_version_template(ctx.service_id, ctx.version_num, ctx.template, "copilot-healed")
            yield emit("healing_done", "template_fixed", f"Fix applied: {_strategy[:200]} — retrying…", ctx.progress(att_base + 0.03), step=attempt)
            continue

        # ── Static Policy Check ──
        yield emit("progress", "static_policy_check",
                    f"Running static policy validation against {len(org_standards) if use_standards_driven else len(gov_policies)} governance rules…",
                    ctx.progress(att_base + 0.04), step=attempt)

        if use_standards_driven:
            report = validate_template_against_standards(template_json, org_standards)
        else:
            report = validate_template(template_json, gov_policies)

        for check in report.results:
            icon = "✅" if check.passed else ("⚠️" if check.enforcement == "warn" else "❌")
            yield emit("policy_result", "static_policy_check",
                        f"{icon} [{check.rule_id}] {check.rule_name}: {check.message}",
                        ctx.progress(att_base + 0.05), passed=check.passed, severity=check.severity, step=attempt)

        if not report.passed:
            fail_msg = f"Static policy check: {report.passed_checks}/{report.total_checks} passed, {report.blockers} blocker(s)"
            yield emit("progress", "static_policy_failed", fail_msg, ctx.progress(att_base + 0.06), step=attempt)

            if is_last:
                if _regen_count < MAX_REGEN - 1:
                    _regen_count += 1
                    async for line in _regenerate_template(ctx, _regen_count, standards_ctx, fail_msg, "static_policy"):
                        yield line
                    tmpl_meta = extract_meta(ctx.template)
                    attempt = 0
                    continue
                await update_service_version_status(ctx.service_id, ctx.version_num, "failed", policy_check=report.to_dict())
                await fail_service_validation(ctx.service_id, fail_msg)
                raise StepFailure(fail_msg, healable=False, phase="static_policy")

            failed_checks = [c for c in report.results if not c.passed and c.enforcement == "block"]
            fix_prompt = build_remediation_prompt(ctx.template, failed_checks)
            yield emit("healing", "fixing_template",
                        f"{len(failed_checks)} policy violation(s) detected — auto-healing template…",
                        ctx.progress(att_base + 0.07), step=attempt)
            _pre_fix = ctx.template
            ctx.template, _strategy = await copilot_fix_two_phase(ctx.template, fix_prompt, standards_ctx, planning_response, ctx.heal_history)
            yield emit("llm_reasoning", "strategy", f"Strategy: {_strategy[:500]}", step=attempt)
            ctx.heal_history.append({"step": len(ctx.heal_history) + 1, "phase": "static_policy", "error": fix_prompt[:500], "fix_summary": summarize_fix(_pre_fix, ctx.template), "strategy": _strategy})
            tmpl_meta = extract_meta(ctx.template)
            await update_service_version_template(ctx.service_id, ctx.version_num, ctx.template, "copilot-healed")
            yield emit("healing_done", "template_fixed", f"Fix applied: {_strategy[:200]} — revalidating…", ctx.progress(att_base + 0.08), step=attempt)
            continue

        yield emit("progress", "static_policy_complete",
                    f"✓ Static policy check passed — {report.passed_checks}/{report.total_checks} checks",
                    ctx.progress(att_base + 0.08), step=attempt)
        await update_service_version_status(ctx.service_id, ctx.version_num, "validating", policy_check=report.to_dict())

        # ── What-If ──
        res_types_str = ", ".join(tmpl_meta["resource_types"][:5]) or "unknown"
        yield emit("progress", "what_if",
                    f"Submitting ARM What-If to Azure — previewing {tmpl_meta['resource_count']} resource(s) [{res_types_str}] in '{ctx.rg_name}' ({ctx.region})",
                    ctx.progress(att_base + 0.10), step=attempt)

        try:
            wif = await run_what_if(resource_group=ctx.rg_name, template=template_json,
                                    parameters=extract_param_values(template_json), region=ctx.region)
        except Exception as e:
            wif = {"status": "error", "errors": [str(e)]}

        if wif.get("status") != "success":
            errors = "; ".join(str(e) for e in wif.get("errors", [])) or "Unknown What-If error"
            brief = brief_azure_error(errors)

            if is_transient_error(errors):
                yield emit("progress", "infra_retry", "Azure is temporarily busy — retrying in 10 seconds…", ctx.progress(att_base + 0.11), step=attempt)
                await asyncio.sleep(10)
                continue

            if is_last:
                if _regen_count < MAX_REGEN - 1:
                    _regen_count += 1
                    async for line in _regenerate_template(ctx, _regen_count, standards_ctx, errors, "what_if"):
                        yield line
                    tmpl_meta = extract_meta(ctx.template)
                    attempt = 0
                    continue
                await update_service_version_status(ctx.service_id, ctx.version_num, "failed", validation_result={"error": errors, "phase": "what_if"})
                await fail_service_validation(ctx.service_id, f"What-If failed: {brief}")
                raise StepFailure(brief, healable=False, phase="what_if")

            yield emit("healing", "fixing_template",
                        f"{brief} — auto-healing template…",
                        ctx.progress(att_base + 0.12), step=attempt)
            _pre_fix = ctx.template
            ctx.template, _strategy = await copilot_fix_two_phase(ctx.template, errors, standards_ctx, planning_response, ctx.heal_history)
            yield emit("llm_reasoning", "strategy", f"Strategy: {_strategy[:500]}", step=attempt)
            ctx.heal_history.append({"step": len(ctx.heal_history) + 1, "phase": "what_if", "error": errors[:500], "fix_summary": summarize_fix(_pre_fix, ctx.template), "strategy": _strategy})
            tmpl_meta = extract_meta(ctx.template)
            await update_service_version_template(ctx.service_id, ctx.version_num, ctx.template, "copilot-healed")
            yield emit("healing_done", "template_fixed", f"Fix applied: {_strategy[:200]} — revalidating…", ctx.progress(att_base + 0.13), step=attempt)
            continue

        change_summary = ", ".join(f"{v} {k}" for k, v in wif.get("change_counts", {}).items())
        yield emit("progress", "what_if_complete",
                    f"✅ Azure accepted the template — {change_summary or 'no issues found'}",
                    ctx.progress(att_base + 0.14), step=attempt, result=wif)

        # ── Deploy ──
        yield emit("progress", "deploying",
                    f"Deploying {tmpl_meta['resource_count']} resource(s) into '{ctx.rg_name}' ({ctx.region})…",
                    ctx.progress(att_base + 0.16), step=attempt)

        # Run deployment with progress forwarding to keep the NDJSON
        # stream alive.  Without this, Azure Firewall deployments
        # (10-20 min) cause an HTTP timeout → "network error" on the
        # browser side.
        _deploy_q: asyncio.Queue = asyncio.Queue()

        async def _on_deploy_progress(evt: dict):
            await _deploy_q.put(evt)

        async def _do_deploy():
            try:
                return await execute_deployment(
                    resource_group=ctx.rg_name, template=template_json,
                    parameters=extract_param_values(template_json), region=ctx.region,
                    deployment_name=f"validate-{attempt}",
                    initiated_by="InfraForge Validator",
                    on_progress=_on_deploy_progress,
                )
            except Exception as exc:
                return {"status": "failed", "error": str(exc)}

        _deploy_task = asyncio.create_task(_do_deploy())

        # Forward deploy-engine progress as NDJSON heartbeats
        while not _deploy_task.done():
            try:
                evt = await asyncio.wait_for(_deploy_q.get(), timeout=20)
                detail = evt.get("detail", "Deployment in progress…")
                yield emit("progress", "deploy_progress", detail,
                           ctx.progress(att_base + 0.17), step=attempt)
            except asyncio.TimeoutError:
                # Heartbeat — keeps HTTP stream alive even when Azure
                # is silently provisioning resources
                yield emit("progress", "deploy_heartbeat",
                           "Deployment in progress — waiting for Azure…",
                           ctx.progress(att_base + 0.17), step=attempt)

        # Drain any remaining queued events
        while not _deploy_q.empty():
            try:
                evt = _deploy_q.get_nowait()
                detail = evt.get("detail", "")
                if detail:
                    yield emit("progress", "deploy_progress", detail,
                               ctx.progress(att_base + 0.18), step=attempt)
            except asyncio.QueueEmpty:
                break

        deploy_result = _deploy_task.result()
        deploy_status = deploy_result.get("status", "unknown")

        ctx.deployed_rg = ctx.rg_name

        if deploy_status != "succeeded":
            deploy_error = deploy_result.get("error", "Unknown deployment error")
            if "Please list deployment operations" in deploy_error or "At least one resource" in deploy_error:
                try:
                    from src.tools.deploy_engine import _get_deployment_operation_errors
                    _rc = _get_resource_client()
                    _lp = asyncio.get_event_loop()
                    op_errors = await _get_deployment_operation_errors(_rc, _lp, ctx.rg_name, f"validate-{attempt}")
                    if op_errors:
                        deploy_error = f"{deploy_error} | Operation errors: {op_errors}"
                except Exception:
                    pass

            brief = brief_azure_error(deploy_error)
            yield emit("progress", "deploy_failed", f"Deployment failed — {brief}", ctx.progress(att_base + 0.20), step=attempt)

            if is_transient_error(deploy_error):
                yield emit("progress", "infra_retry", "Azure is temporarily busy — retrying in 10 seconds…", ctx.progress(att_base + 0.21), step=attempt)
                await asyncio.sleep(10)
                continue

            if is_last:
                if _regen_count < MAX_REGEN - 1:
                    _regen_count += 1
                    async for line in _regenerate_template(ctx, _regen_count, standards_ctx, deploy_error, "deploy"):
                        yield line
                    tmpl_meta = extract_meta(ctx.template)
                    attempt = 0
                    continue
                await cleanup_rg(ctx.rg_name)
                await update_service_version_status(ctx.service_id, ctx.version_num, "failed", validation_result={"error": deploy_error, "phase": "deploy"})
                await fail_service_validation(ctx.service_id, f"Deploy failed: {brief}")
                raise StepFailure(brief, healable=False, phase="deploy")

            yield emit("healing", "fixing_template",
                        f"{brief} — auto-healing template…",
                        ctx.progress(att_base + 0.21), step=attempt)
            _pre_fix = ctx.template
            ctx.template, _strategy = await copilot_fix_two_phase(ctx.template, deploy_error, standards_ctx, planning_response, ctx.heal_history)
            yield emit("llm_reasoning", "strategy", f"Strategy: {_strategy[:500]}", step=attempt)
            ctx.heal_history.append({"step": len(ctx.heal_history) + 1, "phase": "deploy", "error": deploy_error[:500], "fix_summary": summarize_fix(_pre_fix, ctx.template), "strategy": _strategy})
            tmpl_meta = extract_meta(ctx.template)
            await update_service_version_template(ctx.service_id, ctx.version_num, ctx.template, "copilot-healed")
            yield emit("healing_done", "template_fixed", f"Fix applied: {_strategy[:200]} — redeploying…", ctx.progress(att_base + 0.22), step=attempt)
            continue

        # Deploy succeeded!
        provisioned = deploy_result.get("provisioned_resources", [])
        _deploy_name = f"validate-{attempt}"
        await update_service_version_deployment_info(
            ctx.service_id, ctx.version_num,
            run_id=ctx.run_id,
            resource_group=ctx.rg_name,
            deployment_name=_deploy_name,
            subscription_id=deploy_result.get("subscription_id", ""),
        )

        resource_summaries = [f"{r.get('type','?')}/{r.get('name','?')}" for r in provisioned]
        yield emit("progress", "deploy_complete",
                    f"✓ Deployment succeeded — {len(provisioned)} resource(s): {'; '.join(resource_summaries[:5])}",
                    ctx.progress(att_base + 0.22), step=attempt, resources=provisioned)

        # ── Resource verification ──
        yield emit("progress", "resource_check",
                    f"Querying Azure to verify {len(provisioned)} resource(s)…",
                    ctx.progress(att_base + 0.24), step=attempt)

        rc = _get_resource_client()
        loop = asyncio.get_event_loop()
        resource_details = []
        try:
            live_resources = await loop.run_in_executor(None, lambda: list(rc.resources.list_by_resource_group(ctx.rg_name)))
            for r in live_resources:
                detail = {"id": r.id, "name": r.name, "type": r.type, "location": r.location, "tags": dict(r.tags) if r.tags else {}}
                try:
                    full = await loop.run_in_executor(None, lambda r=r: rc.resources.get_by_id(r.id, api_version="2023-07-01"))
                    if full.properties:
                        detail["properties"] = full.properties
                except Exception:
                    pass
                resource_details.append(detail)

            yield emit("progress", "resource_check_complete",
                        f"✓ Verified {len(resource_details)} live resource(s)",
                        ctx.progress(att_base + 0.26), step=attempt,
                        resources=[{"name": r["name"], "type": r["type"], "location": r["location"]} for r in resource_details])
        except Exception as e:
            yield emit("progress", "resource_check_warning",
                        f"Could not enumerate resources (non-fatal): {e}",
                        ctx.progress(att_base + 0.26), step=attempt)

        # ── Runtime policy compliance ──
        # Uses its OWN retry loop (up to 3 rounds) so policy healing
        # doesn't consume the outer template-healing budget or cause
        # a full redeploy cycle.
        MAX_POLICY_HEAL = 3
        policy_results = []
        all_policy_compliant = True

        if ctx.generated_policy and resource_details:
            for _pol_attempt in range(1, MAX_POLICY_HEAL + 1):
                _policy_rule = ctx.generated_policy.get("properties", ctx.generated_policy).get("policyRule", {})
                _policy_effect = _policy_rule.get("then", {}).get("effect", "deny")
                yield emit("progress", "policy_testing",
                            f"🛡️ Evaluating {len(resource_details)} resource(s) against Azure Policy (effect: {_policy_effect})…",
                            ctx.progress(att_base + 0.27), step=attempt)

                policy_results = test_policy_compliance(ctx.generated_policy, resource_details)
                all_policy_compliant = all(r["compliant"] for r in policy_results)

                for pr in policy_results:
                    icon = "✅" if pr["compliant"] else "❌"
                    yield emit("policy_result", "policy_testing",
                                f"{icon} {pr['resource_type']}/{pr['resource_name']} — {pr['reason']}",
                                ctx.progress(att_base + 0.28), compliant=pr["compliant"], resource=pr, step=attempt)

                if all_policy_compliant:
                    yield emit("progress", "policy_testing_complete",
                                f"✓ All {len(policy_results)} resource(s) passed runtime policy compliance",
                                ctx.progress(att_base + 0.30), step=attempt)
                    break  # ✅ policy passed

                # ── Policy failed ──
                violations = [pr for pr in policy_results if not pr["compliant"]]
                violation_desc = "; ".join(f"{v['resource_name']}: {v['reason']}" for v in violations)
                compliant_count = sum(1 for r in policy_results if r["compliant"])
                fail_msg = f"{compliant_count}/{len(policy_results)} compliant — {len(violations)} violation(s)"

                yield emit("progress", "policy_failed", fail_msg, ctx.progress(att_base + 0.29), step=attempt)

                if _pol_attempt == MAX_POLICY_HEAL:
                    # Exhausted policy-heal budget — terminal state.
                    # The deployment SUCCEEDED — resources are live.  The
                    # violation is in the *generated policy*, which may be
                    # overly strict.
                    await cleanup_rg(ctx.rg_name)
                    ctx.deployed_rg = None

                    violation_details = [
                        {"resource": v["resource_name"], "type": v["resource_type"], "reason": v["reason"]}
                        for v in violations
                    ]
                    guidance = (
                        f"The ARM template deployed successfully, but {len(violations)} resource(s) "
                        f"did not pass the generated governance policy. This usually means the "
                        f"policy is stricter than what the resource type supports.\n\n"
                        f"Options:\n"
                        f"1. Submit a policy exception request for this service\n"
                        f"2. Ask the platform team to adjust the governance standards\n"
                        f"3. Retry onboarding — the policy will be regenerated"
                    )

                    await update_service_version_status(
                        ctx.service_id, ctx.version_num, "policy_blocked",
                        validation_result={"error": fail_msg, "phase": "policy_compliance",
                                           "violations": violation_details, "guidance": guidance},
                    )
                    await fail_service_validation(ctx.service_id, f"Policy review needed: {fail_msg}")

                    yield emit("policy_blocked", "policy_blocked", guidance,
                               ctx.progress(att_base + 0.30), step=attempt,
                               violations=violation_details,
                               compliant=compliant_count, total=len(policy_results))
                    raise StepFailure(
                        f"Deployment succeeded but {len(violations)} resource(s) need a policy exception. "
                        f"Submit a policy exception request or ask the platform team to adjust standards.",
                        healable=False, phase="policy_compliance",
                        event_type="policy_blocked",
                    )

                # ── Heal the POLICY, not the template ──
                # The template deployed successfully — don't break it.
                # Re-evaluate in this local loop without redeploying.
                yield emit("healing", "fixing_policy",
                            f"✏️ {len(violations)} resource(s) failed policy — adjusting governance policy (attempt {_pol_attempt}/{MAX_POLICY_HEAL})…",
                            ctx.progress(att_base + 0.30), step=attempt)

                fixed_policy, _strategy = await _heal_policy(
                    ctx.generated_policy, resource_details, violations,
                    standards_ctx, ctx.heal_history,
                )
                ctx.generated_policy = fixed_policy
                yield emit("llm_reasoning", "strategy", f"Policy fix: {_strategy[:500]}", step=attempt)
                ctx.heal_history.append({
                    "step": len(ctx.heal_history) + 1, "phase": "policy_compliance",
                    "error": violation_desc[:500],
                    "fix_summary": "Adjusted generated policy to match deployed resources",
                    "strategy": _strategy,
                })
                yield emit("healing_done", "policy_fixed",
                            f"Policy adjusted: {_strategy[:200]} — re-evaluating…",
                            ctx.progress(att_base + 0.31), step=attempt)
                # loop continues → re-evaluate with healed policy
        elif not ctx.generated_policy:
            yield emit("progress", "policy_skip", "No Azure Policy generated — skipping", ctx.progress(att_base + 0.30), step=attempt)
        else:
            yield emit("progress", "policy_skip", "No resources to test — skipping", ctx.progress(att_base + 0.30), step=attempt)

        # All checks passed — store results for later steps
        ctx.artifacts["report"] = report
        ctx.artifacts["wif"] = wif
        ctx.artifacts["deploy_result"] = deploy_result
        ctx.artifacts["resource_details"] = resource_details
        ctx.artifacts["policy_results"] = policy_results
        ctx.artifacts["all_policy_compliant"] = all_policy_compliant
        ctx.artifacts["deploy_name"] = _deploy_name
        return  # ✅ validation passed


@runner.step("infra_testing")
async def step_infra_testing(ctx: PipelineContext, step: StepDef):
    """Phase 4.6: run AI-generated infrastructure tests against live resources.

    Uses the Copilot SDK to generate Python tests tailored to the deployed
    resource types (e.g. SQL endpoint reachability, App Service HTTPS, etc.),
    executes them against the live validation environment, and reports results.
    """
    from src.pipelines.testing import stream_infra_testing

    resource_details = ctx.artifacts.get("resource_details", [])
    deploy_result = ctx.artifacts.get("deploy_result", {})

    if not resource_details:
        yield emit("progress", "testing_complete",
                    "No deployed resources to test — skipping infrastructure tests",
                    ctx.progress(0.5), status="skipped")
        return

    # Parse the current ARM template
    try:
        arm_template = json.loads(ctx.template)
    except json.JSONDecodeError:
        yield emit("progress", "testing_complete",
                    "⚠ Could not parse ARM template for test generation — skipping",
                    ctx.progress(0.5), status="skipped")
        return

    # Stream testing events — translate NDJSON from testing pipeline into
    # proper pipeline emit() events so the frontend renders flow cards.
    progress_base = 0.1
    async for raw_line in stream_infra_testing(
        arm_template=arm_template,
        resource_group=ctx.rg_name,
        deployed_resources=resource_details,
        region=ctx.region,
        max_retries=2,
    ):
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        phase = event.get("phase", "")
        detail = event.get("detail", "")
        status = event.get("status", "")

        # Map testing phases to progress fractions within this step
        phase_progress = {
            "testing_start": 0.1,
            "testing_generate": 0.3,
            "testing_execute": 0.5,
            "test_result": 0.6,
            "testing_analyze": 0.7,
            "testing_feedback": 0.8,
            "testing_complete": 1.0,
        }
        prog = ctx.progress(phase_progress.get(phase, progress_base))

        # Forward as a pipeline event with correct type field
        if phase == "test_result":
            yield emit("test_result", phase, detail, prog,
                        test_name=event.get("test_name", ""),
                        status=status,
                        message=event.get("message", ""))
        elif phase == "testing_complete":
            yield emit("progress", phase, detail, prog,
                        status=status,
                        tests_passed=event.get("tests_passed", 0),
                        tests_failed=event.get("tests_failed", 0),
                        tests_total=event.get("tests_total", 0))
            # Store test results in artifacts
            ctx.artifacts["infra_test_results"] = {
                "status": status,
                "passed": event.get("tests_passed", 0),
                "failed": event.get("tests_failed", 0),
                "total": event.get("tests_total", 0),
            }

            # Optionally block the pipeline on test failures.
            # Controlled by INFRA_TEST_BLOCKING env var (default: false).
            _test_blocking = os.environ.get("INFRA_TEST_BLOCKING", "").lower() in ("1", "true", "yes")
            _failed_count = event.get("tests_failed", 0)
            if _test_blocking and _failed_count > 0:
                raise StepFailure(
                    f"{_failed_count} infrastructure test(s) failed — "
                    f"set INFRA_TEST_BLOCKING=false to make tests advisory-only",
                    healable=False, phase="infra_testing",
                )
        elif phase == "testing_feedback":
            yield emit("progress", phase, detail, prog,
                        action=event.get("action", ""),
                        fix_guidance=event.get("fix_guidance", ""))
        else:
            yield emit("progress", phase, detail, prog, status=status)


@runner.step("deploy_policy")
async def step_deploy_policy(ctx: PipelineContext, step: StepDef):
    """Phase 4.7: deploy Azure Policy to Azure."""
    svc = ctx.extra["svc"]

    if not ctx.generated_policy:
        yield emit("progress", "policy_deploy_complete", "No Azure Policy generated — skipping deployment", 0.87)
        return

    yield emit("progress", "policy_deploy",
                f"🛡️ Deploying Azure Policy definition to enforce governance on {svc['name']}…", 0.85)

    try:
        from src.tools.policy_deployer import deploy_policy
        ctx.deployed_policy_info = await deploy_policy(
            service_id=ctx.service_id,
            run_id=ctx.run_id,
            policy_json=ctx.generated_policy,
            resource_group=ctx.rg_name,
        )
        yield emit("progress", "policy_deploy_complete",
                    f"✓ Azure Policy deployed — definition '{ctx.deployed_policy_info['definition_name']}' assigned to RG '{ctx.rg_name}'",
                    0.87)
    except Exception as pe:
        logger.warning(f"Azure Policy deployment failed (non-blocking): {pe}", exc_info=True)
        yield emit("progress", "policy_deploy_complete",
                    f"⚠ Azure Policy deployment failed (non-blocking): {str(pe)[:200]}", 0.87)


@runner.step("cleanup")
async def step_cleanup(ctx: PipelineContext, step: StepDef):
    """Phase 4.8: cleanup temp RG + policy."""
    yield emit("progress", "cleanup",
                f"All checks passed — deleting validation RG '{ctx.rg_name}'…", 0.90)

    if ctx.deployed_policy_info:
        try:
            from src.tools.policy_deployer import cleanup_policy
            await cleanup_policy(ctx.service_id, ctx.run_id, ctx.rg_name)
            logger.info(f"Cleaned up Azure Policy for run {ctx.run_id}")
        except Exception as cpe:
            logger.debug(f"Policy cleanup (non-fatal): {cpe}")

    await cleanup_rg(ctx.rg_name)
    ctx.deployed_rg = None

    yield emit("progress", "cleanup_complete",
                f"✓ Validation RG '{ctx.rg_name}' + Azure Policy cleaned up", 0.93)


@runner.step("promote_service")
async def step_promote_service(ctx: PipelineContext, step: StepDef):
    """Phase 4.9: mark service approved, set active version."""
    from src.database import update_service_version_status, set_active_service_version, complete_pipeline_run

    svc = ctx.extra["svc"]
    report = ctx.artifacts.get("report")
    wif = ctx.artifacts.get("wif", {})
    deploy_result = ctx.artifacts.get("deploy_result", {})
    resource_details = ctx.artifacts.get("resource_details", [])
    policy_results = ctx.artifacts.get("policy_results", [])
    all_policy_compliant = ctx.artifacts.get("all_policy_compliant", True)
    _deploy_name = ctx.artifacts.get("deploy_name", "validate-1")

    validation_summary = {
        "run_id": ctx.run_id,
        "resource_group": ctx.rg_name,
        "deployment_name": _deploy_name,
        "subscription_id": deploy_result.get("subscription_id", ""),
        "deployment_id": deploy_result.get("deployment_id", ""),
        "what_if": wif,
        "deploy_result": {
            "status": deploy_result.get("status"),
            "started_at": deploy_result.get("started_at"),
            "completed_at": deploy_result.get("completed_at"),
            "deployment_id": deploy_result.get("deployment_id", ""),
        },
        "deployed_resources": [{"name": r["name"], "type": r["type"], "location": r["location"]} for r in resource_details],
        "policy_check": report.to_dict() if report else {},
        "policy_compliance": policy_results,
        "all_policy_compliant": all_policy_compliant,
        "has_runtime_policy": ctx.generated_policy is not None,
        "policy_deployed_to_azure": ctx.deployed_policy_info is not None,
        "policy_deployment": ctx.deployed_policy_info,
        "infra_tests": ctx.artifacts.get("infra_test_results"),
        "attempts": len(ctx.heal_history) + 1,
        "heal_history": ctx.heal_history,
    }

    yield emit("progress", "promoting", f"Promoting {svc['name']} v{ctx.semver} → approved…", 0.97)

    await update_service_version_status(
        ctx.service_id, ctx.version_num, "approved",
        validation_result=validation_summary,
        policy_check=report.to_dict() if report else {},
    )
    await set_active_service_version(ctx.service_id, ctx.version_num)

    issues_resolved = len(ctx.heal_history)
    heal_msg = f" Resolved {issues_resolved} issue{'s' if issues_resolved != 1 else ''} automatically." if issues_resolved > 0 else ""

    _policy_str = ""
    if policy_results:
        _pc = sum(1 for r in policy_results if r["compliant"])
        _policy_str = f", {_pc}/{len(policy_results)} runtime policy check(s) passed"

    _azure_policy_str = ""
    if ctx.deployed_policy_info:
        _azure_policy_str = ", Azure Policy deployed + cleaned up"

    _test_str = ""
    infra_tests = ctx.artifacts.get("infra_test_results")
    if infra_tests and infra_tests.get("total", 0) > 0:
        _test_str = f", {infra_tests['passed']}/{infra_tests['total']} infra test(s) passed"

    yield emit(
        "done", "approved",
        f"🎉 {svc['name']} v{ctx.semver} approved! "
        f"{len(resource_details)} resource(s) validated, "
        f"{report.passed_checks}/{report.total_checks} static policy checks passed"
        f"{_policy_str}{_test_str}{_azure_policy_str}.{heal_msg}",
        1.0,
        issues_resolved=issues_resolved, version=ctx.version_num, semver=ctx.semver,
        summary=validation_summary, step=len(ctx.heal_history) + 1,
    )

    # Record pipeline completion
    await complete_pipeline_run(
        ctx.run_id, "completed",
        version_num=ctx.version_num, semver=ctx.semver,
        summary={
            "resources": len(resource_details),
            "policy_checks": f"{report.passed_checks}/{report.total_checks}" if report else "0/0",
            "infra_tests": infra_tests,
        },
        heal_count=issues_resolved,
    )


# ══════════════════════════════════════════════════════════════
# FINALIZER — cleanup on abort/cancel
# ══════════════════════════════════════════════════════════════

@runner.finalizer
async def finalizer_cleanup(ctx: PipelineContext):
    """Ensure temp RG and policy artifacts are cleaned up on any exit."""
    # Mark pipeline run as failed if it wasn't completed successfully
    try:
        from src.database import complete_pipeline_run
        from src.database import get_backend
        backend = await get_backend()
        rows = await backend.execute(
            "SELECT status FROM pipeline_runs WHERE run_id = ?", (ctx.run_id,)
        )
        if rows and rows[0].get("status") == "running":
            await complete_pipeline_run(
                ctx.run_id, "failed",
                error_detail="Pipeline did not complete — aborted or encountered an unrecoverable error",
                heal_count=len(ctx.heal_history),
            )
    except Exception:
        pass

    if ctx.deployed_policy_info:
        try:
            from src.tools.policy_deployer import cleanup_policy
            await cleanup_policy(ctx.service_id, ctx.run_id, ctx.deployed_rg or ctx.rg_name)
        except Exception:
            pass
    if ctx.deployed_rg:
        try:
            await cleanup_rg(ctx.deployed_rg)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════

async def _llm_reason(prompt: str, system_msg: str = "", task: Task = Task.PLANNING) -> str:
    """Universal LLM reasoning call with model routing."""
    from src.agents import LLM_REASONER
    from src.copilot_helpers import copilot_send
    from src.web import ensure_copilot_client

    task_model = get_model_for_task(task)
    _client = await ensure_copilot_client()
    if _client is None:
        raise RuntimeError("Copilot SDK not available")
    return await copilot_send(
        _client,
        model=task_model,
        system_prompt=system_msg or LLM_REASONER.system_prompt,
        prompt=prompt,
        timeout=90,
        agent_name="LLM_REASONER",
    )


def _build_meta_dict(svc: dict, ctx: PipelineContext, tmpl_meta: dict, sub_id: str, applicable_standards: list) -> dict:
    """Build the meta dict emitted in the 'init' event."""
    return {
        "service_name": svc.get("name", ctx.service_id),
        "service_id": ctx.service_id,
        "category": svc.get("category", ""),
        "region": ctx.region,
        "subscription": sub_id,
        "resource_group": ctx.rg_name,
        "template_size_kb": tmpl_meta["size_kb"],
        "resource_count": tmpl_meta["resource_count"],
        "resource_types": tmpl_meta["resource_types"],
        "resource_names": tmpl_meta.get("resource_names", []),
        "api_versions": tmpl_meta.get("api_versions", []),
        "schema": tmpl_meta["schema"],
        "parameters": tmpl_meta.get("parameters", []),
        "outputs": tmpl_meta.get("outputs", []),
        "version": ctx.version_num,
        "gen_source": ctx.gen_source,
        "model_routing": ctx.model_routing,
        "standards_count": len(applicable_standards),
    }
