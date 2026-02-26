"""
Service Onboarding Pipeline — migrated from web.py's ``stream_onboarding()``.

Registers step handlers on a ``PipelineRunner`` that drives the end-to-end
service onboarding flow:

  1. initialize         — model routing, cleanup stale drafts
  2. analyze_standards  — fetch org standards
  3. plan_architecture  — LLM planning call
  4. generate_arm       — ARM template generation (skeleton or LLM)
  5. generate_policy    — Azure Policy generation
  6. validate_arm_deploy — HealingLoop with all checks
  7. deploy_policy      — deploy Azure Policy to Azure
  8. cleanup            — delete temp RG + policy
  9. promote_service    — mark approved, set active version

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

    strategy_text = await copilot_send(
        _client, model=plan_model,
        system_prompt="You are an Azure Policy expert. Analyze why a policy rejects valid resources and propose specific fixes.",
        prompt=analysis_prompt, timeout=60,
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
        system_prompt="You are an Azure Policy expert. Fix the policy JSON so it correctly evaluates the deployed resources. Return ONLY raw JSON.",
        prompt=fix_prompt, timeout=60,
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
# STEP HANDLERS
# ══════════════════════════════════════════════════════════════

@runner.step("initialize")
async def step_initialize(ctx: PipelineContext, step: StepDef):
    """Phase 0: model routing table + cleanup stale drafts."""
    from src.database import delete_service_versions_by_status

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
        json.loads(ctx.template)
    except json.JSONDecodeError as e:
        from src.database import fail_service_validation
        await fail_service_validation(ctx.service_id, f"Generated ARM template is not valid JSON: {e}")
        raise PipelineAbort(f"Generated ARM template is not valid JSON: {e}")

    # Sanitize + tag injection + metadata stamping
    ctx.template = sanitize_template(ctx.template)
    ctx.template = await inject_standard_tags(ctx.template, ctx.service_id)

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

    for attempt in range(1, MAX_HEAL + 1):
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
        policy_results = []
        all_policy_compliant = True

        if ctx.generated_policy and resource_details:
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

            if not all_policy_compliant:
                violations = [pr for pr in policy_results if not pr["compliant"]]
                violation_desc = "; ".join(f"{v['resource_name']}: {v['reason']}" for v in violations)
                compliant_count = sum(1 for r in policy_results if r["compliant"])
                fail_msg = f"{compliant_count}/{len(policy_results)} compliant — {len(violations)} violation(s)"

                yield emit("progress", "policy_failed", fail_msg, ctx.progress(att_base + 0.29), step=attempt)

                if is_last:
                    # ── Policy-blocked terminal state ──
                    # The deployment SUCCEEDED — resources are live.  The
                    # violation is in the *generated policy*, which may be
                    # overly strict.  Instead of a hard "failed" we give the
                    # user actionable guidance.
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
                    # Don't demote the service — the template works, just
                    # policy needs adjustment.  Record the issue in notes.
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
                # The generated policy may be too strict, so we ask the
                # LLM to relax the policy to match the real resources.
                yield emit("healing", "fixing_policy",
                            f"{len(violations)} resource(s) failed policy — adjusting governance policy…",
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
                continue
            else:
                yield emit("progress", "policy_testing_complete",
                            f"✓ All {len(policy_results)} resource(s) passed runtime policy compliance",
                            ctx.progress(att_base + 0.30), step=attempt)
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
    from src.database import update_service_version_status, set_active_service_version

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

    yield emit(
        "done", "approved",
        f"🎉 {svc['name']} v{ctx.semver} approved! "
        f"{len(resource_details)} resource(s) validated, "
        f"{report.passed_checks}/{report.total_checks} static policy checks passed"
        f"{_policy_str}{_azure_policy_str}.{heal_msg}",
        1.0,
        issues_resolved=issues_resolved, version=ctx.version_num, semver=ctx.semver,
        summary=validation_summary, step=len(ctx.heal_history) + 1,
    )


# ══════════════════════════════════════════════════════════════
# FINALIZER — cleanup on abort/cancel
# ══════════════════════════════════════════════════════════════

@runner.finalizer
async def finalizer_cleanup(ctx: PipelineContext):
    """Ensure temp RG and policy artifacts are cleaned up on any exit."""
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
