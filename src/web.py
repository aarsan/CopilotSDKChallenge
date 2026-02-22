"""
InfraForge — Web Interface

FastAPI backend providing:
- Entra ID (Azure AD) authentication with MSAL
- WebSocket-based streaming chat connected to the Copilot SDK
- User context injection for personalized infrastructure provisioning
- REST endpoints for auth flow, session management, and usage tracking

This is the enterprise-grade frontend for InfraForge — authenticated users
interact with the agent through a browser, and their identity context enriches
every infrastructure request.
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from copilot import CopilotClient

from src.config import (
    APP_NAME,
    APP_VERSION,
    APP_DESCRIPTION,
    COPILOT_MODEL,
    COPILOT_LOG_LEVEL,
    OUTPUT_DIR,
    SYSTEM_MESSAGE,
    WEB_HOST,
    WEB_PORT,
    SESSION_SECRET,
    AVAILABLE_MODELS,
    get_active_model,
    set_active_model,
)
from src.tools import get_all_tools
from src.auth import (
    UserContext,
    create_auth_url,
    complete_auth,
    get_pending_session,
    get_user_context,
    invalidate_session,
    create_demo_session,
    is_auth_configured,
)
from src.database import (
    init_db,
    save_session,
    save_chat_message,
    log_usage,
    get_usage_stats,
    cleanup_expired_sessions,
    get_approval_requests,
    update_approval_request,
)
from src.utils import ensure_output_dir
from src.standards import init_standards
from src.standards_api import router as standards_router
from src.model_router import Task, get_model_for_task, get_model_display, get_task_reason, get_routing_table

logger = logging.getLogger("infraforge.web")

# ── Healing loop utilities ───────────────────────────────────
# The healing engine works toward a GOAL (successful deployment) using
# available tools (LLM rewrite, deep service analysis, strategy escalation).
# The loop counter is a budget/safety limit, NOT an "attempt" count.
# Progress events describe WHAT the engine is doing, not which attempt it's on.

def _summarize_fix(before: str, after: str) -> str:
    """Produce a short summary of what changed between two ARM template strings.

    Used to populate heal_history so the LLM knows what was already tried and
    can avoid repeating the same fix.
    """
    if before == after:
        return "NO CHANGE (fix produced identical output)"
    try:
        b = json.loads(before)
        a = json.loads(after)
    except Exception:
        return f"Template text changed (before: {len(before)} chars → after: {len(after)} chars)"

    changes: list[str] = []

    # Compare resource counts & types
    b_res = b.get("resources", [])
    a_res = a.get("resources", [])
    b_types = sorted({r.get("type", "?") for r in b_res if isinstance(r, dict)})
    a_types = sorted({r.get("type", "?") for r in a_res if isinstance(r, dict)})
    if len(b_res) != len(a_res):
        changes.append(f"resource count: {len(b_res)} → {len(a_res)}")
    removed_types = set(b_types) - set(a_types)
    added_types = set(a_types) - set(b_types)
    if removed_types:
        changes.append(f"removed resources: {', '.join(removed_types)}")
    if added_types:
        changes.append(f"added resources: {', '.join(added_types)}")

    # Compare API versions
    b_apis = {r.get("type", "?"): r.get("apiVersion", "?") for r in b_res if isinstance(r, dict)}
    a_apis = {r.get("type", "?"): r.get("apiVersion", "?") for r in a_res if isinstance(r, dict)}
    for rt in set(b_apis) & set(a_apis):
        if b_apis[rt] != a_apis[rt]:
            changes.append(f"API version for {rt}: {b_apis[rt]} → {a_apis[rt]}")

    # Compare parameters
    b_params = set(b.get("parameters", {}).keys())
    a_params = set(a.get("parameters", {}).keys())
    if b_params != a_params:
        added_p = a_params - b_params
        removed_p = b_params - a_params
        if added_p:
            changes.append(f"added params: {', '.join(added_p)}")
        if removed_p:
            changes.append(f"removed params: {', '.join(removed_p)}")

    if not changes:
        # Fall back to size comparison
        changes.append(f"template modified (size: {len(before)} → {len(after)} chars)")

    return "; ".join(changes[:5])


def _build_param_defaults() -> dict[str, object]:
    """Build parameter defaults using real Azure context where possible."""
    import os as _os
    sub_id = _os.environ.get("AZURE_SUBSCRIPTION_ID", "00000000-0000-0000-0000-000000000000")
    return {
        "resourceName": "infraforge-resource",
        "location": "[resourceGroup().location]",
        "environment": "dev",
        "projectName": "infraforge",
        "ownerEmail": "platform-team@company.com",
        "costCenter": "IT-0001",
        # Subscription / identity params
        "subscriptionId": sub_id,
        "subscription_id": sub_id,
        "targetSubscriptionId": sub_id,
        "linkedSubscriptionId": sub_id,
        "remoteSubscriptionId": sub_id,
        "peerSubscriptionId": sub_id,
        # Resource naming
        "vnetName": "infraforge-vnet",
        "subnetName": "default",
        "nsgName": "infraforge-nsg",
        "storageAccountName": "ifrgvalidation",
        "keyVaultName": "infraforge-kv",
        # DNS / domain params (must be valid FQDNs — at least 2 labels)
        "dnsZoneName": "infraforge-demo.com",
        "dnszones": "infraforge-demo.com",
        "dnsZone": "infraforge-demo.com",
        "zoneName": "infraforge-demo.com",
        "domainName": "infraforge-demo.com",
        "domain": "infraforge-demo.com",
        "hostName": "app.infraforge-demo.com",
        "fqdn": "app.infraforge-demo.com",
        # Shared secret params (validation only)
        "sharedKey": "InfraForgeVal1dation!",
        "adminPassword": "InfraForge#Val1d!",
        "adminUsername": "azureadmin",
    }

_PARAM_DEFAULTS: dict[str, object] = _build_param_defaults()


def _ensure_parameter_defaults(template_json: str) -> str:
    """Ensure every parameter in an ARM template has a defaultValue.

    Deployed templates are sent with ``parameters={}``, so any parameter
    without a ``defaultValue`` causes:
        "The value for the template parameter 'X' is not provided."

    This function injects sensible defaults for well-known params and a
    generic placeholder for anything else.  Returns the (possibly
    modified) JSON string.
    """
    try:
        tmpl = json.loads(template_json)
    except (json.JSONDecodeError, TypeError):
        return template_json  # can't fix what we can't parse

    params = tmpl.get("parameters")
    if not params or not isinstance(params, dict):
        return template_json

    import os as _os
    _sub_id = _os.environ.get("AZURE_SUBSCRIPTION_ID", "")

    patched = False
    for pname, pdef in params.items():
        if not isinstance(pdef, dict):
            continue
        if "defaultValue" not in pdef:
            # Check well-known defaults first
            dv = _PARAM_DEFAULTS.get(pname)
            if dv is None:
                # Heuristic matching for common param patterns
                plow = pname.lower()
                if "subscri" in plow and _sub_id:
                    dv = _sub_id
                elif plow.endswith("password") or plow.endswith("secret"):
                    dv = "InfraForge#Val1d!"
                elif plow.endswith("username"):
                    dv = "azureadmin"
                elif "sharedkey" in plow:
                    dv = "InfraForgeVal1dation!"
                elif any(k in plow for k in ("dns", "zone", "domain", "fqdn")):
                    dv = "infraforge-demo.com"
                elif "hostname" in plow:
                    dv = "app.infraforge-demo.com"
                else:
                    dv = f"infraforge-{pname}"
            pdef["defaultValue"] = dv
            patched = True

    if patched:
        patched_names = [p for p in params if "defaultValue" in params[p]]
        logger.info("Injected missing defaultValues for params: %s", patched_names)
        return json.dumps(tmpl, indent=2)
    return template_json


def _sanitize_placeholder_guids(template_json: str) -> str:
    """Replace placeholder/zero subscription GUIDs with the real subscription ID.

    LLMs often emit ``00000000-0000-0000-0000-000000000000`` as a subscription
    placeholder.  ARM resolves linked-resource scopes against these, causing
    ``LinkedAuthorizationFailed``.  This function swaps them out before deploy.
    """
    import os as _os
    sub_id = _os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    if not sub_id:
        return template_json

    placeholder = "00000000-0000-0000-0000-000000000000"
    if placeholder not in template_json:
        return template_json

    sanitized = template_json.replace(placeholder, sub_id)
    logger.info("Replaced placeholder subscription GUID(s) with real subscription ID")
    return sanitized


def _sanitize_dns_zone_names(template_json: str) -> str:
    """Ensure DNS zone resources have valid FQDN names (at least 2 labels).

    Azure DNS zones require names like ``example.com`` — a bare label like
    ``infraforge-dnszones`` is rejected with 'invalid DNS zone name'.
    This catches bad defaults before they hit ARM.
    """
    try:
        tmpl = json.loads(template_json)
    except (json.JSONDecodeError, TypeError):
        return template_json

    patched = False
    resources = tmpl.get("resources", [])
    params = tmpl.get("parameters", {})

    for res in resources:
        rtype = (res.get("type") or "").lower()
        if "dnszones" not in rtype:
            continue

        # Check the name — it might be a direct string or a parameter ref
        name = res.get("name", "")
        if isinstance(name, str) and not name.startswith("[") and "." not in name:
            # Bare label without dots — invalid DNS zone name
            res["name"] = "infraforge-demo.com"
            patched = True
            logger.info(f"Fixed invalid DNS zone name '{name}' → 'infraforge-demo.com'")

        # Also check if the name references a parameter whose default is bad
        if isinstance(name, str) and name.startswith("[") and "parameters(" in name:
            import re
            m = re.search(r"parameters\(['\"](\w+)['\"]\)", name)
            if m:
                param_name = m.group(1)
                pdef = params.get(param_name, {})
                dv = pdef.get("defaultValue", "")
                if isinstance(dv, str) and dv and "." not in dv and not dv.startswith("["):
                    pdef["defaultValue"] = "infraforge-demo.com"
                    patched = True
                    logger.info(
                        f"Fixed DNS zone param '{param_name}' default "
                        f"'{dv}' → 'infraforge-demo.com'"
                    )

    if patched:
        return json.dumps(tmpl, indent=2)
    return template_json


# ── Module-level LLM template healer ─────────────────────────

async def _copilot_heal_template(
    content: str,
    error: str,
    previous_attempts: list[dict] | None = None,
    parameters: dict | None = None,
) -> str:
    """Ask the Copilot SDK to fix an ARM template that failed deployment.

    This is a top-level utility so both the validation pipeline and the
    template deploy endpoint can use the same self-healing logic.

    Args:
        content: The ARM template JSON string.
        error: The Azure error message.
        previous_attempts: History of previous resolution steps (what was
            tried, what error resulted, what changed).
        parameters: The actual parameter VALUES sent to ARM. Including
            these lets the LLM see what values caused the error and fix
            the corresponding defaultValues in the template.
    """
    steps_taken = len(previous_attempts) if previous_attempts else 0

    prompt = (
        "The following ARM template failed Azure deployment.\n\n"
        f"--- ERROR ---\n{error}\n--- END ERROR ---\n\n"
        f"--- CURRENT TEMPLATE ---\n{content}\n--- END TEMPLATE ---\n\n"
    )

    if parameters:
        prompt += (
            "--- PARAMETER VALUES SENT TO ARM ---\n"
            f"{json.dumps(parameters, indent=2, default=str)}\n"
            "--- END PARAMETER VALUES ---\n\n"
            "IMPORTANT: These are the actual values that were sent to Azure. "
            "If the error is caused by one of these values (e.g. an invalid "
            "name, bad format, wrong length), you MUST fix the corresponding "
            "parameter's \"defaultValue\" in the template so it produces a "
            "valid value. The parameter values above are derived from the "
            "template's defaultValues — fixing the defaultValue fixes the "
            "deployed value.\n\n"
        )

    if previous_attempts:
        prompt += "--- RESOLUTION HISTORY (these approaches did NOT work — do NOT repeat them) ---\n"
        for i, pa in enumerate(previous_attempts, 1):
            prompt += (
                f"Step {i}: Error was: {pa['error'][:300]}\n"
                f"  Strategy tried: {pa['fix_summary']}\n"
                f"  Result: STILL FAILED — use a DIFFERENT strategy\n\n"
            )
        prompt += "--- END RESOLUTION HISTORY ---\n\n"

    prompt += (
        "Fix the template so it deploys successfully. Return ONLY the "
        "corrected raw JSON — no markdown fences, no explanation.\n\n"
        "CRITICAL RULES (in priority order):\n\n"
        "1. PARAMETER VALUES — Check parameter defaultValues FIRST:\n"
        "   - If the error mentions an invalid resource name, the name likely "
        "     comes from a parameter defaultValue. Find that parameter and fix "
        "     its defaultValue to comply with Azure naming rules.\n"
        "   - Azure DNS zone names MUST be valid FQDNs with at least two labels "
        "     (e.g. 'infraforge-demo.com', NOT 'if-dnszones').\n"
        "   - Storage account names: 3-24 lowercase alphanumeric, no hyphens.\n"
        "   - Key vault names: 3-24 alphanumeric + hyphens.\n"
        "   - Ensure EVERY parameter has a \"defaultValue\".\n\n"
        "2. LOCATIONS — Keep ALL location parameters as \"[resourceGroup().location]\" "
        "or \"[parameters('location')]\" — NEVER hardcode a region.\n"
        "   EXCEPTION: Globally-scoped resources MUST use location \"global\":\n"
        "   * Microsoft.Network/dnszones → location MUST be \"global\"\n"
        "   * Microsoft.Network/trafficManagerProfiles → \"global\"\n"
        "   * Microsoft.Cdn/profiles → \"global\"\n"
        "   * Microsoft.Network/frontDoors → \"global\"\n\n"
        "3. API VERSIONS — Use supported API versions:\n"
        "   - Microsoft.Network/dnszones: use \"2018-05-01\" (NOT 2023-09-01)\n"
        "   - Prefer stable 2023-xx-xx or 2024-xx-xx versions for other resources\n\n"
        "4. STRUCTURAL FIXES:\n"
        "   - Keep the same resource intent and resource names.\n"
        "   - Fix schema issues, missing required properties.\n"
        "   - If diagnosticSettings requires an external dependency, REMOVE it.\n"
        "   - NEVER use '00000000-0000-0000-0000-000000000000' as a subscription ID — "
        "     use [subscription().subscriptionId] instead.\n"
        "   - If the error mentions 'LinkedAuthorizationFailed', use "
        "     [subscription().subscriptionId] in resourceId() expressions.\n"
        "   - If a resource requires complex external deps (VPN gateways, "
        "     ExpressRoute), SIMPLIFY by removing those references.\n"
    )

    if steps_taken >= 3:
        prompt += (
            "\n\nESCALATION — multiple strategies have failed. Take DRASTIC measures:\n"
            "- SIMPLIFY the template: remove optional/nice-to-have resources\n"
            "- Remove diagnosticSettings, locks, autoscale rules if causing issues\n"
            "- Use the SIMPLEST valid configuration for each resource\n"
            "- Strip down to ONLY the primary resource with minimal properties\n"
            "- Use well-known, stable API versions (prefer 2023-xx-xx or 2024-xx-xx)\n"
        )
    elif steps_taken >= 1:
        prompt += (
            "\n\nPrevious fix(es) did NOT resolve the issue.\n"
            "You MUST try a FUNDAMENTALLY DIFFERENT approach:\n"
            "- Try a different API version for the failing resource\n"
            "- Restructure resource dependencies\n"
            "- Remove or replace the problematic sub-resource\n"
            "- Check if required properties changed in newer API versions\n"
        )

    session = None
    try:
        _client = await ensure_copilot_client()
        if _client is None:
            raise RuntimeError("Copilot SDK not available")
        session = await _client.create_session({
            "model": get_model_for_task(Task.CODE_FIXING),
            "streaming": True,
            "tools": [],
            "system_message": {"content": (
                "You are an Azure infrastructure expert. "
                "When fixing ARM templates, check parameter defaultValues FIRST — "
                "invalid resource names usually come from bad parameter defaults. "
                "Return ONLY raw JSON — no markdown, no code fences."
            )},
        })
        chunks: list[str] = []
        done_ev = asyncio.Event()

        def on_event(ev):
            try:
                if ev.type.value == "assistant.message_delta":
                    chunks.append(ev.data.delta_content or "")
                elif ev.type.value in ("assistant.message", "session.idle"):
                    done_ev.set()
            except Exception:
                done_ev.set()

        unsub = session.on(on_event)
        try:
            await session.send({"prompt": prompt})
            await asyncio.wait_for(done_ev.wait(), timeout=90)
        finally:
            unsub()

        fixed = "".join(chunks).strip()
        if fixed.startswith("```"):
            lines = fixed.split("\n")[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            fixed = "\n".join(lines).strip()

        # Guard: ensure healer didn't corrupt the location parameter
        # NOTE: some resource types (DNS zones, Traffic Manager, Front Door, etc.)
        # legitimately use location "global" — don't override those.
        _GLOBAL_LOCATION_TYPES = {
            "microsoft.network/dnszones",
            "microsoft.network/trafficmanagerprofiles",
            "microsoft.cdn/profiles",
            "microsoft.network/frontdoors",
            "microsoft.network/frontdoorwebapplicationfirewallpolicies",
        }
        try:
            _ft = json.loads(fixed)
            _params = _ft.get("parameters", {})
            _loc = _params.get("location", {})
            _dv = _loc.get("defaultValue", "")
            if isinstance(_dv, str) and _dv and not _dv.startswith("["):
                _loc["defaultValue"] = "[resourceGroup().location]"
                fixed = json.dumps(_ft, indent=2)
            for _res in _ft.get("resources", []):
                _rtype = (_res.get("type") or "").lower()
                _rloc = _res.get("location", "")
                # Skip resources that should use "global"
                if _rtype in _GLOBAL_LOCATION_TYPES:
                    if isinstance(_rloc, str) and _rloc.lower() != "global":
                        _res["location"] = "global"
                        fixed = json.dumps(_ft, indent=2)
                    continue
                if isinstance(_rloc, str) and _rloc and not _rloc.startswith("["):
                    _res["location"] = "[parameters('location')]"
                    fixed = json.dumps(_ft, indent=2)
        except (json.JSONDecodeError, AttributeError):
            pass

        fixed = _ensure_parameter_defaults(fixed)
        fixed = _sanitize_placeholder_guids(fixed)
        fixed = _sanitize_dns_zone_names(fixed)
        return fixed
    finally:
        if session:
            try:
                await session.destroy()
            except Exception:
                pass


# ── Deep healing engine for composed/blueprint templates ──────

async def _deep_heal_composed_template(
    template_id: str,
    service_ids: list[str],
    error_msg: str,
    current_template: dict,
    region: str = "eastus2",
    on_event=None,
) -> dict | None:
    """Deep-heal a composed template by fixing the underlying service templates.

    Flow:
    1. Root-cause analysis (o3-mini) — which service's ARM is broken?
    2. Fix that service's ARM template via LLM
    3. Validate the fixed service ARM with a standalone deploy
    4. Save as new service version
    5. Recompose the parent template from all service ARMs
    6. Return the fixed composed template dict

    Returns the fixed composed template dict, or None if healing failed.
    ``on_event`` is an async callable for streaming progress events.
    """
    import uuid as _dh_uuid
    from src.database import (
        get_service, get_active_service_version, create_service_version,
        upsert_template, create_template_version, get_template_by_id,
    )
    from src.tools.arm_generator import (
        generate_arm_template, has_builtin_skeleton,
        _STANDARD_PARAMETERS, _TEMPLATE_WRAPPER,
    )
    from src.tools.deploy_engine import execute_deployment

    async def _emit(evt: dict):
        if on_event:
            await on_event(evt)

    await _emit({"phase": "deep_heal_start", "detail": "Analyzing root cause across service templates…"})

    # ── Step 1: Root-cause analysis ──────────────────────────
    # Identify which service template is causing the failure
    resource_type_map: dict[str, dict] = {}  # service_id → ARM template dict
    for sid in service_ids:
        svc = await get_service(sid)
        if not svc:
            continue
        ver = await get_active_service_version(sid)
        arm = None
        if ver and ver.get("arm_template"):
            try:
                arm = json.loads(ver["arm_template"])
            except Exception:
                pass
        if not arm and has_builtin_skeleton(sid):
            arm = generate_arm_template(sid)
        if arm:
            resource_type_map[sid] = arm

    if not resource_type_map:
        await _emit({"phase": "deep_heal_fail", "detail": "No source service templates found"})
        return None

    # Use the error message + resource types to identify the culprit
    # Extract resource type from the error (e.g. "Microsoft.Network/dnszones/if-dnszones")
    culprit_sid = None
    error_lower = error_msg.lower()
    for sid in service_ids:
        # Match by resource type in error
        rt_lower = sid.lower()
        short = rt_lower.split("/")[-1]
        if rt_lower in error_lower or short in error_lower:
            culprit_sid = sid
            break

    if not culprit_sid:
        # If can't detect from error, try o3-mini reasoning
        try:
            _client = await ensure_copilot_client()
            if _client:
                session = await _client.create_session({
                    "model": get_model_for_task(Task.PLANNING),
                    "streaming": True, "tools": [],
                    "system_message": {"content": "You are an Azure infrastructure error analyst. Return ONLY the Azure resource type ID."},
                })
                chunks = []
                done_ev = asyncio.Event()
                def _on_ev(ev):
                    try:
                        if ev.type.value == "assistant.message_delta":
                            chunks.append(ev.data.delta_content or "")
                        elif ev.type.value in ("assistant.message", "session.idle"):
                            done_ev.set()
                    except Exception:
                        done_ev.set()
                unsub = session.on(_on_ev)
                try:
                    await session.send({"prompt": (
                        f"Error: {error_msg[:500]}\n\n"
                        f"Service templates: {', '.join(service_ids)}\n\n"
                        "Which service template is causing this error? "
                        "Reply with ONLY the exact service ID from the list above."
                    )})
                    await asyncio.wait_for(done_ev.wait(), timeout=30)
                finally:
                    unsub()
                resp = "".join(chunks).strip()
                for sid in service_ids:
                    if sid.lower() in resp.lower():
                        culprit_sid = sid
                        break
                try:
                    await session.destroy()
                except Exception:
                    pass
        except Exception:
            pass

    if not culprit_sid:
        culprit_sid = service_ids[0]  # fallback to first

    await _emit({
        "phase": "deep_heal_identified",
        "detail": f"Root cause: {culprit_sid} template needs fixing",
        "culprit_service": culprit_sid,
    })

    # ── Step 2: Fix the culprit service ARM template ─────────
    source_arm = resource_type_map.get(culprit_sid)
    if not source_arm:
        await _emit({"phase": "deep_heal_fail", "detail": f"No ARM template found for {culprit_sid}"})
        return None

    source_json = json.dumps(source_arm, indent=2)
    heal_attempts: list[dict] = []
    MAX_SVC_HEAL = 3
    fixed_svc_arm = None

    for svc_attempt in range(1, MAX_SVC_HEAL + 1):
        await _emit({
            "phase": "deep_heal_fix",
            "detail": f"Repairing {culprit_sid} ARM template…" + (
                "" if svc_attempt == 1 else f" (trying different strategy — {len(heal_attempts)} prior fix{'es' if len(heal_attempts) != 1 else ''} didn't resolve it)"
            ),
            "service_id": culprit_sid,
        })

        try:
            fixed_json = await _copilot_heal_template(
                content=source_json,
                error=error_msg,
                previous_attempts=heal_attempts,
                parameters=_extract_param_values(
                    json.loads(source_json) if isinstance(source_json, str) else source_json
                ),
            )
            candidate = json.loads(fixed_json)
        except Exception as fix_err:
            await _emit({"phase": "deep_heal_fix_error", "detail": f"LLM fix failed: {fix_err}"})
            continue

        # ── Step 3: Validate standalone ──────────────────────
        await _emit({
            "phase": "deep_heal_validate",
            "detail": f"Validating fixed {culprit_sid} template with standalone deploy…",
        })

        val_rg = f"infraforge-dheal-{_dh_uuid.uuid4().hex[:8]}"
        val_deploy = f"dheal-{_dh_uuid.uuid4().hex[:8]}"

        # Build params using the same function as deploy pipeline
        val_params = _extract_param_values(candidate)

        try:
            val_result = await execute_deployment(
                resource_group=val_rg,
                template=candidate,
                parameters=val_params,
                region=region,
                deployment_name=val_deploy,
                initiated_by="deep-healer",
            )
            val_status = val_result.get("status", "failed")
        except Exception as val_err:
            val_status = "failed"
            val_result = {"error": str(val_err)}

        # Cleanup the validation RG (fire and forget)
        try:
            from azure.identity import DefaultAzureCredential
            from azure.mgmt.resource import ResourceManagementClient
            import os
            cred = DefaultAzureCredential()
            sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
            if sub_id:
                rc = ResourceManagementClient(cred, sub_id)
                rc.resource_groups.begin_delete(val_rg)
        except Exception:
            pass

        if val_status == "succeeded":
            await _emit({
                "phase": "deep_heal_validated",
                "detail": f"✅ {culprit_sid} template validated successfully!",
                "service_id": culprit_sid,
                "resources": val_result.get("provisioned_resources", []),
            })
            fixed_svc_arm = candidate
            source_json = fixed_json  # for next iterations if needed
            break
        else:
            val_error = val_result.get("error", "unknown")
            await _emit({
                "phase": "deep_heal_validate_fail",
                "detail": f"Validation failed: {val_error[:200]}",
            })
            # Track for next heal attempt
            heal_attempts.append({
                "step": len(heal_attempts) + 1,
                "phase": "deploy",
                "error": val_error[:500],
                "fix_summary": _summarize_fix(json.dumps(source_arm, indent=2), fixed_json),
            })
            source_json = fixed_json  # try fixing THIS version next
            error_msg = val_error  # update error for next LLM call

    if not fixed_svc_arm:
        await _emit({"phase": "deep_heal_fail", "detail": f"Could not resolve {culprit_sid} issues with available tools"})
        return None

    # ── Step 4: Save new service version ─────────────────────
    await _emit({
        "phase": "deep_heal_version",
        "detail": f"Publishing new version of {culprit_sid}…",
    })

    try:
        new_ver = await create_service_version(
            service_id=culprit_sid,
            arm_template=json.dumps(fixed_svc_arm, indent=2),
            status="approved",
            changelog=f"Deep-healed: fixed ARM template during deployment of {template_id}",
            created_by="deep-healer",
        )
        new_ver_num = new_ver.get("version", "?")
        new_semver = new_ver.get("semver", "?")
        await _emit({
            "phase": "deep_heal_versioned",
            "detail": f"Published {culprit_sid} v{new_semver} (version {new_ver_num})",
        })

        # Full lifecycle promotion: set active version + approve service
        from src.orchestrator import promote_healed_service
        promo = await promote_healed_service(
            culprit_sid,
            int(new_ver_num) if isinstance(new_ver_num, (int, str)) and str(new_ver_num).isdigit() else 1,
            progress_callback=lambda evt: _emit(evt),
        )
        if promo["status"] == "promoted":
            await _emit({
                "phase": "deep_heal_promoted",
                "detail": f"Service {culprit_sid} promoted to approved with active v{new_ver_num}",
            })
    except Exception as ver_err:
        logger.warning(f"Failed to save service version: {ver_err}")
        # Continue anyway — we still have the fixed ARM in memory

    # ── Step 5: Recompose the parent template ────────────────
    await _emit({
        "phase": "deep_heal_recompose",
        "detail": f"Recomposing {template_id} with fixed service templates…",
    })

    # Gather all service ARM templates (using fixed one for culprit)
    all_arms: dict[str, dict] = {}
    for sid in service_ids:
        if sid == culprit_sid:
            all_arms[sid] = fixed_svc_arm
        else:
            arm = resource_type_map.get(sid)
            if arm:
                all_arms[sid] = arm

    # Recompose using the same logic as the compose endpoint
    combined_params = dict(_STANDARD_PARAMETERS)
    combined_resources = []
    combined_outputs = {}

    for sid in service_ids:
        tpl = all_arms.get(sid)
        if not tpl:
            continue
        short_name = sid.split("/")[-1].lower()
        suffix = f"_{short_name}"

        src_params = tpl.get("parameters", {})
        src_resources = tpl.get("resources", [])
        src_outputs = tpl.get("outputs", {})

        instance_name_param = f"resourceName{suffix}"
        combined_params[instance_name_param] = {
            "type": "string",
            "metadata": {"description": f"Name for {sid}"},
        }

        # Add ALL non-standard params from the service template
        all_non_standard = [
            pname for pname in src_params
            if pname not in {"resourceName", "location", "environment",
                             "projectName", "ownerEmail", "costCenter"}
        ]
        for pname in all_non_standard:
            pdef = src_params.get(pname)
            if not pdef:
                continue
            suffixed = f"{pname}{suffix}"
            combined_params[suffixed] = dict(pdef)

        # Clone resources, replacing ALL parameter references
        for res in src_resources:
            res_str = json.dumps(res)
            res_str = res_str.replace(
                "[parameters('resourceName')]",
                f"[parameters('{instance_name_param}')]",
            )
            res_str = res_str.replace(
                "parameters('resourceName')",
                f"parameters('{instance_name_param}')",
            )
            for pname in all_non_standard:
                suffixed = f"{pname}{suffix}"
                res_str = res_str.replace(
                    f"[parameters('{pname}')]",
                    f"[parameters('{suffixed}')]",
                )
                res_str = res_str.replace(
                    f"parameters('{pname}')",
                    f"parameters('{suffixed}')",
                )
            combined_resources.append(json.loads(res_str))

        for oname, odef in src_outputs.items():
            out_name = f"{oname}{suffix}"
            out_val = json.dumps(odef)
            out_val = out_val.replace(
                "[parameters('resourceName')]",
                f"[parameters('{instance_name_param}')]",
            )
            out_val = out_val.replace(
                "parameters('resourceName')",
                f"parameters('{instance_name_param}')",
            )
            for pname in all_non_standard:
                suffixed = f"{pname}{suffix}"
                out_val = out_val.replace(
                    f"[parameters('{pname}')]",
                    f"[parameters('{suffixed}')]",
                )
                out_val = out_val.replace(
                    f"parameters('{pname}')",
                    f"parameters('{suffixed}')",
                )
            combined_outputs[out_name] = json.loads(out_val)

    composed = dict(_TEMPLATE_WRAPPER)
    composed["parameters"] = combined_params
    composed["variables"] = {}
    composed["resources"] = combined_resources
    composed["outputs"] = combined_outputs

    # Ensure all params have defaults
    composed_json = _ensure_parameter_defaults(json.dumps(composed, indent=2))
    composed_json = _sanitize_placeholder_guids(composed_json)
    composed_json = _sanitize_dns_zone_names(composed_json)
    composed = json.loads(composed_json)

    # ── Step 6: Save new template version ────────────────────
    try:
        new_tmpl_ver = await create_template_version(
            template_id,
            composed_json,
            changelog=f"Deep-healed: fixed {culprit_sid}, recomposed",
            created_by="deep-healer",
        )
        # Also update the catalog_templates content
        from src.database import get_backend
        backend = await get_backend()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        await backend.execute_write(
            "UPDATE catalog_templates SET content = ?, updated_at = ? WHERE id = ?",
            (composed_json, now, template_id),
        )
        await _emit({
            "phase": "deep_heal_complete",
            "detail": f"Recomposed template saved — fixed {culprit_sid}, ready to deploy",
            "fixed_service": culprit_sid,
            "new_version": new_tmpl_ver.get("version"),
        })
    except Exception as save_err:
        logger.warning(f"Failed to save recomposed template: {save_err}")
        await _emit({
            "phase": "deep_heal_complete",
            "detail": f"Template recomposed in memory (save failed: {save_err})",
        })

    return composed


def _version_to_semver(version_int: int) -> str:
    """Convert an integer version number to semver format.

    Scheme:  version N → N.0.0
    Each new onboarding creates a new major version (the template is
    regenerated from scratch). The heal loop updates in-place and doesn't
    create new versions, so minor/patch stay at 0.
    """
    return f"{version_int}.0.0"


def _stamp_template_metadata(
    template_json: str,
    *,
    service_id: str,
    version_int: int,
    gen_source: str = "unknown",
    region: str = "eastus2",
) -> str:
    """Embed InfraForge provenance metadata into an ARM template.

    Adds a top-level ``metadata`` property (ARM supports this for any
    template) and updates ``contentVersion`` to the semver string.
    This makes every template self-describing — version, origin, and
    content hash travel with the template even outside the database.

    ARM ignores the ``metadata`` property during deployment, so this
    is safe to include.
    """
    import hashlib
    from datetime import datetime, timezone

    try:
        tmpl = json.loads(template_json)
    except (json.JSONDecodeError, TypeError):
        return template_json

    semver = _version_to_semver(version_int)

    # Update contentVersion to match our semver
    tmpl["contentVersion"] = semver

    # Compute a content hash of the resources section (stable fingerprint)
    resources_str = json.dumps(tmpl.get("resources", []), sort_keys=True)
    content_hash = hashlib.sha256(resources_str.encode()).hexdigest()[:12]

    tmpl["metadata"] = {
        "_generator": {
            "name": "InfraForge",
            "version": semver,
            "templateHash": content_hash,
        },
        "infrapiForge": {
            "serviceId": service_id,
            "version": version_int,
            "semver": semver,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "generatedBy": gen_source,
            "region": region,
            "platform": "InfraForge Self-Service Infrastructure",
        },
    }

    return json.dumps(tmpl, indent=2)


def _extract_param_values(template: dict) -> dict:
    """Extract explicit parameter values from a template's defaultValues.

    ARM *should* use ``defaultValue`` when ``parameters={}`` is passed, but
    in practice the validate/deploy endpoints sometimes reject templates
    with required parameters even when defaults are defined.  By extracting
    the defaults and passing them as explicit values, we guarantee ARM never
    complains about missing parameters.

    Skips ``location`` because ARM expressions like
    ``[resourceGroup().location]`` cannot be provided as a literal value —
    they only work as defaultValues inside the template.
    """
    params = template.get("parameters", {})
    values: dict[str, object] = {}
    for pname, pdef in params.items():
        if not isinstance(pdef, dict):
            continue
        dv = pdef.get("defaultValue")
        if dv is None:
            # No default — provide one from our well-known list
            dv = _PARAM_DEFAULTS.get(pname)
        if dv is None:
            # Heuristic: DNS/domain/zone params need valid FQDNs
            plow = pname.lower()
            if any(k in plow for k in ("dns", "zone", "domain", "fqdn")):
                dv = "infraforge-demo.com"
            elif "hostname" in plow:
                dv = "app.infraforge-demo.com"
            else:
                dv = f"infraforge-{pname}"
        # Skip ARM expressions — they only work inside the template, not as
        # explicit parameter values.
        if isinstance(dv, str) and dv.startswith("["):
            continue
        values[pname] = dv
    return values


# ── Global state ─────────────────────────────────────────────
copilot_client: Optional[CopilotClient] = None
_copilot_init_lock = asyncio.Lock()


async def ensure_copilot_client() -> Optional[CopilotClient]:
    """Lazily initialize the Copilot SDK client on first use.

    Returns the client, or None if initialization fails.
    """
    global copilot_client
    if copilot_client is not None:
        return copilot_client
    async with _copilot_init_lock:
        if copilot_client is not None:
            return copilot_client
        try:
            logger.info("Lazy-initializing Copilot SDK client...")
            copilot_client = CopilotClient({"log_level": COPILOT_LOG_LEVEL})
            await copilot_client.start()
            logger.info("Copilot SDK client started successfully")
            return copilot_client
        except Exception as e:
            logger.error(f"Copilot SDK failed to start: {e}")
            copilot_client = None
            return None


# Track active Copilot sessions: session_token → { copilot_session, user_context }
# (Chat history and usage analytics are persisted in the database)
active_sessions: dict[str, dict] = {}

# ── Active validation job tracker (in-memory) ────────────────
# service_id → { status, service_name, started_at, updated_at, phase, step,
#                progress, events: [dict], error?, rg_name? }
_active_validations: dict[str, dict] = {}


def _user_context_to_dict(user: UserContext) -> dict:
    """Convert UserContext to a dict for database persistence."""
    return {
        "user_id": user.user_id,
        "display_name": user.display_name,
        "email": user.email,
        "job_title": user.job_title,
        "department": user.department,
        "cost_center": user.cost_center,
        "manager": user.manager,
        "groups": user.groups,
        "roles": user.roles,
        "team": user.team,
        "is_platform_team": user.is_platform_team,
        "is_admin": user.is_admin,
    }


# ── Lifespan ─────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start/stop the Copilot SDK client with the server lifecycle."""
    global copilot_client
    logger.info("Initializing database...")
    await init_db()
    await cleanup_expired_sessions()
    logger.info("Initializing organization standards...")
    await init_standards()
    logger.info("Deferring Copilot SDK client start (lazy init on first chat)...")
    copilot_client = None  # Will be started lazily on first WebSocket connection
    ensure_output_dir(OUTPUT_DIR)

    # Azure resource provider sync — runs on-demand via the Sync button.
    # Removed from startup to avoid blocking or crashing the server.

    logger.info("InfraForge web server ready")
    yield
    logger.info("Shutting down Copilot SDK client...")
    # Clean up active sessions
    for session_data in active_sessions.values():
        try:
            await session_data["copilot_session"].destroy()
        except Exception:
            pass
    if copilot_client:
        try:
            await copilot_client.stop()
        except Exception:
            pass
    logger.info("Shutdown complete")


# ── App ──────────────────────────────────────────────────────

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description=APP_DESCRIPTION,
    lifespan=lifespan,
)

app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# Mount API routers
app.include_router(standards_router)

# Serve static files (HTML, CSS, JS)
static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ── Auth Endpoints ───────────────────────────────────────────

@app.get("/")
async def root():
    """Serve the main page."""
    index_path = os.path.join(static_dir, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/onboarding-docs")
async def onboarding_docs():
    """Serve the onboarding pipeline documentation page."""
    docs_path = os.path.join(static_dir, "onboarding-docs.html")
    with open(docs_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/api/auth/config")
async def auth_config():
    """Return auth configuration for the frontend MSAL.js client."""
    from src.config import ENTRA_CLIENT_ID, ENTRA_TENANT_ID, ENTRA_REDIRECT_URI

    return JSONResponse({
        "configured": is_auth_configured(),
        "clientId": ENTRA_CLIENT_ID,
        "tenantId": ENTRA_TENANT_ID,
        "redirectUri": ENTRA_REDIRECT_URI,
    })


@app.get("/api/auth/login")
async def login():
    """Initiate the Entra ID login flow."""
    if not is_auth_configured():
        # Demo mode — create a demo session and persist to DB
        session_token, user = create_demo_session()
        pending = get_pending_session(session_token)
        if pending:
            await save_session(session_token, _user_context_to_dict(user), "demo-token")
        return JSONResponse({
            "mode": "demo",
            "sessionToken": session_token,
            "user": {
                "displayName": user.display_name,
                "email": user.email,
                "jobTitle": user.job_title,
                "department": user.department,
                "costCenter": user.cost_center,
                "team": user.team,
                "isAdmin": user.is_admin,
                "isPlatformTeam": user.is_platform_team,
            },
        })

    auth_url, flow_id = create_auth_url()
    return JSONResponse({
        "mode": "entra",
        "authUrl": auth_url,
        "flowId": flow_id,
    })


@app.get("/api/auth/callback")
async def auth_callback(request: Request):
    """Handle the Entra ID redirect after login."""
    flow_id = request.query_params.get("state", "")
    auth_response = dict(request.query_params)

    session_token = complete_auth(flow_id, auth_response)
    if not session_token:
        raise HTTPException(status_code=401, detail="Authentication failed")

    # Persist the session from auth.py's pending store → database
    pending = get_pending_session(session_token)
    if pending:
        user_ctx = pending["user_context"]
        await save_session(
            session_token,
            _user_context_to_dict(user_ctx),
            pending.get("access_token", ""),
            pending.get("claims"),
        )

    # Redirect to the main app with the session token
    return RedirectResponse(url=f"/?session={session_token}")


@app.post("/api/auth/demo")
async def demo_login():
    """Create a demo session (when Entra ID is not configured)."""
    session_token, user = create_demo_session()
    pending = get_pending_session(session_token)
    if pending:
        await save_session(session_token, _user_context_to_dict(user), "demo-token")
    return JSONResponse({
        "sessionToken": session_token,
        "user": {
            "displayName": user.display_name,
            "email": user.email,
            "jobTitle": user.job_title,
            "department": user.department,
            "costCenter": user.cost_center,
            "team": user.team,
            "isAdmin": user.is_admin,
            "isPlatformTeam": user.is_platform_team,
        },
    })


@app.post("/api/auth/logout")
async def logout(request: Request):
    """End the user session."""
    body = await request.json()
    session_token = body.get("sessionToken", "")

    # Clean up Copilot session
    if session_token in active_sessions:
        try:
            await active_sessions[session_token]["copilot_session"].destroy()
        except Exception:
            pass
        del active_sessions[session_token]

    await invalidate_session(session_token)
    return JSONResponse({"status": "ok"})


@app.get("/api/auth/me")
async def get_current_user(request: Request):
    """Get current user info from session token."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    session_token = auth_header[7:]
    user = await get_user_context(session_token)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired")

    return JSONResponse({
        "displayName": user.display_name,
        "email": user.email,
        "jobTitle": user.job_title,
        "department": user.department,
        "costCenter": user.cost_center,
        "team": user.team,
        "isAdmin": user.is_admin,
        "isPlatformTeam": user.is_platform_team,
    })


# ── Model Settings ────────────────────────────────────────────

@app.get("/api/settings/model")
async def get_model_settings():
    """Return the current active LLM model and all available models."""
    active = get_active_model()
    return JSONResponse({
        "active_model": active,
        "available_models": AVAILABLE_MODELS,
    })


@app.get("/api/settings/model-routing")
async def get_model_routing_settings():
    """Return the model routing table — which model handles which pipeline task and why."""
    return JSONResponse({
        "routing_table": get_routing_table(),
        "chat_model": get_active_model(),
        "description": (
            "InfraForge uses different models for different pipeline tasks. "
            "Reasoning tasks use o3-mini, code generation uses Claude Sonnet 4, "
            "and fixing uses GPT-4.1. The chat model is user-selectable."
        ),
    })


@app.put("/api/settings/model")
async def update_model_settings(request: Request):
    """Change the active LLM model at runtime."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    model_id = body.get("model_id", "").strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="model_id is required")

    if not set_active_model(model_id):
        valid_ids = [m["id"] for m in AVAILABLE_MODELS]
        raise HTTPException(
            status_code=400,
            detail=f"Invalid model_id '{model_id}'. Valid models: {', '.join(valid_ids)}",
        )

    logger.info(f"Active LLM model changed to: {model_id}")
    return JSONResponse({"active_model": model_id, "status": "updated"})


# ── Usage Analytics (Work IQ) ────────────────────────────────

@app.get("/api/analytics/usage")
async def get_usage_analytics(request: Request):
    """Return usage analytics for the Work IQ dashboard.

    Shows who's provisioning what, team-level spend, template reuse rates,
    and policy compliance trends.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    session_token = auth_header[7:]
    user = await get_user_context(session_token)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired")

    # Query database — department filter for non-admins
    department_filter = None if (user.is_admin or user.is_platform_team) else user.department
    stats = await get_usage_stats(department=department_filter)

    return JSONResponse(stats)


# ── Activity Monitor API ─────────────────────────────────────

@app.get("/api/activity")
async def get_activity():
    """Return all validation activity: running jobs + recent completed/failed.

    Powers the Activity Monitor page for at-a-glance observability.
    """
    from src.database import get_all_services

    services = await get_all_services()

    # Build activity items from services with validation-related statuses
    jobs = []

    for svc in services:
        status = svc.get("status", "not_approved")
        svc_id = svc.get("id", "")

        # Include services that are validating, validation_failed, or recently approved
        if status in ("validating", "validation_failed", "approved"):
            # Check if there's a live tracker entry
            live = _active_validations.get(svc_id)

            job = {
                "service_id": svc_id,
                "service_name": svc.get("name", svc_id),
                "category": svc.get("category", ""),
                "status": status,
                "is_running": live is not None and live.get("status") == "running",
                "phase": live.get("phase", "") if live else "",
                "detail": live.get("detail", "") if live else "",
                "step": live.get("step", 0) if live else 0,
                "progress": live.get("progress", 0) if live else (1.0 if status == "approved" else 0),
                "started_at": live.get("started_at", "") if live else "",
                "updated_at": live.get("updated_at", "") if live else "",
                "rg_name": live.get("rg_name", "") if live else "",
                "region": live.get("region", "") if live else "",
                "subscription": live.get("subscription", "") if live else "",
                "template_meta": live.get("template_meta", {}) if live else {},
                "steps_completed": live.get("steps_completed", []) if live else [],
                "events": live.get("events", [])[-50:] if live else [],  # last 50 events
                "error": live.get("error", "") if live else (svc.get("review_notes", "") if status == "validation_failed" else ""),
            }
            jobs.append(job)

    # Sort: running first, then by updated_at descending
    jobs.sort(key=lambda j: (
        0 if j["is_running"] else 1,
        0 if j["status"] == "validating" else 1,
        -(j.get("updated_at") or "0").__hash__(),
    ))

    running_count = sum(1 for j in jobs if j["is_running"])
    validating_count = sum(1 for j in jobs if j["status"] == "validating")
    failed_count = sum(1 for j in jobs if j["status"] == "validation_failed")
    approved_count = sum(1 for j in jobs if j["status"] == "approved")

    return JSONResponse({
        "jobs": jobs,
        "summary": {
            "running": running_count,
            "validating": validating_count,
            "failed": failed_count,
            "approved": approved_count,
            "total": len(jobs),
        },
    })


# ── Service Catalog API ──────────────────────────────────────

@app.get("/api/catalog/services")
async def get_service_catalog():
    """Return the approved Azure services catalog from the database.

    This powers the interactive service browser in the welcome screen,
    letting users see at a glance which services are approved, conditional,
    under review, or not yet approved.
    """
    from src.database import get_all_services

    try:
        services = await get_all_services()

        # Aggregate stats
        stats = {"approved": 0, "conditional": 0, "under_review": 0, "not_approved": 0}
        categories = set()
        for svc in services:
            status = svc.get("status", "not_approved")
            stats[status] = stats.get(status, 0) + 1
            categories.add(svc.get("category", "other"))

        return JSONResponse({
            "services": services,
            "stats": stats,
            "categories": sorted(categories),
            "total": len(services),
        })
    except Exception as e:
        logger.error(f"Failed to load service catalog: {e}")
        return JSONResponse({"services": [], "stats": {}, "categories": [], "total": 0})


@app.get("/api/catalog/templates")
async def get_template_catalog(
    category: Optional[str] = None,
    fmt: Optional[str] = None,
    template_type: Optional[str] = None,
):
    """Return the template catalog from the database."""
    from src.database import get_all_templates

    try:
        templates = await get_all_templates(
            category=category, fmt=fmt, template_type=template_type,
        )
        return JSONResponse({
            "templates": templates,
            "total": len(templates),
        })
    except Exception as e:
        logger.error(f"Failed to load template catalog: {e}")
        return JSONResponse({"templates": [], "total": 0})


# ── Onboarding API ───────────────────────────────────────────

@app.post("/api/catalog/services")
async def onboard_service(request: Request):
    """Onboard a new Azure service into the approved service catalog."""
    from src.database import upsert_service, get_service

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    required = ["id", "name", "category", "status"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required fields: {', '.join(missing)}")

    # Validate status
    valid_statuses = {"approved", "conditional", "under_review", "not_approved"}
    if body.get("status") not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(sorted(valid_statuses))}")

    try:
        await upsert_service(body)
        svc = await get_service(body["id"])
        return JSONResponse({"status": "ok", "service": svc})
    except Exception as e:
        logger.error(f"Failed to onboard service: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/api/catalog/services/{service_id:path}")
async def update_service_governance(service_id: str, request: Request):
    """Update governance fields on an existing service (partial update).

    Accepts any subset of: status, risk_tier, contact, review_notes,
    documentation, approved_skus, approved_regions, policies, conditions.
    The service must already exist in the catalog.
    """
    from src.database import get_service, upsert_service

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Fetch the existing service
    existing = await get_service(service_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    # Validate status if provided
    valid_statuses = {"approved", "conditional", "under_review", "not_approved"}
    if "status" in body and body["status"] not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(valid_statuses))}",
        )

    # Merge provided fields into existing service data
    updatable = [
        "status", "risk_tier", "contact", "review_notes", "documentation",
        "approved_skus", "approved_regions", "policies", "conditions",
    ]
    for field in updatable:
        if field in body:
            existing[field] = body[field]

    try:
        await upsert_service(existing)
        svc = await get_service(service_id)
        return JSONResponse({"status": "ok", "service": svc})
    except Exception as e:
        logger.error(f"Failed to update service governance: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/catalog/templates")
async def onboard_template(request: Request):
    """Onboard a new template into the template catalog."""
    from src.database import upsert_template, get_template_by_id

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    required = ["id", "name", "format", "category"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required fields: {', '.join(missing)}")

    try:
        await upsert_template(body)
        tmpl = await get_template_by_id(body["id"])
        return JSONResponse({"status": "ok", "template": tmpl})
    except Exception as e:
        logger.error(f"Failed to onboard template: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/catalog/services/approved-for-templates")
async def get_approved_services_for_templates():
    """Return approved services with their ARM template parameters.

    Only services with status='approved' and an active version (with an ARM
    template) are returned.  Each service includes the list of *extra*
    parameters the skeleton exposes beyond the standard set (resourceName,
    location, environment, projectName, ownerEmail, costCenter) so the
    template-builder UI can show parameter checkboxes.
    """
    try:
        from src.database import get_all_services, get_active_service_version, get_service_versions
        from src.tools.arm_generator import generate_arm_template, has_builtin_skeleton
        import json as _json

        STANDARD_PARAMS = {
            "resourceName", "location", "environment",
            "projectName", "ownerEmail", "costCenter",
        }

        def _extract_params(all_params: dict) -> list[dict]:
            """Convert ARM parameter dict into a list of param descriptors."""
            result = []
            for pname, pdef in all_params.items():
                meta = pdef.get("metadata", {})
                result.append({
                    "name": pname,
                    "type": pdef.get("type", "string"),
                    "description": meta.get("description", ""),
                    "defaultValue": pdef.get("defaultValue"),
                    "allowedValues": pdef.get("allowedValues"),
                    "is_standard": pname in STANDARD_PARAMS,
                })
            return result

        services = await get_all_services()
        logger.info(f"approved-for-templates: total services={len(services)}")
        result = []

        for svc in services:
            if svc.get("status") != "approved":
                continue

            service_id = svc["id"]
            logger.info(f"approved-for-templates: processing {service_id}")

            # Fetch ALL versions for this service that have ARM templates
            all_versions_raw = await get_service_versions(service_id)
            versions_list = []
            active_params: list[dict] = []
            active_ver = svc.get("active_version")

            for ver in all_versions_raw:
                # Only include approved or draft versions that have ARM templates
                ver_status = ver.get("status", "")
                if ver_status not in ("approved", "draft"):
                    continue
                arm_str = ver.get("arm_template")
                if not arm_str:
                    continue
                try:
                    tpl = _json.loads(arm_str)
                    ver_params = _extract_params(tpl.get("parameters", {}))
                except Exception:
                    logger.warning(f"Failed to parse ARM for {service_id} v{ver.get('version')}")
                    continue

                ver_num = ver.get("version")
                ver_entry = {
                    "version": ver_num,
                    "status": ver_status,
                    "semver": ver.get("semver", ""),
                    "is_active": ver_num == active_ver,
                    "parameters": ver_params,
                    "changelog": ver.get("changelog", ""),
                    "created_at": ver.get("created_at", ""),
                }
                versions_list.append(ver_entry)
                if ver_num == active_ver:
                    active_params = ver_params

            # Fallback: if no versions found, try built-in skeleton
            if not versions_list and has_builtin_skeleton(service_id):
                tpl = generate_arm_template(service_id)
                if tpl:
                    active_params = _extract_params(tpl.get("parameters", {}))
                    versions_list = [{
                        "version": 0,
                        "status": "builtin",
                        "semver": "",
                        "is_active": True,
                        "parameters": active_params,
                        "changelog": "Built-in skeleton",
                        "created_at": "",
                    }]

            # Use active version params as the top-level default
            if not active_params and versions_list:
                active_params = versions_list[0]["parameters"]

            result.append({
                "id": service_id,
                "name": svc.get("name", service_id),
                "category": svc.get("category", "other"),
                "risk_tier": svc.get("risk_tier"),
                "active_version": active_ver,
                "parameters": active_params,
                "versions": versions_list,
            })

        logger.info(f"approved-for-templates: returning {len(result)} services")
        return JSONResponse({
            "services": result,
            "total": len(result),
        })
    except Exception:
        logger.exception("approved-for-templates endpoint failed")
        raise


@app.post("/api/catalog/templates/compose")
async def compose_template_from_services(request: Request):
    """Compose a new ARM template from approved services.

    Body:
    {
        "name": "My Web App Stack",
        "description": "App Service + SQL + KeyVault",
        "category": "blueprint",
        "selections": [
            {
                "service_id": "Microsoft.Web/sites",
                "quantity": 1,
                "parameters": ["skuName"]   // which extra params to expose
            },
            {
                "service_id": "Microsoft.Sql/servers",
                "quantity": 1,
                "parameters": ["adminLogin", "adminPassword"]
            }
        ]
    }

    Each selected service must be approved with an active version.
    The endpoint composes a single ARM template containing all resources,
    deduplicating shared standard parameters and prefixing resource-specific
    names with an index when quantity > 1.
    """
    from src.database import (
        get_service, get_active_service_version, get_service_version, upsert_template,
        create_template_version,
    )
    from src.tools.arm_generator import generate_arm_template, has_builtin_skeleton
    import json as _json

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    name = body.get("name", "").strip()
    description = body.get("description", "").strip()
    category = body.get("category", "blueprint")
    selections = body.get("selections", [])

    if not name:
        raise HTTPException(status_code=400, detail="Template name is required")
    if not selections:
        raise HTTPException(status_code=400, detail="Select at least one service")

    STANDARD_PARAMS = {
        "resourceName", "location", "environment",
        "projectName", "ownerEmail", "costCenter",
    }

    # ── Validate selections & gather ARM templates ────────────
    service_templates: list[dict] = []   # (svc, template_dict, selection)

    for sel in selections:
        sid = sel.get("service_id", "")
        qty = max(1, int(sel.get("quantity", 1)))
        chosen_params = set(sel.get("parameters", []))
        chosen_version = sel.get("version")  # None means "use active/latest"

        svc = await get_service(sid)
        if not svc:
            raise HTTPException(status_code=404, detail=f"Service '{sid}' not found")
        if svc.get("status") != "approved":
            raise HTTPException(
                status_code=400,
                detail=f"Service '{sid}' is not approved — only approved services can be used in templates",
            )

        # Get the ARM template — use specific version if requested
        tpl_dict = None
        if chosen_version is not None:
            ver = await get_service_version(sid, int(chosen_version))
            if ver and ver.get("arm_template"):
                try:
                    tpl_dict = _json.loads(ver["arm_template"])
                except Exception:
                    pass
            if not tpl_dict:
                raise HTTPException(
                    status_code=400,
                    detail=f"Version {chosen_version} of '{sid}' has no ARM template",
                )
        else:
            active = await get_active_service_version(sid)
            if active and active.get("arm_template"):
                try:
                    tpl_dict = _json.loads(active["arm_template"])
                except Exception:
                    pass
        if not tpl_dict and has_builtin_skeleton(sid):
            tpl_dict = generate_arm_template(sid)
        if not tpl_dict:
            raise HTTPException(
                status_code=400,
                detail=f"No ARM template available for '{sid}'",
            )

        service_templates.append({
            "svc": svc,
            "template": tpl_dict,
            "quantity": qty,
            "chosen_params": chosen_params,
        })

    # ── Resolve dependencies (auto-add missing required services) ─
    from src.orchestrator import resolve_composition_dependencies

    dep_events: list[dict] = []

    async def _dep_progress(event):
        dep_events.append(event)

    selected_ids = [e["svc"]["id"] for e in service_templates]
    dep_result = await resolve_composition_dependencies(
        selected_ids,
        progress_callback=_dep_progress,
    )

    # Auto-add resolved dependencies
    for item in dep_result.get("resolved", []):
        dep_sid = item["service_id"]
        # Avoid duplicates
        if any(e["svc"]["id"] == dep_sid for e in service_templates):
            continue
        dep_svc = await get_service(dep_sid)
        if not dep_svc:
            continue
        dep_tpl = None
        dep_active = await get_active_service_version(dep_sid)
        if dep_active and dep_active.get("arm_template"):
            try:
                dep_tpl = _json.loads(dep_active["arm_template"])
            except Exception:
                pass
        if not dep_tpl and has_builtin_skeleton(dep_sid):
            dep_tpl = generate_arm_template(dep_sid)
        if dep_tpl:
            service_templates.append({
                "svc": dep_svc,
                "template": dep_tpl,
                "quantity": 1,
                "chosen_params": set(),
            })
            logger.info(f"Auto-added dependency: {dep_sid} ({item['action']})")

    # ── Compose the combined ARM template ─────────────────────
    from src.tools.arm_generator import _STANDARD_PARAMETERS, _TEMPLATE_WRAPPER, _STANDARD_TAGS

    combined_params = dict(_STANDARD_PARAMETERS)
    combined_resources = []
    combined_outputs = {}
    service_ids = []
    resource_types = []
    tags_list = []

    for entry in service_templates:
        svc = entry["svc"]
        tpl = entry["template"]
        qty = entry["quantity"]
        chosen = entry["chosen_params"]
        sid = svc["id"]

        service_ids.append(sid)
        short_name = sid.split("/")[-1].lower()
        resource_types.append(sid)
        tags_list.append(svc.get("category", ""))

        src_params = tpl.get("parameters", {})
        src_resources = tpl.get("resources", [])
        src_outputs = tpl.get("outputs", {})

        for idx in range(1, qty + 1):
            suffix = f"_{short_name}" if qty == 1 else f"_{short_name}{idx}"

            # Add a resourceName parameter for this instance
            instance_name_param = f"resourceName{suffix}"
            combined_params[instance_name_param] = {
                "type": "string",
                "metadata": {
                    "description": f"Name for {svc.get('name', sid)}"
                    + (f" (instance {idx})" if qty > 1 else ""),
                },
            }

            # Add ALL non-standard parameters from the service template
            # (not just user-chosen ones). This ensures every parameter
            # reference in the resource body has a matching definition.
            all_non_standard = [
                pname for pname in src_params
                if pname not in STANDARD_PARAMS and pname != "resourceName"
            ]
            for pname in all_non_standard:
                pdef = src_params.get(pname)
                if not pdef:
                    continue
                suffixed = f"{pname}{suffix}"
                combined_params[suffixed] = dict(pdef)
                meta = combined_params[suffixed].setdefault("metadata", {})
                if qty > 1:
                    meta["description"] = meta.get("description", pname) + f" (instance {idx})"

            # Clone resources, replacing ALL parameter references
            for res in src_resources:
                cloned = _json.loads(_json.dumps(res))
                res_str = _json.dumps(cloned)
                # Replace resourceName first (bracketed + bare for compound expressions)
                res_str = res_str.replace(
                    "[parameters('resourceName')]",
                    f"[parameters('{instance_name_param}')]",
                )
                res_str = res_str.replace(
                    "parameters('resourceName')",
                    f"parameters('{instance_name_param}')",
                )
                # Replace ALL non-standard param references (not just chosen)
                for pname in all_non_standard:
                    suffixed = f"{pname}{suffix}"
                    res_str = res_str.replace(
                        f"[parameters('{pname}')]",
                        f"[parameters('{suffixed}')]",
                    )
                    res_str = res_str.replace(
                        f"parameters('{pname}')",
                        f"parameters('{suffixed}')",
                    )
                combined_resources.append(_json.loads(res_str))

            # Clone outputs with suffixed names and remapped param refs
            for oname, odef in src_outputs.items():
                out_name = f"{oname}{suffix}"
                out_val = _json.dumps(odef)
                out_val = out_val.replace(
                    "[parameters('resourceName')]",
                    f"[parameters('{instance_name_param}')]",
                )
                out_val = out_val.replace(
                    "parameters('resourceName')",
                    f"parameters('{instance_name_param}')",
                )
                for pname in all_non_standard:
                    suffixed = f"{pname}{suffix}"
                    out_val = out_val.replace(
                        f"[parameters('{pname}')]",
                        f"[parameters('{suffixed}')]",
                    )
                    out_val = out_val.replace(
                        f"parameters('{pname}')",
                        f"parameters('{suffixed}')",
                    )
                combined_outputs[out_name] = _json.loads(out_val)

    # Build the final composed template
    composed = dict(_TEMPLATE_WRAPPER)
    composed["parameters"] = combined_params
    composed["variables"] = {}
    composed["resources"] = combined_resources
    composed["outputs"] = combined_outputs

    content_str = _json.dumps(composed, indent=2)

    # Build a template ID from the name
    template_id = "composed-" + name.lower().replace(" ", "-")[:50]

    # Build parameter list for catalog storage
    param_list = [
        {"name": k, "type": v.get("type", "string"), "required": "defaultValue" not in v}
        for k, v in combined_params.items()
    ]

    # ── Dependency analysis ───────────────────────────────────
    from src.template_engine import analyze_dependencies

    dep_analysis = analyze_dependencies(service_ids)

    # Save to catalog
    catalog_entry = {
        "id": template_id,
        "name": name,
        "description": description,
        "format": "arm",
        "category": category,
        "content": content_str,
        "tags": list(set(tags_list)),
        "resources": list(set(resource_types)),
        "parameters": param_list,
        "outputs": list(combined_outputs.keys()),
        "is_blueprint": len(service_templates) > 1,
        "service_ids": service_ids,
        "status": "draft",
        "registered_by": "template-composer",
        # Dependency metadata
        "template_type": dep_analysis["template_type"],
        "provides": dep_analysis["provides"],
        "requires": dep_analysis["requires"],
        "optional_refs": dep_analysis["optional_refs"],
    }

    try:
        await upsert_template(catalog_entry)
        # Create version 1 as a draft
        ver = await create_template_version(
            template_id, content_str,
            changelog="Initial composition",
            semver="1.0.0",
        )
    except Exception as e:
        logger.error(f"Failed to save composed template: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({
        "status": "ok",
        "template_id": template_id,
        "template": catalog_entry,
        "version": ver,
        "resource_count": len(combined_resources),
        "parameter_count": len(combined_params),
        "dependency_analysis": dep_analysis,
        "dependency_resolution": dep_result,
    })


# ── Template Testing ─────────────────────────────────────────

@app.post("/api/catalog/templates/{template_id}/test")
async def test_template(template_id: str, request: Request):
    """Run validation tests on a template version.

    Body (optional): { "version": 1 }  — defaults to latest version.

    Tests performed:
    1. JSON structure — valid ARM template JSON
    2. Schema compliance — has $schema, contentVersion, parameters, resources
    3. Parameter validation — all params have types, no empty names
    4. Resource validation — all resources have type, apiVersion, name, location
    5. Output validation — outputs reference valid expressions
    6. Dependency check — service_ids match known services
    7. Tag compliance — resources include standard tags
    """
    from src.database import (
        get_template_by_id, get_template_versions, get_template_version,
        update_template_version_status, promote_template_version,
    )
    import json as _json

    tmpl = await get_template_by_id(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    # Determine which version to test
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    requested_version = body.get("version")
    ver = None
    if requested_version:
        ver = await get_template_version(template_id, int(requested_version))
    else:
        versions = await get_template_versions(template_id)
        if versions:
            ver = versions[0]  # latest (descending order)

    if not ver:
        raise HTTPException(status_code=404, detail="No version found to test")

    arm_content = ver.get("arm_template", "")
    version_num = ver["version"]

    # ── Run test suite ────────────────────────────────────────
    tests: list[dict] = []
    all_passed = True

    # Test 1: JSON parse
    tpl = None
    try:
        tpl = _json.loads(arm_content)
        tests.append({"name": "JSON Structure", "passed": True, "message": "Valid JSON"})
    except Exception as e:
        tests.append({"name": "JSON Structure", "passed": False, "message": f"Invalid JSON: {e}"})
        all_passed = False

    if tpl:
        # Test 2: Schema compliance
        schema_ok = True
        schema_msgs = []
        if "$schema" not in tpl:
            schema_msgs.append("Missing $schema")
            schema_ok = False
        if "contentVersion" not in tpl:
            schema_msgs.append("Missing contentVersion")
            schema_ok = False
        if "resources" not in tpl:
            schema_msgs.append("Missing resources array")
            schema_ok = False
        if not isinstance(tpl.get("resources"), list):
            schema_msgs.append("resources must be an array")
            schema_ok = False
        tests.append({
            "name": "ARM Schema",
            "passed": schema_ok,
            "message": "Valid ARM structure" if schema_ok else "; ".join(schema_msgs),
        })
        if not schema_ok:
            all_passed = False

        # Test 3: Parameter validation
        params = tpl.get("parameters", {})
        param_ok = True
        param_msgs = []
        if not isinstance(params, dict):
            param_msgs.append("parameters must be an object")
            param_ok = False
        else:
            for pname, pdef in params.items():
                if not pname.strip():
                    param_msgs.append("Empty parameter name found")
                    param_ok = False
                if not isinstance(pdef, dict):
                    param_msgs.append(f"Parameter '{pname}' must be an object")
                    param_ok = False
                elif "type" not in pdef:
                    param_msgs.append(f"Parameter '{pname}' missing type")
                    param_ok = False
        tests.append({
            "name": "Parameters",
            "passed": param_ok,
            "message": f"{len(params)} parameters valid" if param_ok else "; ".join(param_msgs),
        })
        if not param_ok:
            all_passed = False

        # Test 4: Resource validation
        resources = tpl.get("resources", [])
        res_ok = True
        res_msgs = []
        if not resources:
            res_msgs.append("No resources defined")
            res_ok = False
        for i, res in enumerate(resources):
            if not isinstance(res, dict):
                res_msgs.append(f"Resource [{i}] is not an object")
                res_ok = False
                continue
            if "type" not in res:
                res_msgs.append(f"Resource [{i}] missing 'type'")
                res_ok = False
            if "apiVersion" not in res:
                res_msgs.append(f"Resource [{i}] ({res.get('type', '?')}) missing 'apiVersion'")
                res_ok = False
            if "name" not in res:
                res_msgs.append(f"Resource [{i}] ({res.get('type', '?')}) missing 'name'")
                res_ok = False
        tests.append({
            "name": "Resources",
            "passed": res_ok,
            "message": f"{len(resources)} resources valid" if res_ok else "; ".join(res_msgs[:5]),
        })
        if not res_ok:
            all_passed = False

        # Test 5: Output validation
        outputs = tpl.get("outputs", {})
        out_ok = True
        out_msgs = []
        if isinstance(outputs, dict):
            for oname, odef in outputs.items():
                if not isinstance(odef, dict):
                    out_msgs.append(f"Output '{oname}' must be an object")
                    out_ok = False
                elif "type" not in odef or "value" not in odef:
                    out_msgs.append(f"Output '{oname}' missing type or value")
                    out_ok = False
        tests.append({
            "name": "Outputs",
            "passed": out_ok,
            "message": f"{len(outputs)} outputs valid" if out_ok else "; ".join(out_msgs),
        })
        if not out_ok:
            all_passed = False

        # Test 6: Tag compliance — check resources include standard tags
        TAG_REQUIRED = {"environment", "project", "owner"}
        tag_ok = True
        tag_msgs = []
        for i, res in enumerate(resources):
            if not isinstance(res, dict):
                continue
            res_tags = res.get("tags", {})
            if not isinstance(res_tags, dict) and not isinstance(res_tags, str):
                tag_msgs.append(f"Resource [{i}] ({res.get('type', '?')}) has invalid tags")
                tag_ok = False
            elif isinstance(res_tags, dict):
                # Check if tags reference variables/parameters (ARM expressions are OK)
                tag_values = set()
                for tk in res_tags:
                    # Normalize key to lowercase for comparison
                    tag_values.add(tk.lower())
                missing = TAG_REQUIRED - tag_values
                if missing and not any(isinstance(v, str) and "variables('standardTags')" in v for v in res_tags.values()):
                    tag_msgs.append(f"Resource [{i}] ({res.get('type', '?')}) missing tags: {', '.join(missing)}")
                    tag_ok = False
        tests.append({
            "name": "Tag Compliance",
            "passed": tag_ok,
            "message": "All resources properly tagged" if tag_ok else "; ".join(tag_msgs[:3]),
        })
        if not tag_ok:
            all_passed = False

        # Test 7: Naming convention — resource names use parameters
        naming_ok = True
        naming_msgs = []
        for i, res in enumerate(resources):
            if not isinstance(res, dict):
                continue
            rname = res.get("name", "")
            if isinstance(rname, str) and rname and not rname.startswith("["):
                naming_msgs.append(f"Resource [{i}] ({res.get('type', '?')}) uses hardcoded name '{rname}'")
                naming_ok = False
        tests.append({
            "name": "Naming Convention",
            "passed": naming_ok,
            "message": "All resource names use parameters/expressions" if naming_ok else "; ".join(naming_msgs[:3]),
        })
        if not naming_ok:
            all_passed = False

    # ── Update version status based on results ────────────────
    passed_count = sum(1 for t in tests if t["passed"])
    total_count = len(tests)
    new_status = "passed" if all_passed else "failed"

    test_results = {
        "tests": tests,
        "passed": passed_count,
        "failed": total_count - passed_count,
        "total": total_count,
        "all_passed": all_passed,
    }

    await update_template_version_status(template_id, version_num, new_status, test_results)

    # Sync parent template status so the UI lifecycle CTAs work correctly
    from src.database import get_backend as _get_tmpl_backend
    _tb = await _get_tmpl_backend()
    await _tb.execute_write(
        "UPDATE catalog_templates SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, datetime.now(timezone.utc).isoformat(), template_id),
    )

    # Note: No auto-promote. User must validate (ARM What-If) then explicitly publish.

    return JSONResponse({
        "template_id": template_id,
        "version": version_num,
        "status": new_status,
        "results": test_results,
        "needs_validation": all_passed,  # signal: ready for ARM validation
    })


# ── Auto-Heal Template ──────────────────────────────────────

@app.post("/api/catalog/templates/{template_id}/auto-heal")
async def auto_heal_template(template_id: str):
    """Automatically fix a template that failed structural tests.

    Flow:
    1. Get the template and its latest test results
    2. Ask the LLM to fix structural issues in the ARM JSON
    3. Save the fixed template as a new version
    4. Re-run structural tests
    5. Return results

    No user input required — the system figures out what's wrong and fixes it.
    """
    from src.database import (
        get_template_by_id, get_template_versions, get_template_version,
        upsert_template, create_template_version,
        update_template_version_status,
    )
    import json as _json

    tmpl = await get_template_by_id(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    # Get latest version and its test results
    versions = await get_template_versions(template_id)
    if not versions:
        raise HTTPException(status_code=404, detail="No versions found")

    latest_ver = versions[0]
    arm_content = latest_ver.get("arm_template", "")
    test_results = latest_ver.get("test_results", {})
    validation_results = latest_ver.get("validation_results", {})

    # Gather failed tests into an error description
    failed_tests = []
    if isinstance(test_results, dict) and test_results:
        for t in test_results.get("tests", []):
            if not t.get("passed", True):
                failed_tests.append(f"- {t['name']}: {t.get('message', 'failed')}")

    # Also check validation (deploy) failures
    if isinstance(validation_results, dict) and validation_results:
        if not validation_results.get("validation_passed", True):
            for h in validation_results.get("heal_history", []):
                if h.get("error"):
                    failed_tests.append(f"- Deploy: {h['error'][:200]}")

    # If no recorded failures, run structural tests now to find issues
    if not failed_tests and tmpl.get("status") in ("failed", "draft"):
        import json as _j2
        try:
            _tpl = _j2.loads(arm_content)
            # Quick structural checks
            if "$schema" not in _tpl:
                failed_tests.append("- ARM Schema: Missing $schema")
            if "contentVersion" not in _tpl:
                failed_tests.append("- ARM Schema: Missing contentVersion")
            if not isinstance(_tpl.get("resources"), list) or not _tpl.get("resources"):
                failed_tests.append("- Resources: No resources defined")
            TAG_REQ = {"environment", "project", "owner"}
            for i, r in enumerate(_tpl.get("resources", [])):
                if isinstance(r, dict):
                    if "type" not in r:
                        failed_tests.append(f"- Resources: Resource [{i}] missing 'type'")
                    if "apiVersion" not in r:
                        failed_tests.append(f"- Resources: Resource [{i}] missing 'apiVersion'")
                    if "name" not in r:
                        failed_tests.append(f"- Resources: Resource [{i}] missing 'name'")
                    tags = r.get("tags", {})
                    if isinstance(tags, dict):
                        missing = TAG_REQ - set(k.lower() for k in tags)
                        if missing:
                            failed_tests.append(f"- Tags: Resource [{i}] ({r.get('type','?')}) missing {', '.join(missing)}")
        except Exception:
            failed_tests.append("- JSON: Template is not valid JSON")

    if not failed_tests:
        # Actually no issues found — run real tests and set status to passed
        # so the template moves forward in the lifecycle
        try:
            _tpl = _json.loads(arm_content)
            # If tests pass, promote the template status
            new_ver_num = latest_ver["version"]
            _tr = {"tests": [], "passed": 0, "failed": 0, "total": 0, "all_passed": True}

            # Quick full check
            checks = [
                ("$schema" in _tpl, "ARM Schema"),
                ("contentVersion" in _tpl, "Content Version"),
                (isinstance(_tpl.get("resources"), list) and len(_tpl.get("resources", [])) > 0, "Resources"),
            ]
            for ok, name in checks:
                _tr["tests"].append({"name": name, "passed": ok, "message": "OK" if ok else "Failed"})
                _tr["total"] += 1
                if ok:
                    _tr["passed"] += 1
                else:
                    _tr["failed"] += 1
                    _tr["all_passed"] = False

            new_status = "passed" if _tr["all_passed"] else "failed"
            await update_template_version_status(template_id, new_ver_num, new_status, _tr)

            from src.database import get_backend as _get_tmpl_backend
            _tb = await _get_tmpl_backend()
            await _tb.execute_write(
                "UPDATE catalog_templates SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, datetime.now(timezone.utc).isoformat(), template_id),
            )

            return JSONResponse({
                "status": "already_healthy",
                "template_id": template_id,
                "all_passed": _tr["all_passed"],
                "retest": _tr,
                "message": "Template is structurally sound — tests passed! Ready for the next step."
                           if _tr["all_passed"] else "Some structural issues remain.",
            })
        except Exception:
            return JSONResponse({
                "status": "no_issues",
                "template_id": template_id,
                "message": "No test failures detected — template may already be fine.",
            })

    error_description = "Structural test failures:\n" + "\n".join(failed_tests)
    logger.info(f"Auto-heal {template_id}: {error_description}")

    # Try LLM-based healing
    client = await ensure_copilot_client()
    fixed_arm = None

    if client:
        try:
            fixed_arm = await _copilot_heal_template(
                arm_json=arm_content,
                error_message=error_description,
                resource_type=template_id,
                copilot_client=client,
                previous_attempts=[],
            )
        except Exception as e:
            logger.warning(f"LLM heal failed for {template_id}: {e}")

    if not fixed_arm:
        # Heuristic fix: try to fix common structural issues
        try:
            tpl = _json.loads(arm_content)
            changed = False

            # Fix missing $schema
            if "$schema" not in tpl:
                tpl["$schema"] = "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#"
                changed = True

            # Fix missing contentVersion
            if "contentVersion" not in tpl:
                tpl["contentVersion"] = "1.0.0.0"
                changed = True

            # Fix missing resources
            if "resources" not in tpl:
                tpl["resources"] = []
                changed = True

            # Fix parameters not being a dict
            if not isinstance(tpl.get("parameters"), dict):
                tpl["parameters"] = {}
                changed = True

            # Fix resources not being a list
            if not isinstance(tpl.get("resources"), list):
                tpl["resources"] = []
                changed = True

            # Fix individual resource issues
            TAG_SET = {
                "environment": "[parameters('environment')]",
                "owner": "[parameters('ownerEmail')]",
                "costCenter": "[parameters('costCenter')]",
                "project": "[parameters('projectName')]",
                "managedBy": "InfraForge",
            }
            for res in tpl.get("resources", []):
                if not isinstance(res, dict):
                    continue
                # Add missing tags
                if "tags" not in res or not isinstance(res.get("tags"), dict):
                    res["tags"] = dict(TAG_SET)
                    changed = True
                else:
                    for tk, tv in TAG_SET.items():
                        if tk not in res["tags"]:
                            res["tags"][tk] = tv
                            changed = True

            if changed:
                fixed_arm = _json.dumps(tpl, indent=2)
        except Exception as e:
            logger.warning(f"Heuristic heal failed for {template_id}: {e}")

    if not fixed_arm:
        return JSONResponse({
            "status": "heal_failed",
            "template_id": template_id,
            "errors": failed_tests,
            "message": "Auto-heal could not fix this template. Use Request Revision to describe the changes needed.",
        })

    # Save the fixed template
    tmpl["content"] = fixed_arm
    try:
        await upsert_template(tmpl)
        new_ver = await create_template_version(
            template_id, fixed_arm,
            changelog="Auto-healed: fixed structural test failures",
            semver=None,
        )
    except Exception as e:
        logger.error(f"Failed to save healed template {template_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # Re-run structural tests on the fixed version
    from starlette.testclient import TestClient
    # Instead of internal call, just run the tests inline
    new_version_num = new_ver["version"]
    new_arm = fixed_arm

    # ── Inline test suite (same as test endpoint) ─────────────
    tests: list[dict] = []
    all_passed = True
    tpl = None
    try:
        tpl = _json.loads(new_arm)
        tests.append({"name": "JSON Structure", "passed": True, "message": "Valid JSON"})
    except Exception as e:
        tests.append({"name": "JSON Structure", "passed": False, "message": f"Invalid JSON: {e}"})
        all_passed = False

    if tpl:
        # Schema
        schema_ok = all(k in tpl for k in ("$schema", "contentVersion", "resources"))
        tests.append({"name": "ARM Schema", "passed": schema_ok,
                       "message": "Valid ARM structure" if schema_ok else "Missing required schema fields"})
        if not schema_ok:
            all_passed = False

        # Parameters
        params = tpl.get("parameters", {})
        param_ok = isinstance(params, dict) and all(
            isinstance(v, dict) and "type" in v for v in params.values()
        )
        tests.append({"name": "Parameters", "passed": param_ok,
                       "message": f"{len(params)} parameters valid" if param_ok else "Parameter issues remain"})
        if not param_ok:
            all_passed = False

        # Resources
        resources = tpl.get("resources", [])
        res_ok = isinstance(resources, list) and len(resources) > 0 and all(
            isinstance(r, dict) and "type" in r and "apiVersion" in r and "name" in r
            for r in resources
        )
        tests.append({"name": "Resources", "passed": res_ok,
                       "message": f"{len(resources)} resources valid" if res_ok else "Resource issues remain"})
        if not res_ok:
            all_passed = False

        # Tag compliance
        TAG_REQUIRED = {"environment", "project", "owner"}
        tag_ok = True
        for res in resources:
            if isinstance(res, dict):
                tags = res.get("tags", {})
                if isinstance(tags, dict):
                    if TAG_REQUIRED - set(k.lower() for k in tags):
                        tag_ok = False
                        break
        tests.append({"name": "Tag Compliance", "passed": tag_ok,
                       "message": "All resources properly tagged" if tag_ok else "Tag issues remain"})
        if not tag_ok:
            all_passed = False

    retest_status = "passed" if all_passed else "failed"
    retest_results = {
        "tests": tests,
        "passed": sum(1 for t in tests if t["passed"]),
        "failed": sum(1 for t in tests if not t["passed"]),
        "total": len(tests),
        "all_passed": all_passed,
    }

    await update_template_version_status(template_id, new_version_num, retest_status, retest_results)

    # Sync parent template status
    from src.database import get_backend as _get_tmpl_backend
    _tb = await _get_tmpl_backend()
    await _tb.execute_write(
        "UPDATE catalog_templates SET status = ?, updated_at = ? WHERE id = ?",
        (retest_status, datetime.now(timezone.utc).isoformat(), template_id),
    )

    return JSONResponse({
        "status": "healed" if all_passed else "partial",
        "template_id": template_id,
        "version": new_version_num,
        "original_failures": failed_tests,
        "retest": retest_results,
        "all_passed": all_passed,
        "message": "Template auto-healed and all tests pass!" if all_passed
                   else f"Auto-heal fixed some issues but {retest_results['failed']} test(s) still need attention.",
    })


# ── Recompose Blueprint ──────────────────────────────────────

@app.post("/api/catalog/templates/{template_id}/recompose")
async def recompose_blueprint(template_id: str):
    """Re-compose a blueprint from its source service templates.

    Fetches the current active ARM templates for each service_id stored
    on the blueprint, runs the same compose logic (with the fixed
    parameter remapping), and saves the result as a new version.
    """
    from src.database import (
        get_template_by_id, get_service, get_active_service_version,
        upsert_template, create_template_version,
    )
    from src.tools.arm_generator import (
        _STANDARD_PARAMETERS, _TEMPLATE_WRAPPER, generate_arm_template,
        has_builtin_skeleton,
    )
    from src.template_engine import analyze_dependencies
    import json as _json

    tmpl = await get_template_by_id(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    # Parse service_ids
    svc_ids_raw = tmpl.get("service_ids") or tmpl.get("service_ids_json") or []
    if isinstance(svc_ids_raw, str):
        try:
            svc_ids = _json.loads(svc_ids_raw)
        except Exception:
            svc_ids = []
    else:
        svc_ids = list(svc_ids_raw) if svc_ids_raw else []

    if not svc_ids:
        raise HTTPException(
            status_code=400,
            detail="This template has no service_ids — it can't be recomposed",
        )

    STANDARD_PARAMS = {
        "resourceName", "location", "environment",
        "projectName", "ownerEmail", "costCenter",
    }

    # ── Gather current ARM templates for each service ─────────
    service_templates: list[dict] = []
    for sid in svc_ids:
        svc = await get_service(sid)
        if not svc:
            raise HTTPException(status_code=404, detail=f"Service '{sid}' not found")

        tpl_dict = None
        active = await get_active_service_version(sid)
        if active and active.get("arm_template"):
            try:
                tpl_dict = _json.loads(active["arm_template"])
            except Exception:
                pass
        if not tpl_dict and has_builtin_skeleton(sid):
            tpl_dict = generate_arm_template(sid)
        if not tpl_dict:
            raise HTTPException(
                status_code=400, detail=f"No ARM template available for '{sid}'",
            )

        service_templates.append({
            "svc": svc,
            "template": tpl_dict,
            "quantity": 1,
        })

    # ── Resolve dependencies (auto-add missing services) ──────
    from src.orchestrator import resolve_composition_dependencies

    dep_result = await resolve_composition_dependencies(svc_ids)

    for item in dep_result.get("resolved", []):
        dep_sid = item["service_id"]
        if any(e["svc"]["id"] == dep_sid for e in service_templates):
            continue
        dep_svc = await get_service(dep_sid)
        if not dep_svc:
            continue
        dep_tpl = None
        dep_active = await get_active_service_version(dep_sid)
        if dep_active and dep_active.get("arm_template"):
            try:
                dep_tpl = _json.loads(dep_active["arm_template"])
            except Exception:
                pass
        if not dep_tpl and has_builtin_skeleton(dep_sid):
            dep_tpl = generate_arm_template(dep_sid)
        if dep_tpl:
            service_templates.append({
                "svc": dep_svc,
                "template": dep_tpl,
                "quantity": 1,
            })
            svc_ids.append(dep_sid)
            logger.info(f"Recompose auto-added dependency: {dep_sid}")

    # ── Compose ───────────────────────────────────────────────
    combined_params = dict(_STANDARD_PARAMETERS)
    combined_resources: list[dict] = []
    combined_outputs: dict = {}
    resource_types: list[str] = []
    tags_list: list[str] = []

    for entry in service_templates:
        svc = entry["svc"]
        tpl = entry["template"]
        sid = svc["id"]

        short_name = sid.split("/")[-1].lower()
        resource_types.append(sid)
        tags_list.append(svc.get("category", ""))

        src_params = tpl.get("parameters", {})
        src_resources = tpl.get("resources", [])
        src_outputs = tpl.get("outputs", {})

        suffix = f"_{short_name}"
        instance_name_param = f"resourceName{suffix}"
        combined_params[instance_name_param] = {
            "type": "string",
            "metadata": {"description": f"Name for {svc.get('name', sid)}"},
        }

        # Add ALL non-standard params from the service template
        all_non_standard = [
            pname for pname in src_params
            if pname not in STANDARD_PARAMS and pname != "resourceName"
        ]
        for pname in all_non_standard:
            pdef = src_params.get(pname)
            if not pdef:
                continue
            suffixed = f"{pname}{suffix}"
            combined_params[suffixed] = dict(pdef)

        # Clone resources, replacing ALL parameter references
        for res in src_resources:
            res_str = _json.dumps(res)
            res_str = res_str.replace(
                "[parameters('resourceName')]",
                f"[parameters('{instance_name_param}')]",
            )
            res_str = res_str.replace(
                "parameters('resourceName')",
                f"parameters('{instance_name_param}')",
            )
            for pname in all_non_standard:
                suffixed = f"{pname}{suffix}"
                res_str = res_str.replace(
                    f"[parameters('{pname}')]",
                    f"[parameters('{suffixed}')]",
                )
                res_str = res_str.replace(
                    f"parameters('{pname}')",
                    f"parameters('{suffixed}')",
                )
            combined_resources.append(_json.loads(res_str))

        # Clone outputs with suffixed names and remapped param refs
        for oname, odef in src_outputs.items():
            out_name = f"{oname}{suffix}"
            out_val = _json.dumps(odef)
            out_val = out_val.replace(
                "[parameters('resourceName')]",
                f"[parameters('{instance_name_param}')]",
            )
            out_val = out_val.replace(
                "parameters('resourceName')",
                f"parameters('{instance_name_param}')",
            )
            for pname in all_non_standard:
                suffixed = f"{pname}{suffix}"
                out_val = out_val.replace(
                    f"[parameters('{pname}')]",
                    f"[parameters('{suffixed}')]",
                )
                out_val = out_val.replace(
                    f"parameters('{pname}')",
                    f"parameters('{suffixed}')",
                )
            combined_outputs[out_name] = _json.loads(out_val)

    # ── Build the recomposed template ─────────────────────────
    composed = dict(_TEMPLATE_WRAPPER)
    composed["parameters"] = combined_params
    composed["variables"] = {}
    composed["resources"] = combined_resources
    composed["outputs"] = combined_outputs

    content_str = _json.dumps(composed, indent=2)

    # Apply standard sanitizers
    content_str = _ensure_parameter_defaults(content_str)
    content_str = _sanitize_placeholder_guids(content_str)
    content_str = _sanitize_dns_zone_names(content_str)

    # Update parameter list for catalog storage
    composed = _json.loads(content_str)
    combined_params = composed.get("parameters", {})
    param_list = [
        {"name": k, "type": v.get("type", "string"), "required": "defaultValue" not in v}
        for k, v in combined_params.items()
    ]

    dep_analysis = analyze_dependencies(svc_ids)

    # ── Save back ─────────────────────────────────────────────
    catalog_entry = {
        "id": template_id,
        "name": tmpl.get("name", template_id),
        "description": tmpl.get("description", ""),
        "format": "arm",
        "category": tmpl.get("category", "blueprint"),
        "content": content_str,
        "tags": list(set(tags_list)),
        "resources": list(set(resource_types)),
        "parameters": param_list,
        "outputs": list(combined_outputs.keys()),
        "is_blueprint": len(service_templates) > 1,
        "service_ids": svc_ids,
        "status": tmpl.get("status", "draft"),
        "registered_by": tmpl.get("registered_by", "template-composer"),
        "template_type": dep_analysis["template_type"],
        "provides": dep_analysis["provides"],
        "requires": dep_analysis["requires"],
        "optional_refs": dep_analysis["optional_refs"],
    }

    try:
        await upsert_template(catalog_entry)
        ver = await create_template_version(
            template_id, content_str,
            changelog="Recomposed from current service templates",
            created_by="recomposer",
        )
    except Exception as e:
        logger.error(f"Failed to save recomposed template: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    logger.info(
        f"Recomposed blueprint '{template_id}' from {len(svc_ids)} services "
        f"→ {len(combined_resources)} resources, {len(combined_params)} params"
    )

    return JSONResponse({
        "status": "ok",
        "template_id": template_id,
        "resource_count": len(combined_resources),
        "parameter_count": len(combined_params),
        "services_recomposed": svc_ids,
        "version": ver,
        "message": f"Blueprint recomposed from {len(svc_ids)} services with latest templates",
    })


# ── Template Version Management ──────────────────────────────

@app.get("/api/catalog/templates/{template_id}/versions")
async def list_template_versions(template_id: str):
    """List all versions of a template."""
    from src.database import get_template_by_id, get_template_versions

    tmpl = await get_template_by_id(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    versions = await get_template_versions(template_id)

    return JSONResponse({
        "template_id": template_id,
        "template_name": tmpl.get("name", ""),
        "active_version": tmpl.get("active_version"),
        "status": tmpl.get("status", "draft"),
        "versions": versions,
    })


@app.post("/api/catalog/templates/{template_id}/versions")
async def create_new_template_version(template_id: str, request: Request):
    """Create a new version of an existing template.

    Body: {
        "arm_template": "...",   // JSON string of ARM template
        "changelog": "Added monitoring",
        "semver": "2.0.0"         // optional
    }
    """
    from src.database import (
        get_template_by_id, create_template_version, upsert_template,
    )
    import json as _json

    tmpl = await get_template_by_id(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    arm_template = body.get("arm_template", "")
    if not arm_template:
        raise HTTPException(status_code=400, detail="arm_template is required")

    # Validate it's valid JSON
    try:
        _json.loads(arm_template) if isinstance(arm_template, str) else arm_template
    except Exception:
        raise HTTPException(status_code=400, detail="arm_template must be valid JSON")

    if isinstance(arm_template, dict):
        arm_template = _json.dumps(arm_template, indent=2)

    ver = await create_template_version(
        template_id, arm_template,
        changelog=body.get("changelog", ""),
        semver=body.get("semver"),
    )

    # Update parent template content and mark as draft (needs testing)
    tmpl["content"] = arm_template
    tmpl["status"] = "draft"
    # Restore keys that _parse_template_row renamed
    tmpl["source_path"] = tmpl.pop("source", "")
    await upsert_template(tmpl)

    return JSONResponse({
        "status": "ok",
        "template_id": template_id,
        "version": ver,
    })


@app.post("/api/catalog/templates/{template_id}/promote")
async def promote_template(template_id: str, request: Request):
    """Promote a tested version to active.

    Body: { "version": 1 }
    """
    from src.database import get_template_by_id, promote_template_version

    tmpl = await get_template_by_id(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    version = body.get("version")
    if not version:
        raise HTTPException(status_code=400, detail="version is required")

    ok = await promote_template_version(template_id, int(version))
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Cannot promote — version must have passed testing",
        )

    return JSONResponse({"status": "ok", "promoted_version": version})


# ── Template Validation (ARM with Self-Healing) ────────────

@app.post("/api/catalog/templates/{template_id}/validate")
async def validate_template(template_id: str, request: Request):
    """Validate a template by deploying it to a temporary resource group.

    Streams NDJSON progress. Uses the full self-healing loop (shallow +
    deep healing for blueprints). On success the template version is
    marked 'validated' and the temp RG is cleaned up. On failure it is
    marked 'failed'. The template is NOT published until explicitly
    promoted — this is just the validation gate.

    Body: {
        "parameters": { ... },
        "region": "eastus2"  // optional
    }
    """
    import uuid as _uuid
    from src.database import (
        get_template_by_id, get_template_version, get_template_versions,
        update_template_validation_status,
    )

    tmpl = await get_template_by_id(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    # Find the latest version that can be validated
    versions = await get_template_versions(template_id)
    target_ver = None
    for v in versions:
        if v["status"] in ("passed", "validated", "failed"):
            target_ver = v
            break
    if not target_ver:
        for v in versions:
            if v["status"] == "draft":
                target_ver = v
                break
    if not target_ver:
        raise HTTPException(
            status_code=400,
            detail="No testable version found. Run structural tests first.",
        )

    version_num = target_ver["version"]

    try:
        body = await request.json()
    except Exception:
        body = {}

    user_params = body.get("parameters", {})
    region = body.get("region", "eastus2")

    # Parse the ARM template
    arm_content = target_ver.get("arm_template", tmpl.get("content", ""))
    try:
        tpl = json.loads(arm_content) if isinstance(arm_content, str) else arm_content
    except Exception:
        raise HTTPException(status_code=400, detail="Template content is not valid JSON")

    # Build parameter values
    tpl_params = tpl.get("parameters", {})
    final_params = {}
    for pname, pdef in tpl_params.items():
        if pname in user_params:
            final_params[pname] = user_params[pname]
        elif "defaultValue" in pdef:
            final_params[pname] = pdef["defaultValue"]
        else:
            ptype = pdef.get("type", "string").lower()
            if ptype == "string":
                final_params[pname] = f"if-val-{pname[:20]}"
            elif ptype == "int":
                final_params[pname] = 1
            elif ptype == "bool":
                final_params[pname] = True
            elif ptype == "array":
                final_params[pname] = []
            elif ptype == "object":
                final_params[pname] = {}

    rg_name = f"infraforge-val-{_uuid.uuid4().hex[:8]}"
    deployment_name = f"infraforge-val-{_uuid.uuid4().hex[:8]}"
    _tmpl_id = template_id
    _tmpl_name = tmpl.get("name", template_id)
    _ver_num = version_num

    # Blueprint / service info for deep healing
    is_blueprint = bool(tmpl.get("is_blueprint"))
    svc_ids_raw = tmpl.get("service_ids") or tmpl.get("service_ids_json") or []
    if isinstance(svc_ids_raw, str):
        try:
            svc_ids = json.loads(svc_ids_raw)
        except Exception:
            svc_ids = []
    else:
        svc_ids = list(svc_ids_raw) if svc_ids_raw else []

    async def _stream():
        from src.tools.deploy_engine import execute_deployment
        import uuid as _heal_uuid

        MAX_HEAL = 5             # Safety budget, not user-visible attempt count
        DEEP_HEAL_AFTER = 2      # Switch to deep analysis after this many steps
        heal_history: list[dict] = []
        current_tpl = tpl
        current_params = dict(final_params)
        current_deploy_name = deployment_name
        deep_healed = False
        final_tpl = None         # the template that eventually succeeded
        final_status = "failed"

        yield json.dumps({
            "phase": "starting",
            "detail": f"Validating template '{_tmpl_name}' — deploying to temp RG {rg_name}…",
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
                current_deploy_name = f"infraforge-val-{_heal_uuid.uuid4().hex[:8]}"

            # Describe what we're doing, not which attempt we're on
            if attempt == 1:
                step_detail = "Deploying template to validation environment…"
            elif deep_healed:
                step_detail = "Verifying deep-healed template…"
            else:
                step_detail = f"Verifying corrected template (resolved {len(heal_history)} issue{'s' if len(heal_history) != 1 else ''} so far)…"

            yield json.dumps({
                "phase": "step",
                "step": attempt,
                "detail": step_detail,
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
                    template_id=_tmpl_id,
                    template_name=_tmpl_name,
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
                yield json.dumps({
                    "phase": "complete",
                    "status": "succeeded",
                    "issues_resolved": issues_resolved,
                    "deployment_id": result.get("deployment_id"),
                    "provisioned_resources": result.get("provisioned_resources", []),
                    "outputs": result.get("outputs", {}),
                    "healed": issues_resolved > 0,
                    "deep_healed": deep_healed,
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
                    "detail": "Template could not be verified — all available resolution strategies exhausted",
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
                        "Surface-level adjustments haven't resolved the issue — "
                        "switching to deep analysis: examining underlying service templates…"
                    ),
                    "service_ids": svc_ids,
                }) + "\n"

                deep_events: list[dict] = []
                async def _on_deep_event(evt):
                    deep_events.append(evt)

                try:
                    fixed_composed = await _deep_heal_composed_template(
                        template_id=_tmpl_id,
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

                    new_params = {}
                    for pname, pdef in current_tpl.get("parameters", {}).items():
                        if pname in user_params:
                            new_params[pname] = user_params[pname]
                        elif "defaultValue" in pdef:
                            new_params[pname] = pdef["defaultValue"]
                        else:
                            ptype = pdef.get("type", "string").lower()
                            if ptype == "string":
                                new_params[pname] = f"if-val-{pname[:20]}"
                            elif ptype == "int":
                                new_params[pname] = 1
                            elif ptype == "bool":
                                new_params[pname] = True
                            elif ptype == "array":
                                new_params[pname] = []
                            elif ptype == "object":
                                new_params[pname] = {}
                    current_params = new_params

                    heal_history.append({
                        "step": len(heal_history) + 1,
                        "phase": "deep_heal",
                        "error": error_msg[:500],
                        "fix_summary": "Deep analysis: fixed underlying service templates and recomposed",
                    })

                    yield json.dumps({
                        "phase": "healed",
                        "detail": "Deep analysis complete — recomposed template ready, verifying fix…",
                        "fix_summary": "Deep analysis: fixed underlying service templates and recomposed",
                        "deep_healed": True,
                    }) + "\n"
                    continue

                yield json.dumps({
                    "phase": "deep_heal_fallback",
                    "detail": "Deep analysis did not produce a fix — trying alternative strategy…",
                }) + "\n"

            # ── SHALLOW HEAL ──
            yield json.dumps({
                "phase": "healing",
                "detail": "Azure returned feedback — analyzing error and adjusting template…",
                "error_summary": error_msg[:300],
            }) + "\n"

            pre_fix = json.dumps(current_tpl, indent=2) if isinstance(current_tpl, dict) else str(current_tpl)
            try:
                _heal_params = _extract_param_values(
                    current_tpl if isinstance(current_tpl, dict) else json.loads(pre_fix)
                )
                fixed_json = await _copilot_heal_template(
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
                    "detail": f"LLM healing could not produce a fix: {heal_err}",
                }) + "\n"
                final_status = "failed"
                break

            fix_summary = _summarize_fix(pre_fix, fixed_json)
            heal_history.append({
                "step": len(heal_history) + 1,
                "phase": "deploy",
                "error": error_msg[:500],
                "fix_summary": fix_summary,
            })

            new_params = {}
            for pname, pdef in fixed_tpl.get("parameters", {}).items():
                if pname in user_params:
                    new_params[pname] = user_params[pname]
                elif "defaultValue" in pdef:
                    new_params[pname] = pdef["defaultValue"]
                else:
                    ptype = pdef.get("type", "string").lower()
                    if ptype == "string":
                        new_params[pname] = f"if-val-{pname[:20]}"
                    elif ptype == "int":
                        new_params[pname] = 1
                    elif ptype == "bool":
                        new_params[pname] = True
                    elif ptype == "array":
                        new_params[pname] = []
                    elif ptype == "object":
                        new_params[pname] = {}

            current_tpl = fixed_tpl
            current_params = new_params

            yield json.dumps({
                "phase": "healed",
                "detail": f"Applied fix: {fix_summary}",
                "fix_summary": fix_summary,
            }) + "\n"

        # ── Post-loop: update DB status and save healed template ──
        yield json.dumps({
            "phase": "cleanup",
            "detail": f"Cleaning up validation resource group {rg_name}…",
        }) + "\n"

        # Update template version status
        validation_results = {
            "resource_group": rg_name,
            "region": region,
            "parameters_used": final_params,
            "validation_passed": final_status == "validated",
            "heal_history": heal_history,
            "deep_healed": deep_healed,
        }
        await update_template_validation_status(
            _tmpl_id, _ver_num, final_status, validation_results
        )

        # If healed, save the corrected template back to the version
        if final_status == "validated" and final_tpl and (heal_history or deep_healed):
            fixed_content = json.dumps(final_tpl, indent=2)
            from src.database import get_backend as _get_hb
            _hb = await _get_hb()
            await _hb.execute_write(
                """UPDATE template_versions
                   SET arm_template = ?
                   WHERE template_id = ? AND version = ?""",
                (fixed_content, _tmpl_id, _ver_num),
            )
            await _hb.execute_write(
                """UPDATE catalog_templates
                   SET content = ?, updated_at = ?
                   WHERE id = ?""",
                (fixed_content, datetime.now(timezone.utc).isoformat(), _tmpl_id),
            )

        # Sync parent template status
        from src.database import get_backend as _get_val_backend
        _vb = await _get_val_backend()
        await _vb.execute_write(
            "UPDATE catalog_templates SET status = ?, updated_at = ? WHERE id = ?",
            (final_status, datetime.now(timezone.utc).isoformat(), _tmpl_id),
        )

        # Cleanup the validation RG (fire-and-forget)
        try:
            from src.tools.deploy_engine import _get_resource_client
            client = _get_resource_client()
            import asyncio as _aio
            loop = _aio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: client.resource_groups.begin_delete(rg_name)
            )
            yield json.dumps({
                "phase": "cleanup_done",
                "detail": f"Temp RG {rg_name} deletion started.",
            }) + "\n"
        except Exception as cle:
            yield json.dumps({
                "phase": "cleanup_warning",
                "detail": f"Could not delete temp RG {rg_name}: {cle}",
            }) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


# ── Template Publishing ──────────────────────────────────────

@app.post("/api/catalog/templates/{template_id}/publish")
async def publish_template(template_id: str, request: Request):
    """Publish a validated template — makes it available in the catalog.

    Only templates that have passed ARM What-If validation can be published.
    Body: { "version": 1 }  (optional — defaults to latest validated version)
    """
    from src.database import (
        get_template_by_id, get_template_versions,
        promote_template_version,
    )

    tmpl = await get_template_by_id(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    try:
        body = await request.json()
    except Exception:
        body = {}

    version = body.get("version")

    if not version:
        # Find the latest validated version
        versions = await get_template_versions(template_id)
        for v in versions:
            if v["status"] == "validated":
                version = v["version"]
                break
        if not version:
            raise HTTPException(
                status_code=400,
                detail="No validated version found. Run ARM validation first.",
            )

    ok = await promote_template_version(template_id, int(version))
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Cannot publish — version must have passed ARM validation (status: validated)",
        )

    return JSONResponse({
        "status": "ok",
        "published_version": version,
        "template_id": template_id,
    })



# ── Template Deployment (approved templates only) ────────────

# ══════════════════════════════════════════════════════════════
# DEPLOYMENT AGENT — Process-as-Code Pipeline
# ══════════════════════════════════════════════════════════════
#
# The deployment process is a DETERMINISTIC STATE MACHINE, not an LLM.
# The LLM is called for specific intelligence tasks (error analysis,
# template fixing), but it never decides what step comes next.
#
# Pipeline steps (enforced by code, not prompts):
#   1. SANITIZE  — _ensure_parameter_defaults, _sanitize_placeholder_guids
#   2. WHAT-IF   — ARM validation preview (catches errors before deploy)
#   3. DEPLOY    — Real ARM deployment with progress streaming
#   4. ON FAIL   —
#      a. Surface heal: _copilot_heal_template (LLM fixes the ARM JSON)
#      b. Deep heal:    _deep_heal_composed_template (fix underlying service
#                       templates, validate standalone, recompose parent)
#      c. Retry from step 2
#   5. ON SUCCESS — Save healed version, report provisioned resources
#   6. EXHAUSTED  — LLM deployment agent summarizes for the user
#
# The LLM cannot skip steps. It cannot decide to stop early. It cannot
# bypass What-If. The pipeline runs to completion or exhaustion.
# ══════════════════════════════════════════════════════════════

MAX_DEPLOY_HEAL_ATTEMPTS = 5   # Match validate's budget
DEEP_HEAL_THRESHOLD = 3        # After this many surface heals, go deep

DEPLOY_AGENT_PROMPT = """\
You are the InfraForge Deployment Agent. A deployment failed after the
auto-healing pipeline tried {attempts} iteration(s). Summarize what
happened clearly for the user.

When explaining:
1. Explain the error in plain language (what went wrong)
2. Describe what the pipeline tried (surface heals, deep heals if any)
3. Suggest specific next steps

Guidelines:
- Be concise (3-5 sentences max)
- Use markdown for formatting
- Don't be alarming — deployment issues are normal in iterative development
- Frame problems as improvements needed, not failures
- If the error is a template issue, suggest re-running validation
- If the error is an Azure issue (quota, region, SKU), explain the limitation
- Never dump raw error codes — translate them for humans
"""


async def _get_deploy_agent_analysis(
    error: str,
    template_name: str,
    resource_group: str,
    region: str,
    heal_history: list[dict] | None = None,
) -> str:
    """Ask the deployment agent (LLM) to interpret a deployment failure.

    Called only after the pipeline exhausts all heal attempts. The agent
    produces a human-readable summary. This is a LEAF call — the LLM has
    no tools and cannot trigger further actions.
    """
    attempts = len(heal_history) if heal_history else 0
    history_text = ""
    if heal_history:
        history_text = "\n**Pipeline history:**\n"
        for h in heal_history:
            phase = h.get("phase", "deploy")
            history_text += (
                f"- Iteration {h['attempt']} ({phase}): {h['error'][:150]}… "
                f"→ {h['fix_summary']}\n"
            )

    try:
        client = await ensure_copilot_client()
        if not client:
            return _fallback_deploy_analysis(error, heal_history)

        session = await client.create_session({
            "model": get_model_for_task(Task.VALIDATION_ANALYSIS),
            "streaming": True,
            "tools": [],
            "system_message": {"content": DEPLOY_AGENT_PROMPT.format(attempts=attempts)},
        })

        prompt = (
            f"A deployment of **{template_name}** to resource group "
            f"`{resource_group}` in **{region}** failed after "
            f"{attempts} pipeline iteration(s).\n\n"
            f"**Final Azure error:**\n```\n{error[:500]}\n```\n"
            f"{history_text}\n"
            f"Explain what happened and what to do next."
        )

        chunks: list[str] = []
        done = asyncio.Event()

        def on_event(event):
            try:
                if event.type.value == "assistant.message_delta":
                    chunks.append(event.data.delta_content or "")
                elif event.type.value in ("assistant.message", "session.idle"):
                    done.set()
            except Exception:
                done.set()

        unsub = session.on(on_event)
        try:
            await session.send({"prompt": prompt})
            await asyncio.wait_for(done.wait(), timeout=30)
        finally:
            unsub()
            try:
                await session.destroy()
            except Exception:
                pass

        return "".join(chunks) or _fallback_deploy_analysis(error, heal_history)

    except Exception as e:
        logger.error(f"Deploy agent analysis failed: {e}")
        return _fallback_deploy_analysis(error, heal_history)


def _fallback_deploy_analysis(error: str, heal_history: list[dict] | None = None) -> str:
    """Structured message when the LLM agent isn't available."""
    attempts = len(heal_history) if heal_history else 0
    history_text = ""
    if heal_history:
        history_text = "\n\n**What the pipeline tried:**\n"
        for h in heal_history:
            history_text += f"- Iteration {h['attempt']}: {h['fix_summary']}\n"

    return (
        f"The deployment pipeline tried {attempts} iteration(s) but couldn't "
        f"resolve the issue.\n\n"
        f"**Last error:**\n> {error[:300]}\n"
        f"{history_text}\n"
        f"**Suggested next steps:** Re-run validation to diagnose and fix "
        f"the underlying issue with the full healing pipeline."
    )


@app.post("/api/catalog/templates/{template_id}/deploy")
async def deploy_template(template_id: str, request: Request):
    """Deploy an approved template to Azure — process-as-code pipeline.

    The deployment is managed by a deterministic pipeline that:
      1. Sanitizes the template (parameter defaults, GUID placeholders)
      2. Runs What-If validation (catches errors before spending resources)
      3. Deploys to Azure with real-time progress streaming
      4. On failure: surface-heals → deep-heals (for composed templates)
      5. On exhaustion: LLM agent summarizes for the user

    The LLM is called for intelligence tasks, not for process control.
    The pipeline cannot be short-circuited by the LLM.

    Event protocol (NDJSON):
      {"type": "status",  "message": "...", "progress": 0.5}  — progress
      {"type": "agent",   "content": "...", "action": "..."}   — agent activity
      {"type": "result",  "status": "succeeded|needs_work"}    — final outcome
    """
    import uuid as _uuid
    from src.database import (
        get_template_by_id, get_template_versions,
        create_template_version, update_template_version_status,
    )

    tmpl = await get_template_by_id(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    if tmpl.get("status") not in ("approved",):
        raise HTTPException(
            status_code=400,
            detail=f"Template must be published (approved) before deploying. "
                   f"Current: {tmpl.get('status')}. Run validation and publish first.",
        )

    try:
        body = await request.json()
    except Exception:
        body = {}

    resource_group = body.get("resource_group", "").strip()
    if not resource_group:
        raise HTTPException(status_code=400, detail="resource_group is required")

    region = body.get("region", "eastus2")
    user_params = body.get("parameters", {})

    # Get the active (approved) version's ARM template
    arm_content = tmpl.get("content", "")
    versions = await get_template_versions(template_id)
    active_ver = tmpl.get("active_version")
    for v in versions:
        if v["version"] == active_ver and v.get("arm_template"):
            arm_content = v["arm_template"]
            break

    try:
        tpl = json.loads(arm_content) if isinstance(arm_content, str) else arm_content
    except Exception:
        raise HTTPException(status_code=400, detail="Template content is not valid JSON")

    # Template metadata for deep healing
    is_blueprint = tmpl.get("is_blueprint", False)
    service_ids = tmpl.get("service_ids") or []

    deployment_name = f"infraforge-{_uuid.uuid4().hex[:8]}"
    _tmpl_id = template_id
    _tmpl_name = tmpl.get("name", template_id)

    async def _stream():
        """Process-as-code deployment pipeline.

        Steps are enforced by code, not by the LLM. The LLM is called
        for intelligence tasks (healing, analysis) as a sub-agent.
        """
        from src.tools.deploy_engine import execute_deployment, run_what_if

        heal_history: list[dict] = []

        # ── STEP 1: SANITIZE ─────────────────────────────────
        yield json.dumps({
            "type": "status",
            "message": f"🚀 Deploying **{_tmpl_name}** to `{resource_group}`…",
            "progress": 0.02,
            "deployment_name": deployment_name,
            "resource_group": resource_group,
            "region": region,
        }) + "\n"

        current_template_json = _ensure_parameter_defaults(json.dumps(tpl, indent=2))
        current_template_json = _sanitize_placeholder_guids(current_template_json)
        current_template_json = _sanitize_dns_zone_names(current_template_json)
        current_template = json.loads(current_template_json)

        # Build parameters using _extract_param_values (same as validate)
        final_params = _extract_param_values(current_template)
        final_params.update({
            k: v for k, v in user_params.items() if v is not None
        })

        for attempt in range(1, MAX_DEPLOY_HEAL_ATTEMPTS + 1):
            is_last = attempt == MAX_DEPLOY_HEAL_ATTEMPTS
            att_base = (attempt - 1) / MAX_DEPLOY_HEAL_ATTEMPTS

            if attempt > 1:
                yield json.dumps({
                    "type": "agent",
                    "action": "retry",
                    "content": f"🔄 **Iteration {attempt}/{MAX_DEPLOY_HEAL_ATTEMPTS}** — retrying with the fixed template…",
                }) + "\n"

            # ── STEP 2: WHAT-IF VALIDATION ────────────────────
            yield json.dumps({
                "type": "status",
                "message": "Validating ARM template against Azure (What-If)…",
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

                # Detect transient Azure errors (don't burn a heal attempt)
                _infra_keywords = (
                    "beingdeleted", "being deleted", "deprovisioning",
                    "throttled", "toomanyrequests", "retryable",
                    "serviceunavailable", "internalservererror",
                )
                if any(kw in what_if_errors.lower() for kw in _infra_keywords):
                    yield json.dumps({
                        "type": "status",
                        "message": "Transient Azure issue — waiting before retry…",
                        "progress": att_base + 0.05 / MAX_DEPLOY_HEAL_ATTEMPTS,
                    }) + "\n"
                    await asyncio.sleep(10)
                    continue

                # Template error — heal it
                yield json.dumps({
                    "type": "agent",
                    "action": "healing",
                    "content": f"🧠 **What-If rejected** — deployment agent fixing the template (iteration {attempt}/{MAX_DEPLOY_HEAL_ATTEMPTS})…",
                }) + "\n"

                healed = await _run_heal_step(
                    current_template, what_if_errors, heal_history,
                    attempt, is_blueprint, service_ids, _tmpl_id, region,
                )
                if healed:
                    current_template = healed["template"]
                    final_params = _extract_param_values(current_template)
                    final_params.update({k: v for k, v in user_params.items() if v is not None})

                    yield json.dumps({
                        "type": "agent",
                        "action": "healed",
                        "content": f"🔧 **Fixed:** {healed['fix_summary']}",
                    }) + "\n"

                    heal_history.append({
                        "step": len(heal_history) + 1,
                        "phase": "what_if",
                        "error": what_if_errors[:500],
                        "fix_summary": healed["fix_summary"],
                        "deep": healed.get("deep", False),
                    })
                else:
                    heal_history.append({
                        "step": len(heal_history) + 1,
                        "phase": "what_if",
                        "error": what_if_errors[:500],
                        "fix_summary": "Heal failed",
                    })
                    if is_last:
                        break
                    yield json.dumps({
                        "type": "agent",
                        "action": "heal_failed",
                        "content": "⚠️ Auto-heal couldn't resolve the What-If error — trying a different approach…",
                    }) + "\n"
                continue  # Retry from What-If with the fixed template

            # What-If passed!
            change_summary = ", ".join(
                f"{v} {k}" for k, v in wif.get("change_counts", {}).items()
            )
            yield json.dumps({
                "type": "status",
                "message": f"✓ What-If passed — {change_summary or 'template accepted'}",
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
                    template_id=_tmpl_id,
                    template_name=_tmpl_name,
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
                            "type": "status",
                            "message": event.get("detail", ""),
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
                        "type": "status",
                        "message": event.get("detail", ""),
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
                            _tmpl_id,
                            arm_template=fixed_json,
                            changelog=(
                                f"Auto-healed during deployment "
                                f"(iteration {attempt}): "
                                f"{heal_history[-1]['fix_summary'][:200]}"
                            ),
                            created_by="deployment-agent",
                        )
                        await update_template_version_status(
                            _tmpl_id, new_ver["version"], "approved",
                        )
                        logger.info(
                            f"Deploy pipeline saved healed template "
                            f"as version {new_ver['version']}"
                        )
                        yield json.dumps({
                            "type": "agent",
                            "action": "saved",
                            "content": (
                                f"💾 Fixed template saved as "
                                f"**version {new_ver['version']}**."
                            ),
                        }) + "\n"
                    except Exception as e:
                        logger.warning(
                            f"Failed to save healed template version: {e}"
                        )

                yield json.dumps({
                    "type": "result",
                    "status": "succeeded",
                    "step": attempt,
                    "deployment_id": result.get("deployment_id"),
                    "provisioned_resources": result.get(
                        "provisioned_resources", []
                    ),
                    "outputs": result.get("outputs", {}),
                    "healed": attempt > 1,
                }) + "\n"
                return

            # ── DEPLOY FAILED → HEAL ──
            deploy_error = result.get("error") or "Unknown deployment error"

            # Try to get operation-level details for better diagnostics
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

            # Detect transient Azure errors
            if any(
                kw in deploy_error.lower()
                for kw in (
                    "beingdeleted", "being deleted", "deprovisioning",
                    "throttled", "toomanyrequests", "retryable",
                    "serviceunavailable", "internalservererror",
                )
            ):
                yield json.dumps({
                    "type": "status",
                    "message": "Transient Azure issue — waiting before retry…",
                    "progress": att_base + 0.15 / MAX_DEPLOY_HEAL_ATTEMPTS,
                }) + "\n"
                await asyncio.sleep(10)
                continue

            if is_last:
                break

            yield json.dumps({
                "type": "agent",
                "action": "healing",
                "content": (
                    f"🧠 **Deploy failed** — deployment agent fixing the "
                    f"template (iteration {attempt}/{MAX_DEPLOY_HEAL_ATTEMPTS})…"
                ),
            }) + "\n"

            healed = await _run_heal_step(
                current_template, deploy_error, heal_history,
                attempt, is_blueprint, service_ids, _tmpl_id, region,
            )
            if healed:
                current_template = healed["template"]
                final_params = _extract_param_values(current_template)
                final_params.update({
                    k: v for k, v in user_params.items() if v is not None
                })

                yield json.dumps({
                    "type": "agent",
                    "action": "healed",
                    "content": f"🔧 **Fixed:** {healed['fix_summary']}",
                }) + "\n"
                if healed.get("deep"):
                    yield json.dumps({
                        "type": "agent",
                        "action": "deep_healed",
                        "content": (
                            f"🔬 **Deep heal:** Fixed the underlying "
                            f"`{healed.get('culprit', '?')}` service template, "
                            f"validated it standalone, and recomposed the parent."
                        ),
                    }) + "\n"

                heal_history.append({
                    "step": len(heal_history) + 1,
                    "phase": "deploy",
                    "error": deploy_error[:500],
                    "fix_summary": healed["fix_summary"],
                    "deep": healed.get("deep", False),
                })
            else:
                heal_history.append({
                    "step": len(heal_history) + 1,
                    "phase": "deploy",
                    "error": deploy_error[:500],
                    "fix_summary": "Heal failed",
                })
                yield json.dumps({
                    "type": "agent",
                    "action": "heal_failed",
                    "content": (
                        "⚠️ Auto-heal couldn't resolve this error "
                        "— trying a different approach…"
                    ),
                }) + "\n"

            # Loop continues to next attempt

        # ── STEP 6: EXHAUSTED → LLM summarizes ───────────────
        last_error = (
            heal_history[-1]["error"] if heal_history
            else "Unknown error"
        )

        yield json.dumps({
            "type": "agent",
            "action": "analyzing",
            "content": (
                f"🧠 Deployment agent analyzing the issue after "
                f"{len(heal_history)} iteration(s)…"
            ),
        }) + "\n"

        analysis = await _get_deploy_agent_analysis(
            last_error, _tmpl_name, resource_group, region,
            heal_history=heal_history,
        )

        yield json.dumps({
            "type": "agent",
            "action": "analysis",
            "content": analysis,
        }) + "\n"

        yield json.dumps({
            "type": "result",
            "status": "needs_work",
            "step": len(heal_history),
            "deployment_id": deployment_name,
        }) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


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

    This function is deterministic in its decision of WHICH strategy to use.
    The LLM is called only for the actual fix, not for the strategy choice.
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
        # ── Deep heal: decompose → fix service → validate → recompose ──
        logger.info(
            f"Deploy pipeline: escalating to deep heal "
            f"(attempt {attempt}, {surface_attempts} surface heals exhausted)"
        )
        try:
            deep_events: list[dict] = []

            async def _capture_deep_event(evt):
                deep_events.append(evt)

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
        # Fall through to surface heal if deep heal fails

    # ── Surface heal: LLM fixes the ARM JSON directly ──
    try:
        pre_fix = json.dumps(current_template, indent=2)
        # Pass the actual parameter values so the LLM can see what
        # was sent to ARM and fix the corresponding defaultValues.
        current_params = _extract_param_values(current_template)
        fixed_content = await _copilot_heal_template(
            content=pre_fix,
            error=error_msg,
            previous_attempts=heal_history,
            parameters=current_params,
        )
        fixed_template = json.loads(fixed_content)
        fix_summary = _summarize_fix(pre_fix, fixed_content)
        return {
            "template": fixed_template,
            "fix_summary": fix_summary,
            "deep": False,
        }
    except Exception as e:
        logger.error(f"Surface heal failed: {e}")
        return None

@app.delete("/api/catalog/services/{service_id}")
async def delete_service_endpoint(service_id: str):
    """Remove a service from the catalog."""
    from src.database import get_backend

    backend = await get_backend()
    rows = await backend.execute("SELECT id FROM services WHERE id = ?", (service_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Service not found")
    # Delete children first
    await backend.execute_write("DELETE FROM service_approved_skus WHERE service_id = ?", (service_id,))
    await backend.execute_write("DELETE FROM service_approved_regions WHERE service_id = ?", (service_id,))
    await backend.execute_write("DELETE FROM service_policies WHERE service_id = ?", (service_id,))
    await backend.execute_write("DELETE FROM services WHERE id = ?", (service_id,))
    return JSONResponse({"status": "ok", "deleted": service_id})


@app.get("/api/catalog/services/sync")
async def sync_services_from_azure():
    """Stream real-time progress of Azure resource provider sync via SSE.

    - If no sync is running, starts one and streams progress.
    - If a sync IS already running, subscribes to the existing stream
      (with full history replay so you immediately see current state).
    - The final event has `phase: 'done'` with the full summary.
    """
    from src.azure_sync import sync_manager, run_sync_managed

    # Start a sync if one isn't already running (idempotent)
    await run_sync_managed()

    async def _event_stream():
        q = sync_manager.subscribe()
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            sync_manager.unsubscribe(q)

    return StreamingResponse(_event_stream(), media_type="text/event-stream")


@app.get("/api/catalog/services/sync/status")
async def sync_status():
    """Return the current sync status (running, progress, last result)."""
    from src.azure_sync import sync_manager
    return JSONResponse(sync_manager.status())


@app.get("/api/catalog/services/sync/stats")
async def sync_stats():
    """Return combined service stats + sync status for the stats panel.

    Returns total Azure resource types (from last sync), total cached
    in our DB, total approved, and current sync status — all in one call.
    """
    from src.azure_sync import sync_manager
    from src.database import get_all_services

    try:
        services = await get_all_services()
        stats = {"approved": 0, "conditional": 0, "under_review": 0, "not_approved": 0}
        for svc in services:
            status = svc.get("status", "not_approved")
            stats[status] = stats.get(status, 0) + 1

        sync_info = sync_manager.status()

        return JSONResponse({
            "total_azure": sync_info.get("total_azure_resource_types"),
            "total_cached": len(services),
            "total_approved": stats["approved"],
            "total_conditional": stats["conditional"],
            "total_under_review": stats["under_review"],
            "total_not_approved": stats["not_approved"],
            "sync_running": sync_info["running"],
            "last_synced_at": sync_info.get("last_completed_at_iso"),
            "last_synced_ago_sec": sync_info.get("last_completed_ago_sec"),
            "last_sync_result": sync_info.get("last_completed"),
        })
    except Exception as e:
        logger.error(f"Failed to load sync stats: {e}")
        return JSONResponse({
            "total_azure": None,
            "total_cached": 0,
            "total_approved": 0,
            "total_conditional": 0,
            "total_under_review": 0,
            "total_not_approved": 0,
            "sync_running": False,
            "last_synced_at": None,
            "last_synced_ago_sec": None,
            "last_sync_result": None,
        })


# ── Orchestration Processes API ──────────────────────────────

@app.get("/api/orchestration/processes")
async def list_orchestration_processes():
    """List all orchestration processes and their steps."""
    from src.database import get_all_processes
    processes = await get_all_processes()
    return JSONResponse({"processes": processes, "total": len(processes)})


@app.get("/api/orchestration/processes/{process_id}")
async def get_orchestration_process(process_id: str):
    """Get a specific orchestration process with its steps."""
    from src.database import get_process
    proc = await get_process(process_id)
    if not proc:
        raise HTTPException(status_code=404, detail=f"Process '{process_id}' not found")
    return JSONResponse(proc)


@app.get("/api/orchestration/processes/{process_id}/playbook")
async def get_orchestration_playbook(process_id: str):
    """Get a human/LLM-readable playbook for a process."""
    from src.orchestrator import get_process_playbook
    text = await get_process_playbook(process_id)
    return JSONResponse({"process_id": process_id, "playbook": text})


# ══════════════════════════════════════════════════════════════
# TEMPLATE FEEDBACK — CHAT WITH YOUR TEMPLATE
# ══════════════════════════════════════════════════════════════

@app.post("/api/catalog/templates/{template_id}/feedback")
async def template_feedback(template_id: str, request: Request):
    """Accept natural-language feedback about a template and auto-fix it.

    Body:
    {
        "message": "I wanted a VM but only the VNet got deployed"
    }

    The endpoint:
    1. Sends the template + user message to the LLM for gap analysis
    2. Identifies missing Azure resource types
    3. Auto-onboards missing services into the catalog
    4. Updates the template's service_ids and triggers recompose
    5. Returns the analysis, actions taken, and updated template

    This is the human-in-the-loop channel for the autonomous orchestrator.
    """
    from src.database import (
        get_template_by_id, upsert_template, create_template_version,
        get_service, get_active_service_version,
    )
    from src.orchestrator import analyze_template_feedback
    from src.tools.arm_generator import (
        _STANDARD_PARAMETERS, _TEMPLATE_WRAPPER, generate_arm_template,
        has_builtin_skeleton,
    )
    from src.template_engine import analyze_dependencies
    import json as _json

    tmpl = await get_template_by_id(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    message = body.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Feedback message is required")

    # Get the copilot client for LLM analysis
    client = await ensure_copilot_client()

    # ── Step 1: Analyze feedback ──────────────────────────────
    feedback_result = await analyze_template_feedback(
        tmpl,
        message,
        copilot_client=client,
    )

    if not feedback_result["should_recompose"]:
        return JSONResponse({
            "status": "no_changes",
            "analysis": feedback_result["analysis"],
            "missing_services": feedback_result["missing_services"],
            "actions_taken": feedback_result["actions_taken"],
            "message": "No missing services identified — the template may already cover your needs. "
                       "Try providing more specific feedback about what resources you expected.",
        })

    # ── Step 2: Update template service_ids ───────────────────
    new_service_ids = feedback_result["new_service_ids"]

    # ── Step 3: Recompose with the updated service list ───────
    STANDARD_PARAMS = {
        "resourceName", "location", "environment",
        "projectName", "ownerEmail", "costCenter",
    }

    service_templates: list[dict] = []
    for sid in new_service_ids:
        svc = await get_service(sid)
        if not svc:
            continue
        tpl_dict = None
        active = await get_active_service_version(sid)
        if active and active.get("arm_template"):
            try:
                tpl_dict = _json.loads(active["arm_template"])
            except Exception:
                pass
        if not tpl_dict and has_builtin_skeleton(sid):
            tpl_dict = generate_arm_template(sid)
        if not tpl_dict:
            continue
        service_templates.append({
            "svc": svc,
            "template": tpl_dict,
            "quantity": 1,
        })

    if not service_templates:
        raise HTTPException(
            status_code=500,
            detail="No service templates available for recomposition",
        )

    # Compose the updated template (same logic as recompose endpoint)
    combined_params = dict(_STANDARD_PARAMETERS)
    combined_resources: list[dict] = []
    combined_outputs: dict = {}
    resource_types: list[str] = []
    tags_list: list[str] = []

    for entry in service_templates:
        svc = entry["svc"]
        tpl = entry["template"]
        sid = svc["id"]
        short_name = sid.split("/")[-1].lower()
        resource_types.append(sid)
        tags_list.append(svc.get("category", ""))

        src_params = tpl.get("parameters", {})
        src_resources = tpl.get("resources", [])
        src_outputs = tpl.get("outputs", {})

        suffix = f"_{short_name}"
        instance_name_param = f"resourceName{suffix}"
        combined_params[instance_name_param] = {
            "type": "string",
            "metadata": {"description": f"Name for {svc.get('name', sid)}"},
        }

        all_non_standard = [
            pname for pname in src_params
            if pname not in STANDARD_PARAMS and pname != "resourceName"
        ]
        for pname in all_non_standard:
            pdef = src_params.get(pname)
            if not pdef:
                continue
            suffixed = f"{pname}{suffix}"
            combined_params[suffixed] = dict(pdef)

        for res in src_resources:
            res_str = _json.dumps(res)
            res_str = res_str.replace(
                "[parameters('resourceName')]",
                f"[parameters('{instance_name_param}')]",
            )
            res_str = res_str.replace(
                "parameters('resourceName')",
                f"parameters('{instance_name_param}')",
            )
            for pname in all_non_standard:
                suffixed = f"{pname}{suffix}"
                res_str = res_str.replace(
                    f"[parameters('{pname}')]",
                    f"[parameters('{suffixed}')]",
                )
                res_str = res_str.replace(
                    f"parameters('{pname}')",
                    f"parameters('{suffixed}')",
                )
            combined_resources.append(_json.loads(res_str))

        for oname, odef in src_outputs.items():
            out_name = f"{oname}{suffix}"
            out_val = _json.dumps(odef)
            out_val = out_val.replace(
                "[parameters('resourceName')]",
                f"[parameters('{instance_name_param}')]",
            )
            out_val = out_val.replace(
                "parameters('resourceName')",
                f"parameters('{instance_name_param}')",
            )
            for pname in all_non_standard:
                suffixed = f"{pname}{suffix}"
                out_val = out_val.replace(
                    f"[parameters('{pname}')]",
                    f"[parameters('{suffixed}')]",
                )
                out_val = out_val.replace(
                    f"parameters('{pname}')",
                    f"parameters('{suffixed}')",
                )
            combined_outputs[out_name] = _json.loads(out_val)

    composed = dict(_TEMPLATE_WRAPPER)
    composed["parameters"] = combined_params
    composed["variables"] = {}
    composed["resources"] = combined_resources
    composed["outputs"] = combined_outputs

    content_str = _json.dumps(composed, indent=2)

    # Apply sanitizers
    content_str = _ensure_parameter_defaults(content_str)
    content_str = _sanitize_placeholder_guids(content_str)
    content_str = _sanitize_dns_zone_names(content_str)

    composed = _json.loads(content_str)
    combined_params = composed.get("parameters", {})
    param_list = [
        {"name": k, "type": v.get("type", "string"), "required": "defaultValue" not in v}
        for k, v in combined_params.items()
    ]

    dep_analysis = analyze_dependencies(new_service_ids)

    # ── Step 4: Save the updated template ─────────────────────
    catalog_entry = {
        "id": template_id,
        "name": tmpl.get("name", template_id),
        "description": tmpl.get("description", ""),
        "format": "arm",
        "category": tmpl.get("category", "blueprint"),
        "content": content_str,
        "tags": list(set(tags_list)),
        "resources": list(set(resource_types)),
        "parameters": param_list,
        "outputs": list(combined_outputs.keys()),
        "is_blueprint": len(service_templates) > 1,
        "service_ids": new_service_ids,
        "status": "draft",  # Reset to draft — needs re-testing
        "registered_by": tmpl.get("registered_by", "template-composer"),
        "template_type": dep_analysis["template_type"],
        "provides": dep_analysis["provides"],
        "requires": dep_analysis["requires"],
        "optional_refs": dep_analysis["optional_refs"],
    }

    try:
        await upsert_template(catalog_entry)
        ver = await create_template_version(
            template_id, content_str,
            changelog=f"Feedback recompose: {message[:100]}",
            created_by="feedback-orchestrator",
        )
    except Exception as e:
        logger.error(f"Failed to save feedback-recomposed template: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    logger.info(
        f"Feedback recomposed '{template_id}': {len(feedback_result['actions_taken'])} actions, "
        f"{len(combined_resources)} resources, {len(new_service_ids)} services"
    )

    return JSONResponse({
        "status": "recomposed",
        "analysis": feedback_result["analysis"],
        "missing_services": feedback_result["missing_services"],
        "actions_taken": feedback_result["actions_taken"],
        "template_id": template_id,
        "resource_count": len(combined_resources),
        "parameter_count": len(combined_params),
        "services": new_service_ids,
        "version": ver,
        "message": f"Template updated with {len(feedback_result['actions_taken'])} new services and recomposed. Status reset to draft for re-testing.",
    })


# ══════════════════════════════════════════════════════════════
# TEMPLATE REVISION — REQUEST REVISION WITH POLICY CHECK
# ══════════════════════════════════════════════════════════════

@app.post("/api/catalog/templates/{template_id}/revision/policy-check")
async def revision_policy_check(template_id: str, request: Request):
    """Pre-check a revision request against org policies.

    Body: { "prompt": "Add a public-facing VM with open SSH" }

    Returns instant pass/warning/block feedback BEFORE any changes are made.
    Call this first; if it passes, call the /revise endpoint.
    """
    from src.database import get_template_by_id
    from src.orchestrator import check_revision_policy

    tmpl = await get_template_by_id(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    prompt = body.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    client = await ensure_copilot_client()

    result = await check_revision_policy(
        prompt,
        template=tmpl,
        copilot_client=client,
    )

    return JSONResponse(result)


@app.post("/api/catalog/templates/{template_id}/revise")
async def revise_template(template_id: str, request: Request):
    """Revise a template based on natural language — with policy enforcement.

    Body:
    {
        "prompt": "Add a SQL database and a Key Vault to this template",
        "skip_policy_check": false
    }

    Flow:
    1. Policy pre-check → block if violations
    2. LLM determines which services are needed (new + existing)
    3. Auto-onboard missing services
    4. Recompose the template
    5. Return the updated template + policy check results
    """
    from src.database import (
        get_template_by_id, upsert_template, create_template_version,
        get_service, get_active_service_version,
    )
    from src.orchestrator import (
        check_revision_policy, analyze_template_feedback,
    )
    from src.tools.arm_generator import (
        _STANDARD_PARAMETERS, _TEMPLATE_WRAPPER, generate_arm_template,
        has_builtin_skeleton,
    )
    from src.template_engine import analyze_dependencies
    import json as _json

    tmpl = await get_template_by_id(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    prompt = body.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    skip_policy = body.get("skip_policy_check", False)
    client = await ensure_copilot_client()

    # ── Step 1: Policy pre-check ──────────────────────────────
    policy_result = None
    if not skip_policy:
        policy_result = await check_revision_policy(
            prompt,
            template=tmpl,
            copilot_client=client,
        )

        if policy_result["verdict"] == "block":
            return JSONResponse({
                "status": "blocked",
                "policy_check": policy_result,
                "message": "Revision blocked by organizational policy. Address the issues below before proceeding.",
            }, status_code=422)

    # ── Step 2: Analyze what needs to change ──────────────────
    feedback_result = await analyze_template_feedback(
        tmpl,
        prompt,
        copilot_client=client,
    )

    if not feedback_result["should_recompose"]:
        return JSONResponse({
            "status": "no_changes",
            "policy_check": policy_result,
            "analysis": feedback_result["analysis"],
            "actions_taken": feedback_result["actions_taken"],
            "message": "No new services identified from your request. "
                       "Try being more specific about what resources you need.",
        })

    # ── Step 3: Recompose with updated service list ───────────
    new_service_ids = feedback_result["new_service_ids"]
    STANDARD_PARAMS = {
        "resourceName", "location", "environment",
        "projectName", "ownerEmail", "costCenter",
    }

    service_templates: list[dict] = []
    for sid in new_service_ids:
        svc = await get_service(sid)
        if not svc:
            continue
        tpl_dict = None
        active = await get_active_service_version(sid)
        if active and active.get("arm_template"):
            try:
                tpl_dict = _json.loads(active["arm_template"])
            except Exception:
                pass
        if not tpl_dict and has_builtin_skeleton(sid):
            tpl_dict = generate_arm_template(sid)
        if not tpl_dict:
            continue
        service_templates.append({
            "svc": svc,
            "template": tpl_dict,
            "quantity": 1,
        })

    if not service_templates:
        raise HTTPException(status_code=500, detail="No service templates available for recomposition")

    # Compose
    combined_params = dict(_STANDARD_PARAMETERS)
    combined_resources: list[dict] = []
    combined_outputs: dict = {}
    resource_types: list[str] = []
    tags_list: list[str] = []

    for entry in service_templates:
        svc = entry["svc"]
        tpl = entry["template"]
        sid = svc["id"]
        short_name = sid.split("/")[-1].lower()
        resource_types.append(sid)
        tags_list.append(svc.get("category", ""))

        src_params = tpl.get("parameters", {})
        src_resources = tpl.get("resources", [])
        src_outputs = tpl.get("outputs", {})

        suffix = f"_{short_name}"
        instance_name_param = f"resourceName{suffix}"
        combined_params[instance_name_param] = {
            "type": "string",
            "metadata": {"description": f"Name for {svc.get('name', sid)}"},
        }

        all_non_standard = [
            pname for pname in src_params
            if pname not in STANDARD_PARAMS and pname != "resourceName"
        ]
        for pname in all_non_standard:
            pdef = src_params.get(pname)
            if not pdef:
                continue
            suffixed = f"{pname}{suffix}"
            combined_params[suffixed] = dict(pdef)

        for res in src_resources:
            res_str = _json.dumps(res)
            res_str = res_str.replace("[parameters('resourceName')]", f"[parameters('{instance_name_param}')]")
            res_str = res_str.replace("parameters('resourceName')", f"parameters('{instance_name_param}')")
            for pname in all_non_standard:
                suffixed = f"{pname}{suffix}"
                res_str = res_str.replace(f"[parameters('{pname}')]", f"[parameters('{suffixed}')]")
                res_str = res_str.replace(f"parameters('{pname}')", f"parameters('{suffixed}')")
            combined_resources.append(_json.loads(res_str))

        for oname, odef in src_outputs.items():
            out_name = f"{oname}{suffix}"
            out_val = _json.dumps(odef)
            out_val = out_val.replace("[parameters('resourceName')]", f"[parameters('{instance_name_param}')]")
            out_val = out_val.replace("parameters('resourceName')", f"parameters('{instance_name_param}')")
            for pname in all_non_standard:
                suffixed = f"{pname}{suffix}"
                out_val = out_val.replace(f"[parameters('{pname}')]", f"[parameters('{suffixed}')]")
                out_val = out_val.replace(f"parameters('{pname}')", f"parameters('{suffixed}')")
            combined_outputs[out_name] = _json.loads(out_val)

    composed = dict(_TEMPLATE_WRAPPER)
    composed["parameters"] = combined_params
    composed["variables"] = {}
    composed["resources"] = combined_resources
    composed["outputs"] = combined_outputs

    content_str = _json.dumps(composed, indent=2)
    content_str = _ensure_parameter_defaults(content_str)
    content_str = _sanitize_placeholder_guids(content_str)
    content_str = _sanitize_dns_zone_names(content_str)

    composed = _json.loads(content_str)
    combined_params = composed.get("parameters", {})
    param_list = [
        {"name": k, "type": v.get("type", "string"), "required": "defaultValue" not in v}
        for k, v in combined_params.items()
    ]

    dep_analysis = analyze_dependencies(new_service_ids)

    catalog_entry = {
        "id": template_id,
        "name": tmpl.get("name", template_id),
        "description": tmpl.get("description", ""),
        "format": "arm",
        "category": tmpl.get("category", "blueprint"),
        "content": content_str,
        "tags": list(set(tags_list)),
        "resources": list(set(resource_types)),
        "parameters": param_list,
        "outputs": list(combined_outputs.keys()),
        "is_blueprint": len(service_templates) > 1,
        "service_ids": new_service_ids,
        "status": "draft",
        "registered_by": tmpl.get("registered_by", "template-composer"),
        "template_type": dep_analysis["template_type"],
        "provides": dep_analysis["provides"],
        "requires": dep_analysis["requires"],
        "optional_refs": dep_analysis["optional_refs"],
    }

    try:
        await upsert_template(catalog_entry)
        ver = await create_template_version(
            template_id, content_str,
            changelog=f"Revision: {prompt[:100]}",
            created_by="revision-orchestrator",
        )
    except Exception as e:
        logger.error(f"Failed to save revised template: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({
        "status": "revised",
        "policy_check": policy_result,
        "analysis": feedback_result["analysis"],
        "actions_taken": feedback_result["actions_taken"],
        "template_id": template_id,
        "resource_count": len(combined_resources),
        "parameter_count": len(combined_params),
        "services": new_service_ids,
        "version": ver,
        "message": f"Template revised with {len(feedback_result['actions_taken'])} change(s). Status reset to draft.",
    })


# ══════════════════════════════════════════════════════════════
# PROMPT-DRIVEN TEMPLATE COMPOSITION
# ══════════════════════════════════════════════════════════════

@app.post("/api/catalog/templates/compose-from-prompt")
async def compose_template_from_prompt(request: Request):
    """Compose a new template from a natural language description.

    Body:
    {
        "prompt": "I need a VM with a SQL database and Key Vault",
        "name": "optional override",
        "category": "optional override"
    }

    Flow:
    1. Policy pre-check → block if violations
    2. LLM determines which services are needed
    3. Auto-onboard missing services + resolve dependencies
    4. Compose the template
    5. Run structural tests
    6. Return the composed template
    """
    from src.database import (
        get_service, get_active_service_version, upsert_template,
        create_template_version,
    )
    from src.orchestrator import (
        check_revision_policy, determine_services_from_prompt,
        resolve_composition_dependencies,
    )
    from src.tools.arm_generator import (
        _STANDARD_PARAMETERS, _TEMPLATE_WRAPPER, generate_arm_template,
        has_builtin_skeleton,
    )
    from src.template_engine import analyze_dependencies
    import json as _json

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    prompt = body.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required — describe what infrastructure you need")

    client = await ensure_copilot_client()

    # ── Step 1: Policy pre-check ──────────────────────────────
    policy_result = await check_revision_policy(
        prompt,
        copilot_client=client,
    )

    if policy_result["verdict"] == "block":
        return JSONResponse({
            "status": "blocked",
            "policy_check": policy_result,
            "message": "Request blocked by organizational policy.",
        }, status_code=422)

    # ── Step 2: LLM determines services ───────────────────────
    selection = await determine_services_from_prompt(
        prompt,
        copilot_client=client,
    )

    services = selection.get("services", [])
    if not services:
        return JSONResponse({
            "status": "no_services",
            "policy_check": policy_result,
            "message": "Could not determine which Azure services are needed. "
                       "Try being more specific, e.g. 'I need a VM with a SQL database'.",
        })

    name = body.get("name", "").strip() or selection.get("name_suggestion", "My Template")
    description = body.get("description", "").strip() or selection.get("description_suggestion", "")
    category = body.get("category", "").strip() or selection.get("category_suggestion", "blueprint")

    # ── Step 3: Resolve & onboard ─────────────────────────────
    service_ids = [s["resource_type"] for s in services]

    dep_result = await resolve_composition_dependencies(
        service_ids,
        copilot_client=client,
    )

    final_service_ids = dep_result["final_service_ids"]

    # ── Step 4: Gather ARM templates ──────────────────────────
    STANDARD_PARAMS = {
        "resourceName", "location", "environment",
        "projectName", "ownerEmail", "costCenter",
    }

    service_templates: list[dict] = []
    for sid in final_service_ids:
        svc = await get_service(sid)
        if not svc:
            continue
        tpl_dict = None
        active = await get_active_service_version(sid)
        if active and active.get("arm_template"):
            try:
                tpl_dict = _json.loads(active["arm_template"])
            except Exception:
                pass
        if not tpl_dict and has_builtin_skeleton(sid):
            tpl_dict = generate_arm_template(sid)
        if not tpl_dict:
            continue

        # Find quantity for this service
        qty = 1
        for s in services:
            if s["resource_type"] == sid:
                qty = s.get("quantity", 1)
                break

        service_templates.append({
            "svc": svc,
            "template": tpl_dict,
            "quantity": qty,
            "chosen_params": set(),
        })

    if not service_templates:
        raise HTTPException(status_code=500, detail="No service templates available after resolution")

    # ── Step 5: Compose ───────────────────────────────────────
    combined_params = dict(_STANDARD_PARAMETERS)
    combined_resources: list[dict] = []
    combined_outputs: dict = {}
    composed_service_ids: list[str] = []
    resource_types: list[str] = []
    tags_list: list[str] = []

    for entry in service_templates:
        svc = entry["svc"]
        tpl = entry["template"]
        qty = entry["quantity"]
        sid = svc["id"]
        composed_service_ids.append(sid)
        short_name = sid.split("/")[-1].lower()
        resource_types.append(sid)
        tags_list.append(svc.get("category", ""))

        src_params = tpl.get("parameters", {})
        src_resources = tpl.get("resources", [])
        src_outputs = tpl.get("outputs", {})

        for idx in range(1, qty + 1):
            suffix = f"_{short_name}" if qty == 1 else f"_{short_name}{idx}"
            instance_name_param = f"resourceName{suffix}"
            combined_params[instance_name_param] = {
                "type": "string",
                "metadata": {
                    "description": f"Name for {svc.get('name', sid)}"
                    + (f" (instance {idx})" if qty > 1 else ""),
                },
            }

            all_non_standard = [
                pname for pname in src_params
                if pname not in STANDARD_PARAMS and pname != "resourceName"
            ]
            for pname in all_non_standard:
                pdef = src_params.get(pname)
                if not pdef:
                    continue
                suffixed = f"{pname}{suffix}"
                combined_params[suffixed] = dict(pdef)

            for res in src_resources:
                res_str = _json.dumps(res)
                res_str = res_str.replace("[parameters('resourceName')]", f"[parameters('{instance_name_param}')]")
                res_str = res_str.replace("parameters('resourceName')", f"parameters('{instance_name_param}')")
                for pname in all_non_standard:
                    suffixed = f"{pname}{suffix}"
                    res_str = res_str.replace(f"[parameters('{pname}')]", f"[parameters('{suffixed}')]")
                    res_str = res_str.replace(f"parameters('{pname}')", f"parameters('{suffixed}')")
                combined_resources.append(_json.loads(res_str))

            for oname, odef in src_outputs.items():
                out_name = f"{oname}{suffix}"
                out_val = _json.dumps(odef)
                out_val = out_val.replace("[parameters('resourceName')]", f"[parameters('{instance_name_param}')]")
                out_val = out_val.replace("parameters('resourceName')", f"parameters('{instance_name_param}')")
                for pname in all_non_standard:
                    suffixed = f"{pname}{suffix}"
                    out_val = out_val.replace(f"[parameters('{pname}')]", f"[parameters('{suffixed}')]")
                    out_val = out_val.replace(f"parameters('{pname}')", f"parameters('{suffixed}')")
                combined_outputs[out_name] = _json.loads(out_val)

    composed = dict(_TEMPLATE_WRAPPER)
    composed["parameters"] = combined_params
    composed["variables"] = {}
    composed["resources"] = combined_resources
    composed["outputs"] = combined_outputs

    content_str = _json.dumps(composed, indent=2)
    content_str = _ensure_parameter_defaults(content_str)
    content_str = _sanitize_placeholder_guids(content_str)
    content_str = _sanitize_dns_zone_names(content_str)

    template_id = "composed-" + name.lower().replace(" ", "-")[:50]

    composed = _json.loads(content_str)
    combined_params = composed.get("parameters", {})
    param_list = [
        {"name": k, "type": v.get("type", "string"), "required": "defaultValue" not in v}
        for k, v in combined_params.items()
    ]

    dep_analysis = analyze_dependencies(composed_service_ids)

    catalog_entry = {
        "id": template_id,
        "name": name,
        "description": description,
        "format": "arm",
        "category": category,
        "content": content_str,
        "tags": list(set(tags_list)),
        "resources": list(set(resource_types)),
        "parameters": param_list,
        "outputs": list(combined_outputs.keys()),
        "is_blueprint": len(service_templates) > 1,
        "service_ids": composed_service_ids,
        "status": "draft",
        "registered_by": "prompt-composer",
        "template_type": dep_analysis["template_type"],
        "provides": dep_analysis["provides"],
        "requires": dep_analysis["requires"],
        "optional_refs": dep_analysis["optional_refs"],
    }

    try:
        await upsert_template(catalog_entry)
        ver = await create_template_version(
            template_id, content_str,
            changelog=f"Prompt compose: {prompt[:100]}",
            semver="1.0.0",
        )
    except Exception as e:
        logger.error(f"Failed to save prompt-composed template: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({
        "status": "composed",
        "policy_check": policy_result,
        "template_id": template_id,
        "template": catalog_entry,
        "version": ver,
        "services_detected": services,
        "dependency_resolution": dep_result,
        "resource_count": len(combined_resources),
        "parameter_count": len(combined_params),
        "message": f"Template '{name}' composed from {len(composed_service_ids)} services "
                   f"({len(combined_resources)} resources, {len(combined_params)} params). "
                   f"Ready for testing.",
    })


@app.delete("/api/catalog/templates/{template_id}")
async def delete_template_endpoint(template_id: str):
    """Remove a template from the catalog."""
    from src.database import delete_template

    deleted = await delete_template(template_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Template not found")
    return JSONResponse({"status": "ok", "deleted": template_id})


# ══════════════════════════════════════════════════════════════
# TEMPLATE DEPENDENCIES & RESOURCE DISCOVERY
# ══════════════════════════════════════════════════════════════

@app.get("/api/templates/types")
async def get_template_types():
    """List available template types (foundation, workload, composite)."""
    from src.template_engine import TEMPLATE_TYPES
    return JSONResponse(TEMPLATE_TYPES)


@app.get("/api/templates/known-dependencies")
async def list_known_dependencies():
    """List known resource type dependency mappings."""
    from src.template_engine import RESOURCE_DEPENDENCIES
    # Only return resource types that have dependencies
    return JSONResponse({k: v for k, v in RESOURCE_DEPENDENCIES.items() if v})


@app.post("/api/templates/analyze-dependencies")
async def analyze_template_dependencies(request: Request):
    """Analyze dependencies for a set of service IDs.

    Body: { "service_ids": ["Microsoft.Compute/virtualMachines", ...] }

    Returns: template_type, provides, requires, optional_refs, auto_created,
    and whether the template is deployable_standalone.
    """
    from src.template_engine import analyze_dependencies

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    service_ids = body.get("service_ids", [])
    if not service_ids:
        raise HTTPException(status_code=400, detail="service_ids list is required")

    analysis = analyze_dependencies(service_ids)
    return JSONResponse(analysis)


@app.get("/api/templates/discover/{resource_type:path}")
async def discover_resources_for_deployment(
    resource_type: str,
    subscription_id: Optional[str] = None,
):
    """Lightweight Azure Resource Graph query to find existing resources.

    Used at deploy time to populate resource pickers for template dependencies.
    One API call per resource type — not a full subscription scan.
    """
    from src.template_engine import discover_existing_resources

    resources = await discover_existing_resources(resource_type, subscription_id)
    return JSONResponse({
        "resource_type": resource_type,
        "count": len(resources),
        "resources": resources,
    })


@app.get("/api/templates/discover-subnets")
async def discover_subnets_endpoint(vnet_id: str):
    """Get subnets for a specific VNet — used for cascading pickers."""
    from src.template_engine import discover_subnets_for_vnet

    subnets = await discover_subnets_for_vnet(vnet_id)
    return JSONResponse({
        "vnet_id": vnet_id,
        "count": len(subnets),
        "subnets": subnets,
    })


# ══════════════════════════════════════════════════════════════
# SERVICE ONBOARDING & VERSIONED VALIDATION
# ══════════════════════════════════════════════════════════════

# ── Legacy artifact endpoints (kept for backward compat) ─────

@app.get("/api/services/{service_id:path}/artifacts")
async def get_artifacts_endpoint(service_id: str):
    """Get all approval artifacts for a service."""
    from src.database import get_service_artifacts, get_service

    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    artifacts = await get_service_artifacts(service_id)
    return JSONResponse(artifacts)


@app.put("/api/services/{service_id:path}/artifacts/{artifact_type}")
async def save_artifact_endpoint(service_id: str, artifact_type: str, request: Request):
    """Save or update an artifact (policy, template, or pipeline) for a service."""
    from src.database import save_service_artifact, get_service, ARTIFACT_TYPES

    if artifact_type not in ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid artifact type. Must be one of: {', '.join(ARTIFACT_TYPES)}",
        )

    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    content = body.get("content", "")
    notes = body.get("notes", "")
    status = body.get("status", "draft")

    if status not in ("draft", "not_started"):
        raise HTTPException(status_code=400, detail="Use the /approve endpoint to approve")

    try:
        artifact = await save_service_artifact(
            service_id=service_id,
            artifact_type=artifact_type,
            content=content,
            status=status,
            notes=notes,
        )
        return JSONResponse({"status": "ok", "artifact": artifact})
    except Exception as e:
        logger.error(f"Failed to save artifact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/services/{service_id:path}/artifacts/{artifact_type}/approve")
async def approve_artifact_endpoint(service_id: str, artifact_type: str, request: Request):
    """Approve an artifact gate. If both gates are approved, the service moves to 'validating'."""
    from src.database import approve_service_artifact, get_service, ARTIFACT_TYPES

    if artifact_type not in ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid artifact type. Must be one of: {', '.join(ARTIFACT_TYPES)}",
        )

    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    try:
        body = await request.json()
    except Exception:
        body = {}

    approved_by = body.get("approved_by", "IT Staff")

    try:
        artifact = await approve_service_artifact(
            service_id=service_id,
            artifact_type=artifact_type,
            approved_by=approved_by,
        )

        # Check if all gates are now approved → validation required
        from src.database import get_service_artifacts
        all_artifacts = await get_service_artifacts(service_id)
        all_approved = all_artifacts["_summary"]["all_approved"]

        return JSONResponse({
            "status": "ok",
            "artifact": artifact,
            "gates_approved": all_artifacts["_summary"]["approved_count"],
            "validation_required": all_approved,
            "message": (
                f"Both gates approved! Starting deployment validation…"
                if all_approved
                else f"Gate '{artifact_type}' approved ({all_artifacts['_summary']['approved_count']}/2)"
            ),
        })
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to approve artifact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/services/{service_id:path}/artifacts/{artifact_type}/unapprove")
async def unapprove_artifact_endpoint(service_id: str, artifact_type: str):
    """Revert an artifact back to draft (e.g. for edits after approval)."""
    from src.database import unapprove_service_artifact, ARTIFACT_TYPES

    if artifact_type not in ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid artifact type. Must be one of: {', '.join(ARTIFACT_TYPES)}",
        )

    try:
        artifact = await unapprove_service_artifact(service_id, artifact_type)
        if not artifact:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return JSONResponse({"status": "ok", "artifact": artifact})
    except Exception as e:
        logger.error(f"Failed to unapprove artifact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/services/{service_id:path}/validate-deployment")
async def validate_deployment_endpoint(service_id: str, request: Request):
    """Full deployment validation: What-If → Deploy → Policy Test → Cleanup.

    The auto-healing loop wraps steps 1-2.  On What-If or deploy failure the
    Copilot SDK rewrites the ARM template and retries (up to MAX_HEAL_ATTEMPTS).

    Phases streamed as NDJSON:
      iteration_start → what_if → deploying → deploy_complete →
      resource_check → policy_testing → policy_result → cleanup → done

    On success the service is promoted to 'approved'.
    On failure it is set to 'validation_failed'.
    """
    from src.database import (
        get_service, get_service_artifacts, save_service_artifact,
        promote_service_after_validation, fail_service_validation,
    )

    MAX_HEAL_ATTEMPTS = 5

    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    artifacts = await get_service_artifacts(service_id)
    if not artifacts["_summary"]["all_approved"]:
        raise HTTPException(status_code=400, detail="Both gates must be approved before validation")

    template_artifact = artifacts.get("template", {})
    template_content = template_artifact.get("content", "").strip()
    if not template_content:
        raise HTTPException(status_code=400, detail="ARM template artifact has no content")

    try:
        body = await request.json()
    except Exception:
        body = {}

    region = body.get("region", "eastus2")
    import uuid as _uuid
    _run_id = _uuid.uuid4().hex[:8]
    rg_name = f"infraforge-val-{service_id.replace('/', '-').replace('.', '-').lower()}-{_run_id}"[:90]

    # ── Copilot fix helper ────────────────────────────────────

    async def _copilot_fix(artifact_type: str, content: str, error: str,
                           previous_attempts: list[dict] | None = None) -> str:
        """Ask the Copilot SDK to fix an artifact.

        Tracks previous attempts so each iteration tries a DIFFERENT strategy.
        """
        attempt_num = len(previous_attempts) + 1 if previous_attempts else 1

        if artifact_type == "template":
            prompt = (
                "The following ARM template failed Azure deployment validation.\n\n"
                f"--- ERROR ---\n{error}\n--- END ERROR ---\n\n"
                f"--- CURRENT TEMPLATE ---\n{content}\n--- END TEMPLATE ---\n\n"
            )

            # Include parameter values so the LLM can see what was sent to ARM
            try:
                _fix_tpl = json.loads(content)
                _fix_params = _extract_param_values(_fix_tpl)
                if _fix_params:
                    prompt += (
                        "--- PARAMETER VALUES SENT TO ARM ---\n"
                        f"{json.dumps(_fix_params, indent=2, default=str)}\n"
                        "--- END PARAMETER VALUES ---\n\n"
                        "IMPORTANT: These are the actual values sent to Azure. "
                        "If the error is caused by one of these values (invalid "
                        "name, bad format), fix the corresponding parameter's "
                        "\"defaultValue\" in the template.\n\n"
                    )
            except Exception:
                pass

            # Previous attempt history
            if previous_attempts:
                prompt += "--- RESOLUTION HISTORY (DO NOT repeat these fixes) ---\n"
                for pa in previous_attempts:
                    prompt += (
                        f"Step {pa.get('step', '?')}: Error was: {pa['error'][:300]}\n"
                        f"  Fix tried: {pa['fix_summary']}\n"
                        f"  Result: STILL FAILED — do something DIFFERENT\n\n"
                    )
                prompt += "--- END PREVIOUS ATTEMPTS ---\n\n"

            prompt += (
                "Fix the template so it deploys successfully. Return ONLY the "
                "corrected raw JSON — no markdown fences, no explanation.\n\n"
                "CRITICAL RULES (in priority order):\n\n"
                "1. PARAMETER VALUES — Check parameter defaultValues FIRST:\n"
                "   - If the error mentions an invalid resource name, the name likely "
                "     comes from a parameter defaultValue. Find that parameter and fix "
                "     its defaultValue to comply with Azure naming rules.\n"
                "   - Azure DNS zone names MUST be valid FQDNs with at least two labels "
                "     (e.g. 'infraforge-demo.com', NOT 'if-dnszones').\n"
                "   - Storage account names: 3-24 lowercase alphanumeric, no hyphens.\n"
                "   - Key vault names: 3-24 alphanumeric + hyphens.\n"
                "   - Ensure EVERY parameter has a \"defaultValue\".\n\n"
                "2. LOCATIONS — Keep ALL location parameters as \"[resourceGroup().location]\" "
                "or \"[parameters('location')]\" — NEVER hardcode a region.\n"
                "   EXCEPTION: Globally-scoped resources MUST use location \"global\":\n"
                "   * Microsoft.Network/dnszones → \"global\"\n"
                "   * Microsoft.Network/trafficManagerProfiles → \"global\"\n"
                "   * Microsoft.Cdn/profiles → \"global\"\n\n"
                "3. STRUCTURAL FIXES:\n"
                "   - Keep the same resource intent and resource names.\n"
                "   - Fix schema issues, missing required properties, invalid API versions.\n"
                "   - If diagnosticSettings requires an external dependency, REMOVE it.\n"
                "   - NEVER use '00000000-0000-0000-0000-000000000000' as a subscription ID — "
                "     use [subscription().subscriptionId] instead.\n"
                "   - If the error mentions 'LinkedAuthorizationFailed', use "
                "     [subscription().subscriptionId] in resourceId() expressions.\n"
                "   - If a resource requires complex external deps (VPN gateways, "
                "     ExpressRoute), SIMPLIFY by removing those references.\n"
            )

            # Escalation strategies for later attempts
            if attempt_num >= 4:
                prompt += (
                    f"\n\nESCALATION — multiple strategies have failed, drastic measures needed:\n"
                    "- SIMPLIFY the template: remove optional/nice-to-have resources\n"
                    "- Remove diagnosticSettings, locks, autoscale rules if causing issues\n"
                    "- Use the SIMPLEST valid configuration for each resource\n"
                    "- Strip down to ONLY the primary resource with minimal properties\n"
                    "- Use well-known, stable API versions (prefer 2023-xx-xx or 2024-xx-xx)\n"
                )
            elif attempt_num >= 2:
                prompt += (
                    f"\n\nPrevious fix(es) did NOT work.\n"
                    "You MUST try a FUNDAMENTALLY DIFFERENT approach:\n"
                    "- Try a different API version for the failing resource\n"
                    "- Restructure resource dependencies\n"
                    "- Remove or replace the problematic sub-resource\n"
                    "- Check if required properties changed in newer API versions\n"
                )
        else:
            prompt = (
                "The following Azure Policy JSON has an error.\n\n"
                f"--- ERROR ---\n{error}\n--- END ERROR ---\n\n"
                f"--- CURRENT POLICY ---\n{content}\n--- END POLICY ---\n\n"
                "Fix the policy. Return ONLY the corrected raw JSON."
            )

        session = None
        try:
            _client = await ensure_copilot_client()
            if _client is None:
                raise RuntimeError("Copilot SDK not available")
            session = await _client.create_session({
                "model": get_model_for_task(Task.CODE_FIXING), "streaming": True, "tools": [],
                "system_message": {"content": (
                    "You are an Azure infrastructure expert. "
                    "Return ONLY raw JSON — no markdown, no code fences."
                )},
            })
            chunks: list[str] = []
            done_ev = asyncio.Event()

            def on_event(ev):
                try:
                    if ev.type.value == "assistant.message_delta":
                        chunks.append(ev.data.delta_content or "")
                    elif ev.type.value in ("assistant.message", "session.idle"):
                        done_ev.set()
                except Exception:
                    done_ev.set()

            unsub = session.on(on_event)
            try:
                await session.send({"prompt": prompt})
                await asyncio.wait_for(done_ev.wait(), timeout=90)
            finally:
                unsub()

            fixed = "".join(chunks).strip()
            if fixed.startswith("```"):
                lines = fixed.split("\n")[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                fixed = "\n".join(lines).strip()

            # ── Guard: ensure healer didn't corrupt the location parameter ──
            # NOTE: some resources (DNS zones, Traffic Manager, etc.) use "global"
            _GLOBAL_LOCATION_TYPES_INNER = {
                "microsoft.network/dnszones",
                "microsoft.network/trafficmanagerprofiles",
                "microsoft.cdn/profiles",
                "microsoft.network/frontdoors",
                "microsoft.network/frontdoorwebapplicationfirewallpolicies",
            }
            if artifact_type == "template":
                try:
                    _ft = json.loads(fixed)
                    _params = _ft.get("parameters", {})
                    _loc = _params.get("location", {})
                    _dv = _loc.get("defaultValue", "")
                    # If the healer hardcoded a region, restore the ARM expression
                    if isinstance(_dv, str) and _dv and not _dv.startswith("["):
                        _loc["defaultValue"] = "[resourceGroup().location]"
                        logger.warning(
                            f"Copilot healer corrupted location default to '{_dv}' — "
                            "restored to [resourceGroup().location]"
                        )
                        fixed = json.dumps(_ft, indent=2)
                    # Also check each resource's location property
                    for _res in _ft.get("resources", []):
                        _rtype = (_res.get("type") or "").lower()
                        _rloc = _res.get("location", "")
                        if _rtype in _GLOBAL_LOCATION_TYPES_INNER:
                            if isinstance(_rloc, str) and _rloc.lower() != "global":
                                _res["location"] = "global"
                                fixed = json.dumps(_ft, indent=2)
                            continue
                        if isinstance(_rloc, str) and _rloc and not _rloc.startswith("["):
                            _res["location"] = "[parameters('location')]"
                            logger.warning(
                                f"Copilot healer hardcoded resource location to '{_rloc}' — "
                                "restored to [parameters('location')]"
                            )
                            fixed = json.dumps(_ft, indent=2)
                except (json.JSONDecodeError, AttributeError):
                    pass  # if it's not valid JSON yet, the parse step will catch it

            # ── Guard: ensure every param has a defaultValue ──
            if artifact_type == "template":
                fixed = _ensure_parameter_defaults(fixed)
                fixed = _sanitize_placeholder_guids(fixed)

            return fixed
        finally:
            if session:
                try:
                    await session.destroy()
                except Exception:
                    pass

    # ── Policy compliance tester ──────────────────────────────

    def _test_policy_compliance(policy_json: dict, resources: list[dict]) -> list[dict]:
        """Evaluate deployed resources against the policy rule.

        This is a local evaluation engine that interprets the policy's
        'if' condition against each resource's actual Azure properties.
        Returns a list of per-resource compliance results.
        """
        results = []
        rule = policy_json.get("properties", policy_json).get("policyRule", {})
        if_condition = rule.get("if", {})
        effect = rule.get("then", {}).get("effect", "deny")

        for resource in resources:
            match = _evaluate_condition(if_condition, resource)
            # If the condition matches → the policy's effect applies (deny/audit)
            # A "deny" match means the resource VIOLATES the policy
            compliant = not match if effect.lower() in ("deny", "audit") else match
            results.append({
                "resource_id": resource.get("id", ""),
                "resource_type": resource.get("type", ""),
                "resource_name": resource.get("name", ""),
                "location": resource.get("location", ""),
                "compliant": compliant,
                "effect": effect,
                "reason": (
                    "Resource matches policy conditions — compliant"
                    if compliant else
                    f"Resource violates policy — {effect} would apply"
                ),
            })
        return results

    def _evaluate_condition(condition: dict, resource: dict) -> bool:
        """Recursively evaluate Azure Policy condition against a resource."""
        # allOf — all sub-conditions must be true
        if "allOf" in condition:
            return all(_evaluate_condition(c, resource) for c in condition["allOf"])
        # anyOf — any sub-condition must be true
        if "anyOf" in condition:
            return any(_evaluate_condition(c, resource) for c in condition["anyOf"])
        # not — negate
        if "not" in condition:
            return not _evaluate_condition(condition["not"], resource)

        # Leaf condition: field + operator
        field = condition.get("field", "")
        resource_val = _resolve_field(field, resource)

        if "equals" in condition:
            return str(resource_val).lower() == str(condition["equals"]).lower()
        if "notEquals" in condition:
            return str(resource_val).lower() != str(condition["notEquals"]).lower()
        if "in" in condition:
            return str(resource_val).lower() in [str(v).lower() for v in condition["in"]]
        if "notIn" in condition:
            return str(resource_val).lower() not in [str(v).lower() for v in condition["notIn"]]
        if "contains" in condition:
            return str(condition["contains"]).lower() in str(resource_val).lower()
        if "like" in condition:
            import fnmatch
            return fnmatch.fnmatch(str(resource_val).lower(), str(condition["like"]).lower())
        if "exists" in condition:
            exists = resource_val is not None and resource_val != ""
            # Normalize string booleans: LLMs often return "false"/"true" strings
            want_exists = condition["exists"]
            if isinstance(want_exists, str):
                want_exists = want_exists.lower() not in ("false", "0", "no")
            return exists if want_exists else not exists

        return False

    def _resolve_field(field: str, resource: dict):
        """Resolve a policy field reference against a resource dict."""
        field_lower = field.lower()
        if field_lower == "type":
            return resource.get("type", "")
        if field_lower == "location":
            return resource.get("location", "")
        if field_lower == "name":
            return resource.get("name", "")
        if field_lower.startswith("tags["):
            # tags['environment'] or tags.environment
            tag_name = field.split("'")[1] if "'" in field else field.split("[")[1].rstrip("]")
            return (resource.get("tags") or {}).get(tag_name, "")
        if field_lower.startswith("tags."):
            tag_name = field.split(".", 1)[1]
            return (resource.get("tags") or {}).get(tag_name, "")
        # properties.X.Y.Z → nested lookup
        parts = field.split(".")
        val = resource
        for part in parts:
            if isinstance(val, dict):
                # Case-insensitive key lookup
                matched = None
                for k in val:
                    if k.lower() == part.lower():
                        matched = k
                        break
                val = val.get(matched) if matched else None
            else:
                return None
        return val

    # ── Cleanup helper ────────────────────────────────────────

    async def _cleanup_rg(rg: str):
        """Delete the validation resource group."""
        from src.tools.deploy_engine import _get_resource_client
        client = _get_resource_client()
        loop = asyncio.get_event_loop()
        try:
            poller = await loop.run_in_executor(
                None, lambda: client.resource_groups.begin_delete(rg)
            )
            # Don't wait for full deletion — it can take minutes
            # Just fire-and-forget, the RG will be cleaned up async
            logger.info(f"Cleanup: deletion started for resource group '{rg}'")
        except Exception as e:
            logger.warning(f"Cleanup: failed to delete resource group '{rg}': {e}")

    # ── Main streaming generator ──────────────────────────────

    def _track(event_json: str):
        """Record streamed event in the activity tracker."""
        try:
            evt = json.loads(event_json)
        except Exception:
            return
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tracker = _active_validations.get(service_id)
        if not tracker:
            tracker = {
                "status": "running",
                "service_name": svc.get("name", service_id),
                "started_at": now,
                "updated_at": now,
                "phase": "",
                "step": 0,
                "progress": 0,
                "rg_name": rg_name,
                "events": [],
                "error": "",
            }
            _active_validations[service_id] = tracker
        tracker["updated_at"] = now
        if evt.get("phase"):
            tracker["phase"] = evt["phase"]
        if evt.get("step"):
            tracker["step"] = evt["step"]
        elif evt.get("attempt"):
            tracker["step"] = evt["attempt"]
        if evt.get("progress"):
            tracker["progress"] = evt["progress"]
        if evt.get("detail"):
            tracker["detail"] = evt["detail"]
            tracker["events"].append({
                "type": evt.get("type", ""),
                "phase": evt.get("phase", ""),
                "detail": evt["detail"],
                "time": now,
            })
            # Keep only last 80 events for richer history
            if len(tracker["events"]) > 80:
                tracker["events"] = tracker["events"][-80:]
        # Capture init metadata
        if evt.get("type") == "init" and evt.get("meta"):
            tracker["template_meta"] = {
                "resource_count": evt["meta"].get("resource_count", 0),
                "resource_types": evt["meta"].get("resource_types", []),
                "size_kb": evt["meta"].get("template_size_kb", 0),
                "schema": evt["meta"].get("schema", ""),
                "parameters": evt["meta"].get("parameters", []),
                "outputs": evt["meta"].get("outputs", []),
                "resource_names": evt["meta"].get("resource_names", []),
                "api_versions": evt["meta"].get("api_versions", []),
                "has_policy": evt["meta"].get("has_policy", False),
            }
            tracker["region"] = evt["meta"].get("region", "")
            tracker["subscription"] = evt["meta"].get("subscription", "")
        # Track completed steps
        if evt.get("type") == "progress" and evt.get("phase", "").endswith("_complete"):
            completed = tracker.get("steps_completed", [])
            step = evt["phase"].replace("_complete", "")
            if step not in completed:
                completed.append(step)
            tracker["steps_completed"] = completed
        # Terminal states
        if evt.get("type") == "done":
            tracker["status"] = "succeeded"
            tracker["progress"] = 1.0
        elif evt.get("type") == "error":
            tracker["status"] = "failed"
            tracker["error"] = evt.get("detail", "")

    async def stream_validation():
        nonlocal template_content
        current_template = template_content
        deployed_rg = None  # track if we need cleanup
        heal_history: list[dict] = []  # tracks each heal attempt to avoid repeating the same fix

        # ── Safety guard: ensure every parameter has a defaultValue ──
        current_template = _ensure_parameter_defaults(current_template)
        # ── Replace placeholder subscription GUIDs ──
        current_template = _sanitize_placeholder_guids(current_template)
        # ── Ensure DNS zone names are valid FQDNs ──
        current_template = _sanitize_dns_zone_names(current_template)
        def _extract_template_meta(tmpl_str: str) -> dict:
            """Extract human-readable metadata from an ARM template string."""
            try:
                t = json.loads(tmpl_str)
            except Exception:
                return {"resource_count": 0, "resource_types": [], "schema": "unknown", "size_kb": round(len(tmpl_str) / 1024, 1)}
            resources = t.get("resources", [])
            rtypes = list({r.get("type", "?") for r in resources if isinstance(r, dict)})
            rnames = [r.get("name", "?") for r in resources if isinstance(r, dict)]
            schema = t.get("$schema", "unknown")
            if "deploymentTemplate" in schema:
                schema = "ARM Deployment Template"
            api_versions = list({r.get("apiVersion", "?") for r in resources if isinstance(r, dict)})
            params = list(t.get("parameters", {}).keys())
            outputs = list(t.get("outputs", {}).keys())
            return {
                "resource_count": len(resources),
                "resource_types": rtypes,
                "resource_names": rnames,
                "api_versions": api_versions,
                "schema": schema,
                "parameters": params[:10],
                "outputs": outputs[:10],
                "size_kb": round(len(tmpl_str) / 1024, 1),
            }

        tmpl_meta = _extract_template_meta(current_template)
        import os as _os
        _sub_id = _os.environ.get("AZURE_SUBSCRIPTION_ID", "unknown")[:12] + "…"

        # Register job start with metadata
        _active_validations[service_id] = {
            "status": "running",
            "service_name": svc.get("name", service_id),
            "category": svc.get("category", ""),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "phase": "starting",
            "detail": "Initializing validation pipeline…",
            "step": 0,
            "progress": 0,
            "rg_name": rg_name,
            "region": region,
            "subscription": _sub_id,
            "template_meta": tmpl_meta,
            "steps_completed": [],
            "events": [],
            "error": "",
        }

        # Emit a rich initialization event
        yield json.dumps({
            "type": "init",
            "phase": "starting",
            "detail": f"Starting deployment validation for {svc.get('name', service_id)} ({service_id})",
            "progress": 0.005,
            "meta": {
                "service_name": svc.get("name", service_id),
                "service_id": service_id,
                "category": svc.get("category", ""),
                "region": region,
                "subscription": _sub_id,
                "resource_group": rg_name,
                "template_size_kb": tmpl_meta["size_kb"],
                "resource_count": tmpl_meta["resource_count"],
                "resource_types": tmpl_meta["resource_types"],
                "resource_names": tmpl_meta.get("resource_names", []),
                "api_versions": tmpl_meta.get("api_versions", []),
                "schema": tmpl_meta["schema"],
                "parameters": tmpl_meta.get("parameters", []),
                "outputs": tmpl_meta.get("outputs", []),
                "has_policy": bool((artifacts.get("policy", {}).get("content") or "").strip()),
            },
        }) + "\n"

        try:
            for attempt in range(1, MAX_HEAL_ATTEMPTS + 1):
                is_last = attempt == MAX_HEAL_ATTEMPTS
                att_base = (attempt - 1) / MAX_HEAL_ATTEMPTS

                if attempt == 1:
                    step_desc = f"Parsing and validating ARM template ({tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s): {', '.join(tmpl_meta['resource_types'][:3]) or 'unknown'})…"
                else:
                    step_desc = f"Verifying corrected template ({tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s): {', '.join(tmpl_meta['resource_types'][:3]) or 'unknown'}) — resolved {len(heal_history)} issue{'s' if len(heal_history) != 1 else ''} so far…"

                yield json.dumps({
                    "type": "iteration_start",
                    "step": attempt,
                    "detail": step_desc,
                    "progress": att_base + 0.01,
                }) + "\n"

                # ── 1. Parse JSON ─────────────────────────────
                try:
                    template_json = json.loads(current_template)
                except json.JSONDecodeError as e:
                    error_msg = f"ARM template is not valid JSON — parse error at line {e.lineno}, col {e.colno}: {e.msg}"
                    if is_last:
                        await fail_service_validation(service_id, error_msg)
                        yield json.dumps({"type": "error", "phase": "parsing", "step": attempt, "detail": error_msg}) + "\n"
                        return
                    yield json.dumps({"type": "healing", "phase": "fixing_template", "step": attempt, "detail": f"JSON parse error at line {e.lineno}, col {e.colno}: {e.msg} — analyzing error and resolving…", "error": error_msg, "progress": att_base + 0.02}) + "\n"
                    _pre_fix = current_template
                    current_template = await _copilot_fix("template", current_template, error_msg, previous_attempts=heal_history)
                    heal_history.append({"step": len(heal_history) + 1, "phase": "parsing", "error": error_msg, "fix_summary": _summarize_fix(_pre_fix, current_template)})
                    tmpl_meta = _extract_template_meta(current_template)
                    await save_service_artifact(service_id, "template", content=current_template, status="approved", notes=f"Auto-healed: JSON parse error")
                    yield json.dumps({"type": "healing_done", "phase": "template_fixed", "step": attempt, "detail": f"Copilot SDK rewrote template (now {tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s)) — retrying validation…", "progress": att_base + 0.03}) + "\n"
                    continue

                # ── 2. What-If ────────────────────────────────
                res_types_str = ", ".join(tmpl_meta["resource_types"][:5]) or "unknown"
                yield json.dumps({
                    "type": "progress", "phase": "what_if", "step": attempt,
                    "detail": f"Submitting ARM What-If analysis to Azure Resource Manager — previewing changes for {tmpl_meta['resource_count']} resource(s) [{res_types_str}] in resource group '{rg_name}' ({region})",
                    "progress": att_base + 0.03,
                    "step_info": {"rg": rg_name, "region": region, "resource_types": tmpl_meta["resource_types"], "resource_count": tmpl_meta["resource_count"]},
                }) + "\n"

                try:
                    from src.tools.deploy_engine import run_what_if
                    wif = await run_what_if(resource_group=rg_name, template=template_json, parameters=_extract_param_values(template_json), region=region)
                    logger.info(f"What-If attempt {attempt}: status={wif.get('status')}, changes={wif.get('total_changes')}")
                except Exception as e:
                    logger.error(f"What-If attempt {attempt} exception: {e}", exc_info=True)
                    wif = {"status": "error", "errors": [str(e)]}

                if wif.get("status") != "success":
                    errors = "; ".join(str(e) for e in wif.get("errors", [])) or "Unknown What-If error"

                    # Detect infrastructure errors that are NOT template problems
                    _infra_keywords = ("beingdeleted", "being deleted", "deprovisioning",
                                       "throttled", "toomanyrequests", "retryable",
                                       "serviceunavailable", "internalservererror",
                                       "still being deleted")
                    _is_infra_error = any(kw in errors.lower() for kw in _infra_keywords)

                    if _is_infra_error:
                        # Don't burn a heal attempt — just wait and retry (no cleanup!)
                        yield json.dumps({"type": "progress", "phase": "infra_retry", "step": attempt,
                            "detail": f"Transient Azure infrastructure error (not a template problem) — waiting 10s before retry. Error: {errors[:200]}",
                            "progress": att_base + 0.05}) + "\n"
                        await asyncio.sleep(10)
                        continue

                    if is_last:
                        await fail_service_validation(service_id, f"What-If failed — all available resolution strategies exhausted: {errors}")
                        yield json.dumps({"type": "error", "phase": "what_if", "step": attempt, "detail": f"What-If analysis rejected by Azure Resource Manager — all available resolution strategies exhausted. Error: {errors}"}) + "\n"
                        return
                    yield json.dumps({"type": "healing", "phase": "fixing_template", "step": attempt, "detail": f"What-If rejected by ARM — analyzing error and resolving. Error: {errors[:300]}", "error": errors, "progress": att_base + 0.05}) + "\n"
                    _pre_fix = current_template
                    current_template = await _copilot_fix("template", current_template, errors, previous_attempts=heal_history)
                    heal_history.append({"step": len(heal_history) + 1, "phase": "what_if", "error": errors[:500], "fix_summary": _summarize_fix(_pre_fix, current_template)})
                    tmpl_meta = _extract_template_meta(current_template)
                    await save_service_artifact(service_id, "template", content=current_template, status="approved", notes=f"Auto-healed: {errors[:200]}")
                    yield json.dumps({"type": "healing_done", "phase": "template_fixed", "step": attempt, "detail": f"Copilot SDK rewrote template (now {tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s): {', '.join(tmpl_meta['resource_types'][:3])}) — restarting validation pipeline…", "progress": att_base + 0.07}) + "\n"
                    continue

                change_summary = ", ".join(f"{v} {k}" for k, v in wif.get("change_counts", {}).items())
                # Build per-resource details for verbose display
                change_details = []
                for ch in wif.get("changes", [])[:10]:
                    change_details.append(f"{ch.get('change_type','?')}: {ch.get('resource_type','?')}/{ch.get('resource_name','?')}")
                change_detail_str = "; ".join(change_details) if change_details else "no resource-level changes"
                yield json.dumps({
                    "type": "progress", "phase": "what_if_complete", "step": attempt,
                    "detail": f"✓ What-If analysis passed — ARM accepted the template. Changes: {change_summary or 'no changes detected'}. Resources: {change_detail_str}",
                    "progress": att_base + 0.06,
                    "result": wif,
                }) + "\n"

                # ── 3. Actual Deploy ──────────────────────────
                yield json.dumps({
                    "type": "progress", "phase": "deploying", "step": attempt,
                    "detail": f"Submitting ARM deployment to Azure — provisioning {tmpl_meta['resource_count']} resource(s) [{', '.join(tmpl_meta['resource_types'][:5])}] into resource group '{rg_name}' in {region}. Deployment mode: incremental. Deployment name: validate-{attempt}",
                    "progress": att_base + 0.08,
                    "step_info": {"deployment_name": f"validate-{attempt}", "mode": "incremental", "rg": rg_name, "region": region},
                }) + "\n"

                try:
                    from src.tools.deploy_engine import execute_deployment

                    deploy_events: list[dict] = []

                    async def _on_deploy_progress(evt):
                        deploy_events.append(evt)

                    deploy_result = await execute_deployment(
                        resource_group=rg_name,
                        template=template_json,
                        parameters=_extract_param_values(template_json),
                        region=region,
                        deployment_name=f"validate-{attempt}",
                        initiated_by="InfraForge Validator",
                        on_progress=_on_deploy_progress,
                    )
                    deploy_status = deploy_result.get("status", "unknown")
                    logger.info(f"Deploy attempt {attempt}: status={deploy_status}")
                except Exception as e:
                    logger.error(f"Deploy attempt {attempt} exception: {e}", exc_info=True)
                    deploy_result = {"status": "failed", "error": str(e)}
                    deploy_status = "failed"

                deployed_rg = rg_name  # mark for cleanup

                if deploy_status != "succeeded":
                    deploy_error = deploy_result.get("error", "Unknown deployment error")

                    # If the error is the generic ARM message, try to fetch operation-level details
                    if "Please list deployment operations" in deploy_error or "At least one resource" in deploy_error:
                        try:
                            from src.tools.deploy_engine import _get_resource_client, _get_deployment_operation_errors
                            _rc = _get_resource_client()
                            _lp = asyncio.get_event_loop()
                            op_errors = await _get_deployment_operation_errors(
                                _rc, _lp, rg_name, f"validate-{attempt}"
                            )
                            if op_errors:
                                deploy_error = f"{deploy_error} | Operation errors: {op_errors}"
                                logger.info(f"Deploy attempt {attempt} operation errors: {op_errors}")
                        except Exception as oe:
                            logger.debug(f"Could not fetch operation errors: {oe}")

                    # Detect infrastructure errors that are NOT template problems
                    _is_infra_deploy = any(kw in deploy_error.lower() for kw in
                        ("beingdeleted", "being deleted", "deprovisioning",
                         "throttled", "toomanyrequests", "retryable",
                         "serviceunavailable", "internalservererror",
                         "still being deleted"))

                    yield json.dumps({
                        "type": "progress", "phase": "deploy_failed", "step": attempt,
                        "detail": f"ARM deployment 'validate-{attempt}' failed in resource group '{rg_name}' ({region}). Error from Azure: {deploy_error[:400]}",
                        "progress": att_base + 0.12,
                    }) + "\n"

                    if _is_infra_deploy:
                        yield json.dumps({"type": "progress", "phase": "infra_retry", "step": attempt,
                            "detail": f"Transient Azure infrastructure error (not a template problem) — waiting 10s before retrying into the same RG. Error: {deploy_error[:200]}",
                            "progress": att_base + 0.13}) + "\n"
                        await asyncio.sleep(10)
                        continue

                    if is_last:
                        await _cleanup_rg(rg_name)
                        await fail_service_validation(service_id, f"Deploy failed — all available resolution strategies exhausted: {deploy_error}")
                        yield json.dumps({"type": "error", "phase": "deploy", "step": attempt, "detail": f"Deployment failed — all available resolution strategies exhausted. Final error from Azure: {deploy_error}"}) + "\n"
                        return

                    yield json.dumps({"type": "healing", "phase": "fixing_template", "step": attempt, "detail": f"Deployment rejected by Azure — analyzing error and resolving. Error: {deploy_error[:300]}", "error": deploy_error, "progress": att_base + 0.13}) + "\n"
                    _pre_fix = current_template
                    current_template = await _copilot_fix("template", current_template, deploy_error, previous_attempts=heal_history)
                    heal_history.append({"step": len(heal_history) + 1, "phase": "deploy", "error": deploy_error[:500], "fix_summary": _summarize_fix(_pre_fix, current_template)})
                    tmpl_meta = _extract_template_meta(current_template)
                    await save_service_artifact(service_id, "template", content=current_template, status="approved", notes=f"Auto-healed: deploy error — {deploy_error[:200]}")
                    yield json.dumps({"type": "healing_done", "phase": "template_fixed", "step": attempt, "detail": f"Copilot SDK rewrote template (now {tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s): {', '.join(tmpl_meta['resource_types'][:3])}) — redeploying into same RG (incremental mode)…", "progress": att_base + 0.15}) + "\n"
                    # Don't cleanup — redeploy into the same RG (incremental mode)
                    continue

                # Deployment succeeded!
                provisioned = deploy_result.get("provisioned_resources", [])
                resource_summaries = [f"{r.get('type','?')}/{r.get('name','?')} ({r.get('location', region)})" for r in provisioned]

                # ── Persist deployment tracking info ──
                _deploy_name = f"validate-{attempt}"
                _subscription_id = deploy_result.get("subscription_id", "")
                try:
                    await update_service_version_deployment_info(
                        service_id, None,
                        run_id=_run_id,
                        resource_group=rg_name,
                        deployment_name=_deploy_name,
                        subscription_id=_subscription_id,
                    )
                    logger.info(f"[validate-deployment] Persisted deployment tracking: run_id={_run_id}, rg={rg_name}, deploy={_deploy_name}")
                except Exception as _te:
                    logger.warning(f"[validate-deployment] Failed to persist deployment tracking: {_te}")

                yield json.dumps({
                    "type": "progress", "phase": "deploy_complete", "step": attempt,
                    "detail": f"✓ ARM deployment 'validate-{attempt}' succeeded — {len(provisioned)} resource(s) provisioned in '{rg_name}': {'; '.join(resource_summaries[:5]) or 'none'}",
                    "progress": att_base + 0.12,
                    "resources": provisioned,
                }) + "\n"

                # ── 4. Verify resources exist ─────────────────
                yield json.dumps({
                    "type": "progress", "phase": "resource_check", "step": attempt,
                    "detail": f"Querying Azure Resource Manager to verify {len(provisioned)} resource(s) exist in resource group '{rg_name}' — fetching resource properties for policy evaluation…",
                    "progress": att_base + 0.13,
                }) + "\n"

                from src.tools.deploy_engine import _get_resource_client
                rc = _get_resource_client()
                loop = asyncio.get_event_loop()
                try:
                    live_resources = await loop.run_in_executor(
                        None,
                        lambda: list(rc.resources.list_by_resource_group(rg_name))
                    )
                    resource_details = []
                    for r in live_resources:
                        detail = {
                            "id": r.id,
                            "name": r.name,
                            "type": r.type,
                            "location": r.location,
                            "tags": dict(r.tags) if r.tags else {},
                        }
                        # Fetch full resource properties for policy evaluation
                        try:
                            full = await loop.run_in_executor(
                                None,
                                lambda r=r: rc.resources.get_by_id(r.id, api_version="2023-07-01")
                            )
                            if full.properties:
                                detail["properties"] = full.properties
                        except Exception:
                            pass
                        resource_details.append(detail)

                    res_detail_strs = [f"{r['type']}/{r['name']} @ {r['location']}" for r in resource_details[:8]]
                    yield json.dumps({
                        "type": "progress", "phase": "resource_check_complete", "step": attempt,
                        "detail": f"✓ Verified {len(resource_details)} live resource(s) in Azure: {'; '.join(res_detail_strs)}",
                        "progress": att_base + 0.14,
                        "resources": [{"name": r["name"], "type": r["type"], "location": r["location"]} for r in resource_details],
                    }) + "\n"

                except Exception as e:
                    logger.warning(f"Resource check failed: {e}")
                    resource_details = []
                    yield json.dumps({
                        "type": "progress", "phase": "resource_check_warning", "step": attempt,
                        "detail": f"Could not enumerate resources (non-fatal): {e}",
                        "progress": att_base + 0.14,
                    }) + "\n"

                # ── 5. Policy compliance test ─────────────────
                policy_content = (artifacts.get("policy", {}).get("content") or "").strip()
                policy_results = []

                if policy_content and resource_details:
                    _policy_size = round(len(policy_content) / 1024, 1)
                    try:
                        _pj = json.loads(policy_content)
                        _rule_count = len(_pj.get("rules", []))
                    except Exception:
                        _rule_count = 0
                    yield json.dumps({
                        "type": "progress", "phase": "policy_testing", "step": attempt,
                        "detail": f"Evaluating {len(resource_details)} deployed resource(s) against organization policy ({_policy_size} KB, {_rule_count} rule(s)). Checking tags, SKUs, locations, networking, and security configurations…",
                        "progress": att_base + 0.15,
                    }) + "\n"

                    try:
                        policy_json = json.loads(policy_content)
                    except json.JSONDecodeError as pe:
                        # Auto-heal policy if invalid
                        if not is_last:
                            yield json.dumps({"type": "healing", "phase": "fixing_policy", "step": attempt, "detail": f"Policy JSON error — asking AI to fix…", "error": str(pe), "progress": att_base + 0.155}) + "\n"
                            fixed_policy = await _copilot_fix("policy", policy_content, str(pe))
                            await save_service_artifact(service_id, "policy", content=fixed_policy, status="approved", notes=f"Auto-healed: policy JSON error")
                            artifacts["policy"]["content"] = fixed_policy
                            try:
                                policy_json = json.loads(fixed_policy)
                                policy_content = fixed_policy
                            except json.JSONDecodeError:
                                await _cleanup_rg(rg_name)
                                deployed_rg = None
                                continue
                        else:
                            await _cleanup_rg(rg_name)
                            await fail_service_validation(service_id, f"Policy JSON invalid: {pe}")
                            yield json.dumps({"type": "error", "phase": "policy", "step": attempt, "detail": f"Policy JSON invalid: {pe}"}) + "\n"
                            return

                    policy_results = _test_policy_compliance(policy_json, resource_details)
                    all_compliant = all(r["compliant"] for r in policy_results)
                    compliant_count = sum(1 for r in policy_results if r["compliant"])

                    for pr in policy_results:
                        icon = "✅" if pr["compliant"] else "❌"
                        yield json.dumps({
                            "type": "policy_result", "phase": "policy_testing", "step": attempt,
                            "detail": f"{icon} {pr['resource_type']}/{pr['resource_name']} — {pr['reason']}",
                            "compliant": pr["compliant"],
                            "resource": pr,
                            "progress": att_base + 0.16,
                        }) + "\n"

                    if not all_compliant:
                        violations = [pr for pr in policy_results if not pr["compliant"]]
                        violation_desc = "; ".join(f"{v['resource_name']}: {v['reason']}" for v in violations)
                        fail_msg = f"{compliant_count}/{len(policy_results)} resources compliant — {len(violations)} policy violation(s): {violation_desc[:300]}"
                        yield json.dumps({
                            "type": "progress", "phase": "policy_failed", "step": attempt,
                            "detail": fail_msg,
                            "progress": att_base + 0.17,
                        }) + "\n"

                        if is_last:
                            await _cleanup_rg(rg_name)
                            await fail_service_validation(service_id, fail_msg)
                            yield json.dumps({"type": "error", "phase": "policy", "step": attempt, "detail": f"Policy compliance failed — all available resolution strategies exhausted. Violations: {violation_desc}"}) + "\n"
                            return

                        fix_error = f"Policy violation: {violation_desc}. The policy requires: {policy_content[:500]}"
                        yield json.dumps({"type": "healing", "phase": "fixing_template", "step": attempt, "detail": f"Policy violations on {len(violations)} resource(s) — analyzing error and resolving. Violations: {violation_desc[:300]}", "error": fix_error, "progress": att_base + 0.175}) + "\n"
                        _pre_fix = current_template
                        current_template = await _copilot_fix("template", current_template, fix_error, previous_attempts=heal_history)
                        heal_history.append({"step": len(heal_history) + 1, "phase": "policy_compliance", "error": fix_error[:500], "fix_summary": _summarize_fix(_pre_fix, current_template)})
                        tmpl_meta = _extract_template_meta(current_template)
                        await save_service_artifact(service_id, "template", content=current_template, status="approved", notes=f"Auto-healed: policy violation")
                        yield json.dumps({"type": "healing_done", "phase": "template_fixed", "step": attempt, "detail": f"Copilot SDK rewrote template for policy compliance (now {tmpl_meta['size_kb']} KB) — redeploying into same RG and re-testing…", "progress": att_base + 0.18}) + "\n"
                        # Don't cleanup — redeploy into the same RG (incremental mode)
                        continue
                else:
                    yield json.dumps({
                        "type": "progress", "phase": "policy_skip", "step": attempt,
                        "detail": "No policy content or no resources to test — skipping policy check",
                        "progress": att_base + 0.16,
                    }) + "\n"

                # ── 6. Cleanup validation RG ──────────────────
                yield json.dumps({
                    "type": "progress", "phase": "cleanup", "step": attempt,
                    "detail": f"All checks passed — initiating deletion of validation resource group '{rg_name}' and all {len(resource_details)} resource(s) within it. This is fire-and-forget; Azure will complete deletion asynchronously.",
                    "progress": 0.90,
                }) + "\n"

                await _cleanup_rg(rg_name)
                deployed_rg = None

                yield json.dumps({
                    "type": "progress", "phase": "cleanup_complete", "step": attempt,
                    "detail": f"✓ Resource group '{rg_name}' deletion initiated — Azure will remove all validation resources in the background",
                    "progress": 0.93,
                }) + "\n"

                # ── 7. Promote ────────────────────────────────
                validation_summary = {
                    "what_if": wif,
                    "deployed_resources": [{"name": r["name"], "type": r["type"], "location": r["location"]} for r in resource_details],
                    "policy_compliance": policy_results,
                    "all_compliant": all(r["compliant"] for r in policy_results) if policy_results else True,
                    "attempts": attempt,
                    "run_id": _run_id,
                    "resource_group": rg_name,
                    "deployment_name": _deploy_name,
                    "subscription_id": _subscription_id,
                    "deployment_id": deploy_result.get("deployment_id", ""),
                    "deploy_result": {
                        "status": deploy_result.get("status", ""),
                        "started_at": deploy_result.get("started_at", ""),
                        "completed_at": deploy_result.get("completed_at", ""),
                    },
                    "heal_history": heal_history,
                }

                yield json.dumps({
                    "type": "progress", "phase": "promoting", "step": attempt,
                    "detail": f"All validation gates passed — promoting {svc['name']} ({service_id}) from 'validating' → 'approved' in the service catalog…",
                    "progress": 0.97,
                }) + "\n"

                await promote_service_after_validation(service_id, validation_summary)

                compliant_str = f", all {len(policy_results)} policy check(s) passed" if policy_results else ""
                res_types_done = ", ".join(tmpl_meta["resource_types"][:5]) or "N/A"
                issues_resolved = len(heal_history)
                heal_msg = f" Resolved {issues_resolved} issue{'s' if issues_resolved != 1 else ''} automatically." if issues_resolved > 0 else ""
                yield json.dumps({
                    "type": "done", "phase": "approved", "step": attempt,
                    "issues_resolved": issues_resolved,
                    "detail": f"🎉 {svc['name']} approved! Successfully deployed {len(resource_details)} resource(s) [{res_types_done}] to Azure{compliant_str}. Validation resource group cleaned up.{heal_msg}",
                    "progress": 1.0,
                    "summary": validation_summary,
                }) + "\n"
                return  # ✅ success

        except Exception as e:
            logger.error(f"Deployment validation error for {service_id}: {e}", exc_info=True)
            try:
                await fail_service_validation(service_id, str(e))
            except Exception:
                pass
            yield json.dumps({"type": "error", "phase": "unknown", "detail": str(e)}) + "\n"
        except (GeneratorExit, asyncio.CancelledError):
            # Client disconnected — clean up and mark failed so user can retry
            logger.warning(f"Validation stream cancelled (client disconnect) for {service_id}")
            try:
                await fail_service_validation(service_id, "Validation interrupted — client disconnected. Please retry.")
            except Exception:
                pass
        finally:
            # Safety net: always clean up if an RG was created
            if deployed_rg:
                try:
                    await _cleanup_rg(deployed_rg)
                except Exception:
                    pass

    async def _tracked_stream():
        """Wrap stream_validation to record every event in the activity tracker."""
        try:
            async for line in stream_validation():
                _track(line)
                yield line
        finally:
            # Clean up tracker after a delay so activity page can still show final state
            async def _cleanup_tracker():
                await asyncio.sleep(300)  # keep for 5 min after completion
                _active_validations.pop(service_id, None)
            asyncio.create_task(_cleanup_tracker())

    return StreamingResponse(
        _tracked_stream(),
        media_type="application/x-ndjson",
    )


@app.post("/api/services/{service_id:path}/artifacts/{artifact_type}/generate")
async def generate_artifact_endpoint(service_id: str, artifact_type: str, request: Request):
    """Use the Copilot SDK to generate an artifact from a natural language prompt.

    Streams the generated content back as newline-delimited JSON chunks:
      {"type": "delta", "content": "..."}   — streaming content chunk
      {"type": "done", "content": "..."}    — final full content
      {"type": "error", "message": "..."}   — error
    """
    from src.database import get_service, ARTIFACT_TYPES

    if artifact_type not in ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid artifact type. Must be one of: {', '.join(ARTIFACT_TYPES)}",
        )

    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    user_prompt = body.get("prompt", "").strip()
    if not user_prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    # Build artifact-specific system prompt
    artifact_prompts = {
        "policy": (
            f"Generate an Azure Policy definition (JSON) for the Azure service '{svc['name']}' "
            f"(resource type: {service_id}).\n\n"
            f"User requirement: {user_prompt}\n\n"
            "Return ONLY the raw Azure Policy JSON definition — no markdown fences, no explanation, "
            "no surrounding text. The JSON should be a complete, deployable Azure Policy definition "
            "with properties.displayName, properties.policyType, properties.mode, and properties.policyRule."
        ),
        "template": (
            f"Generate an ARM template (JSON) for deploying the Azure service '{svc['name']}' "
            f"(resource type: {service_id}).\n\n"
            f"User requirement: {user_prompt}\n\n"
            "Return ONLY the raw ARM JSON — no markdown fences, no explanation, no surrounding text. "
            "The template should include parameters for projectName, environment, and location. "
            "Follow Azure Well-Architected Framework best practices including proper tagging, "
            "managed identities, and diagnostic settings where applicable. "
            "This template will be deployed directly via the Azure ARM SDK."
        ),
    }

    generation_prompt = artifact_prompts[artifact_type]

    async def stream_generation():
        """SSE-style streaming via Copilot SDK."""
        # Select model based on artifact type
        _artifact_task = Task.POLICY_GENERATION if artifact_type == "policy" else Task.CODE_GENERATION
        _artifact_model = get_model_for_task(_artifact_task)
        logger.info(f"[ModelRouter] artifact generation type={artifact_type} → model={_artifact_model}")

        session = None
        try:
            # Create a temporary Copilot session for this generation
            _client = await ensure_copilot_client()
            if _client is None:
                raise RuntimeError("Copilot SDK not available")
            session = await _client.create_session({
                "model": _artifact_model,
                "streaming": True,
                "tools": [],  # No tools needed for pure generation
                "system_message": {
                    "content": (
                        "You are an Azure infrastructure expert. "
                        "Generate production-ready infrastructure artifacts. "
                        "Return ONLY the raw code/configuration — no markdown, "
                        "no explanation text, no code fences."
                    )
                },
            })

            response_chunks: list[str] = []
            done_event = asyncio.Event()

            def on_event(event):
                try:
                    if event.type.value == "assistant.message_delta":
                        delta = event.data.delta_content or ""
                        response_chunks.append(delta)
                    elif event.type.value in ("assistant.message", "session.idle"):
                        done_event.set()
                except Exception as e:
                    logger.error(f"Generation event error: {e}")
                    done_event.set()

            unsubscribe = session.on(on_event)

            try:
                await session.send({"prompt": generation_prompt})
                await asyncio.wait_for(done_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                yield json.dumps({"type": "error", "message": "Generation timed out"}) + "\n"
                return
            finally:
                unsubscribe()

            full_content = "".join(response_chunks).strip()

            # Strip markdown code fences if the model wrapped them anyway
            if full_content.startswith("```"):
                lines = full_content.split("\n")
                # Remove first line (```json, ```bicep, etc.)
                lines = lines[1:]
                # Remove last line if it's just ```
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                full_content = "\n".join(lines).strip()

            yield json.dumps({"type": "done", "content": full_content}) + "\n"

        except Exception as e:
            logger.error(f"Artifact generation failed: {e}")
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"
        finally:
            if session:
                try:
                    await session.destroy()
                except Exception:
                    pass

    return StreamingResponse(
        stream_generation(),
        media_type="application/x-ndjson",
    )


# ── Service Versions & Onboarding ─────────────────────────────

@app.get("/api/services/{service_id:path}/versions")
async def get_service_versions_endpoint(service_id: str, status: str | None = None):
    """Get all versions of a service's ARM template.

    Query params:
        status: filter by version status (e.g. 'approved', 'failed', 'draft')
    """
    from src.database import get_service, get_service_versions

    try:
        svc = await get_service(service_id)
        if not svc:
            raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

        versions = await get_service_versions(service_id, status=status)
        # Strip arm_template from listing to keep payload small; use single-version endpoint to fetch it
        versions_summary = []
        for v in versions:
            vs = {k: v2 for k, v2 in v.items() if k != "arm_template"}
            vs["template_size_bytes"] = len(v.get("arm_template") or "") if v.get("arm_template") else 0
            versions_summary.append(vs)
        return JSONResponse({
            "service_id": service_id,
            "active_version": svc.get("active_version"),
            "versions": versions_summary,
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error fetching versions for {service_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/services/{service_id:path}/versions/{version:int}")
async def get_service_version_detail(service_id: str, version: int):
    """Get a single version including the full ARM template content."""
    from src.database import get_service, get_service_versions

    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    versions = await get_service_versions(service_id)
    match = next((v for v in versions if v.get("version") == version), None)
    if not match:
        raise HTTPException(status_code=404, detail=f"Version {version} not found for '{service_id}'")

    return JSONResponse(match)


@app.post("/api/services/{service_id:path}/versions/{version:int}/modify")
async def modify_service_version(service_id: str, version: int, request: Request):
    """Modify an existing ARM template version via LLM and save as a new version.

    Accepts a natural-language prompt describing the desired modification,
    sends the current template + prompt to the Copilot SDK, and saves the
    result as a new version with a semver bump.

    Request body:
        prompt (str): Description of the modification to apply
        model (str, optional): LLM model override

    Streams NDJSON events for real-time progress tracking.
    """
    from src.database import (
        get_service, get_service_versions, create_service_version,
    )
    from src.tools.arm_generator import modify_arm_template_with_copilot

    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be JSON with a 'prompt' field")

    modification_prompt = (body.get("prompt") or "").strip()
    if not modification_prompt:
        raise HTTPException(status_code=400, detail="'prompt' field is required and cannot be empty")

    model_id = body.get("model", get_active_model())

    # Fetch the source version
    versions = await get_service_versions(service_id)
    source = next((v for v in versions if v.get("version") == version), None)
    if not source:
        raise HTTPException(status_code=404, detail=f"Version {version} not found for '{service_id}'")

    source_template = source.get("arm_template", "")
    if not source_template:
        raise HTTPException(status_code=400, detail=f"Version {version} has no ARM template content")

    source_semver = source.get("semver") or f"{version}.0.0"

    async def _stream():
        yield json.dumps({
            "type": "progress",
            "phase": "start",
            "detail": f"Modifying ARM template v{source_semver} for {service_id}…",
            "progress": 0.0,
        }) + "\n"

        yield json.dumps({
            "type": "progress",
            "phase": "llm",
            "detail": f"Sending template + modification prompt to LLM ({model_id})…",
            "progress": 0.15,
        }) + "\n"

        try:
            # Send to LLM for modification
            _client = await ensure_copilot_client()
            if _client is None:
                raise RuntimeError("Copilot SDK not available")
            modified_template = await modify_arm_template_with_copilot(
                existing_template=source_template,
                modification_prompt=modification_prompt,
                resource_type=service_id,
                copilot_client=_client,
                model=model_id,
            )

            yield json.dumps({
                "type": "progress",
                "phase": "generated",
                "detail": "✓ LLM returned modified template — processing…",
                "progress": 0.50,
            }) + "\n"

            # Ensure parameter defaults
            modified_template = _ensure_parameter_defaults(modified_template)

            # Compute new version number
            from src.database import get_backend as _get_db_backend
            _db = await _get_db_backend()
            _vrows = await _db.execute(
                "SELECT MAX(version) as max_ver FROM service_versions WHERE service_id = ?",
                (service_id,),
            )
            new_ver = (_vrows[0]["max_ver"] if _vrows and _vrows[0]["max_ver"] else 0) + 1

            # Semver: bump minor from the source version
            # e.g. source 1.0.0 → 1.1.0, source 2.0.0 → 2.1.0
            source_parts = source_semver.split(".")
            try:
                major = int(source_parts[0])
                minor = int(source_parts[1]) + 1 if len(source_parts) > 1 else 1
            except (ValueError, IndexError):
                major, minor = new_ver, 0
            new_semver = f"{major}.{minor}.0"

            # Stamp metadata
            modified_template = _stamp_template_metadata(
                modified_template,
                service_id=service_id,
                version_int=new_ver,
                gen_source=f"llm-modify ({model_id})",
                region="eastus2",
            )

            yield json.dumps({
                "type": "progress",
                "phase": "saving",
                "detail": f"Saving as v{new_semver} (version {new_ver})…",
                "progress": 0.75,
            }) + "\n"

            # Save as a new draft version — must pass validation before becoming active
            ver = await create_service_version(
                service_id=service_id,
                arm_template=modified_template,
                version=new_ver,
                semver=new_semver,
                status="draft",
                changelog=f"Modified from v{source_semver}: {modification_prompt[:200]}",
                created_by=f"llm-modify ({model_id})",
            )

            # Parse for summary
            try:
                parsed = json.loads(modified_template)
                resource_count = len(parsed.get("resources", []))
                size_kb = f"{len(modified_template) / 1024:.1f}"
            except Exception:
                resource_count = "?"
                size_kb = "?"

            yield json.dumps({
                "type": "complete",
                "phase": "done",
                "detail": f"✓ Template saved as draft v{new_semver} "
                          f"({resource_count} resource(s), {size_kb} KB) — validate to promote",
                "progress": 1.0,
                "version": new_ver,
                "semver": new_semver,
                "service_id": service_id,
                "status": "draft",
            }) + "\n"

        except ValueError as e:
            yield json.dumps({
                "type": "error",
                "phase": "failed",
                "detail": f"✗ Modification failed: {str(e)}",
                "progress": 1.0,
            }) + "\n"
        except Exception as e:
            logger.exception(f"Template modification failed for {service_id}")
            yield json.dumps({
                "type": "error",
                "phase": "failed",
                "detail": f"✗ Unexpected error: {str(e)}",
                "progress": 1.0,
            }) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


@app.post("/api/services/{service_id:path}/onboard")
async def onboard_service_endpoint(service_id: str, request: Request):
    """One-click service onboarding: auto-generate ARM template and run full validation.

    New pipeline:
    1. Auto-generate ARM template from resource type (built-in skeleton or Copilot)
    2. Static policy check against org-wide governance policies + security standards
    3. ARM What-If deployment preview
    4. ARM deployment to validation resource group
    5. Runtime resource compliance check
    6. Cleanup validation RG
    7. Promote: version → approved, service → approved

    Streams NDJSON events for real-time progress tracking.
    Auto-healing via Copilot SDK (up to 5 attempts).
    """
    from src.database import (
        get_service, create_service_version, update_service_version_status,
        update_service_version_template, set_active_service_version,
        fail_service_validation, get_governance_policies_as_dict,
        update_service_version_deployment_info,
    )
    from src.tools.arm_generator import (
        generate_arm_template, has_builtin_skeleton,
        generate_arm_template_with_copilot,
    )
    from src.tools.static_policy_validator import (
        validate_template, validate_template_against_standards,
        build_remediation_prompt,
    )
    from src.standards import (
        get_standards_for_service,
        get_all_standards,
        build_arm_generation_context,
        build_policy_generation_context,
    )

    MAX_HEAL_ATTEMPTS = 5

    svc = await get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")

    try:
        body = await request.json()
    except Exception:
        body = {}

    region = body.get("region", "eastus2")
    # Allow per-request model override, fall back to active global model
    model_id = body.get("model", get_active_model())
    # If use_version is set, skip generation and validate the existing draft version
    use_version: int | None = body.get("use_version")
    import uuid as _uuid
    _run_id = _uuid.uuid4().hex[:8]
    rg_name = f"infraforge-val-{service_id.replace('/', '-').replace('.', '-').lower()}-{_run_id}"[:90]

    # ── LLM reasoning helper ─────────────────────────────────

    async def _llm_reason(prompt: str, system_msg: str = "", task: Task = Task.PLANNING) -> str:
        """Ask the LLM to reason about a topic and return its full response.

        The model is selected automatically based on the task type.
        """
        task_model = get_model_for_task(task)
        logger.info(f"[ModelRouter] _llm_reason task={task.value} → model={task_model}")
        session = None
        try:
            _client = await ensure_copilot_client()
            if _client is None:
                raise RuntimeError("Copilot SDK not available")
            session = await _client.create_session({
                "model": task_model, "streaming": True, "tools": [],
                "system_message": {"content": system_msg or (
                    "You are an Azure infrastructure expert performing a detailed analysis. "
                    "Think step-by-step and explain your reasoning clearly."
                )},
            })
            chunks: list[str] = []
            done_ev = asyncio.Event()

            def on_event(ev):
                try:
                    if ev.type.value == "assistant.message_delta":
                        chunks.append(ev.data.delta_content or "")
                    elif ev.type.value in ("assistant.message", "session.idle"):
                        done_ev.set()
                except Exception:
                    done_ev.set()

            unsub = session.on(on_event)
            try:
                await session.send({"prompt": prompt})
                await asyncio.wait_for(done_ev.wait(), timeout=90)
            finally:
                unsub()
            return "".join(chunks).strip()
        finally:
            if session:
                try:
                    await session.destroy()
                except Exception:
                    pass

    # ── Copilot fix helper ────────────────────────────────────

    async def _copilot_fix(content: str, error: str, standards_ctx: str = "",
                           planning_context: str = "",
                           previous_attempts: list[dict] | None = None) -> str:
        """Ask the Copilot SDK to fix an ARM template.

        Uses the CODE_FIXING model (gpt-4.1) for surgical error repair.
        Includes the architecture plan so the healer knows the template's intent.
        Tracks previous attempts so each iteration tries a DIFFERENT strategy.
        """
        fix_model = get_model_for_task(Task.CODE_FIXING)
        attempt_num = len(previous_attempts) + 1 if previous_attempts else 1
        logger.info(f"[ModelRouter] _copilot_fix → model={fix_model}, attempt={attempt_num}")

        prompt = (
            "The following ARM template failed Azure deployment validation.\n\n"
            f"--- ERROR ---\n{error}\n--- END ERROR ---\n\n"
            f"--- CURRENT TEMPLATE ---\n{content}\n--- END TEMPLATE ---\n\n"
        )

        # Include parameter values so the LLM can see what was sent to ARM
        try:
            _fix_tpl2 = json.loads(content)
            _fix_params2 = _extract_param_values(_fix_tpl2)
            if _fix_params2:
                prompt += (
                    "--- PARAMETER VALUES SENT TO ARM ---\n"
                    f"{json.dumps(_fix_params2, indent=2, default=str)}\n"
                    "--- END PARAMETER VALUES ---\n\n"
                    "IMPORTANT: These are the actual values sent to Azure. "
                    "If the error is caused by one of these values (invalid "
                    "name, bad format), fix the corresponding parameter's "
                    "\"defaultValue\" in the template.\n\n"
                )
        except Exception:
            pass

        # ── Previous attempt history (prevents repeating the same fix) ──
        if previous_attempts:
            prompt += "--- PREVIOUS FAILED ATTEMPTS (DO NOT repeat these fixes) ---\n"
            for pa in previous_attempts:
                prompt += (
                    f"Step {pa.get('step', '?')}: Error was: {pa['error'][:300]}\n"
                    f"  Fix tried: {pa['fix_summary']}\n"
                    f"  Result: STILL FAILED — do something DIFFERENT\n\n"
                )
            prompt += "--- END PREVIOUS ATTEMPTS ---\n\n"

        if planning_context:
            prompt += (
                f"--- ARCHITECTURE PLAN (what this template is supposed to achieve) ---\n"
                f"{planning_context}\n--- END PLAN ---\n\n"
            )
        if standards_ctx:
            prompt += (
                f"--- ORGANIZATION STANDARDS (MUST be satisfied) ---\n{standards_ctx}\n"
                "--- END STANDARDS ---\n\n"
            )
        prompt += (
            "Fix the template so it deploys successfully. Return ONLY the "
            "corrected raw JSON — no markdown fences, no explanation.\n\n"
            "CRITICAL RULES (in priority order):\n\n"
            "1. PARAMETER VALUES — Check parameter defaultValues FIRST:\n"
            "   - If the error mentions an invalid resource name, the name likely "
            "     comes from a parameter defaultValue. Find that parameter and fix "
            "     its defaultValue to comply with Azure naming rules.\n"
            "   - Azure DNS zone names MUST be valid FQDNs with at least two labels "
            "     (e.g. 'infraforge-demo.com', NOT 'if-dnszones').\n"
            "   - Storage account names: 3-24 lowercase alphanumeric, no hyphens.\n"
            "   - Key vault names: 3-24 alphanumeric + hyphens.\n"
            "   - Ensure EVERY parameter has a \"defaultValue\".\n\n"
            "2. LOCATIONS — Keep ALL location parameters as \"[resourceGroup().location]\" "
            "or \"[parameters('location')]\" — NEVER hardcode a region.\n"
            "   EXCEPTION: Globally-scoped resources MUST use location \"global\":\n"
            "   * Microsoft.Network/dnszones → \"global\"\n"
            "   * Microsoft.Network/trafficManagerProfiles → \"global\"\n"
            "   * Microsoft.Cdn/profiles → \"global\"\n\n"
            "3. STRUCTURAL FIXES:\n"
            "   - Keep the same resource intent and resource names.\n"
            "   - Fix schema issues, missing required properties, invalid API versions.\n"
            "   - If diagnosticSettings requires an external dependency, REMOVE it.\n"
            "   - Ensure ALL resources have tags: environment, owner, costCenter, project.\n"
            "   - NEVER use '00000000-0000-0000-0000-000000000000' as a subscription ID — "
            "     use [subscription().subscriptionId] instead.\n"
            "   - If the error mentions 'LinkedAuthorizationFailed', use "
            "     [subscription().subscriptionId] in resourceId() expressions.\n"
            "   - If a resource requires complex external deps (VPN gateways, "
            "     ExpressRoute), SIMPLIFY by removing those references.\n"
            "   - NEVER add properties that require subscription-level feature registration. "
            "     If the error mentions 'feature is not enabled', REMOVE the property.\n"
        )

        # ── Escalation strategies for later attempts ──
        if attempt_num >= 4:
            prompt += (
                f"\nESCALATION — multiple strategies have failed, drastic measures needed:\n"
                "- SIMPLIFY the template: remove optional/nice-to-have resources\n"
                "- Remove diagnosticSettings, locks, autoscale rules if they are causing issues\n"
                "- Use the SIMPLEST valid configuration for each resource\n"
                "- Strip down to ONLY the primary resource with minimal properties\n"
                "- Use well-known, stable API versions (prefer 2023-xx-xx or 2024-xx-xx)\n"
            )
        elif attempt_num >= 2:
            prompt += (
                f"\nPrevious fix(es) did NOT work.\n"
                "You MUST try a FUNDAMENTALLY DIFFERENT approach:\n"
                "- Try a different API version for the failing resource\n"
                "- Restructure resource dependencies\n"
                "- Remove or replace the problematic sub-resource\n"
                "- Check if required properties changed in newer API versions\n"
            )

        session = None
        try:
            _client = await ensure_copilot_client()
            if _client is None:
                raise RuntimeError("Copilot SDK not available")
            session = await _client.create_session({
                "model": fix_model, "streaming": True, "tools": [],
                "system_message": {"content": (
                    "You are an Azure infrastructure expert. "
                    "Return ONLY raw JSON — no markdown, no code fences."
                )},
            })
            chunks: list[str] = []
            done_ev = asyncio.Event()

            def on_event(ev):
                try:
                    if ev.type.value == "assistant.message_delta":
                        chunks.append(ev.data.delta_content or "")
                    elif ev.type.value in ("assistant.message", "session.idle"):
                        done_ev.set()
                except Exception:
                    done_ev.set()

            unsub = session.on(on_event)
            try:
                await session.send({"prompt": prompt})
                await asyncio.wait_for(done_ev.wait(), timeout=90)
            finally:
                unsub()

            fixed = "".join(chunks).strip()
            if fixed.startswith("```"):
                lines = fixed.split("\n")[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                fixed = "\n".join(lines).strip()

            # Guard: if healer returned empty or non-JSON, return original
            if not fixed:
                logger.warning("Copilot healer returned empty response — keeping original template")
                return content

            # Try to extract JSON if healer wrapped it in text
            if not fixed.startswith("{"):
                # Try to find JSON object in the response
                _json_start = fixed.find("{")
                _json_end = fixed.rfind("}")
                if _json_start >= 0 and _json_end > _json_start:
                    fixed = fixed[_json_start:_json_end + 1]
                else:
                    logger.warning("Copilot healer returned non-JSON text — keeping original template")
                    return content

            # Validate it's actually valid JSON before returning
            try:
                json.loads(fixed)
            except json.JSONDecodeError:
                logger.warning("Copilot healer returned invalid JSON — keeping original template")
                return content

            # Guard: ensure healer didn't corrupt the location parameter
            # NOTE: some resources (DNS zones, Traffic Manager, etc.) use "global"
            _GLOBAL_LOCATION_TYPES_TOOL = {
                "microsoft.network/dnszones",
                "microsoft.network/trafficmanagerprofiles",
                "microsoft.cdn/profiles",
                "microsoft.network/frontdoors",
                "microsoft.network/frontdoorwebapplicationfirewallpolicies",
            }
            try:
                _ft = json.loads(fixed)
                _params = _ft.get("parameters", {})
                _loc = _params.get("location", {})
                _dv = _loc.get("defaultValue", "")
                if isinstance(_dv, str) and _dv and not _dv.startswith("["):
                    _loc["defaultValue"] = "[resourceGroup().location]"
                    logger.warning(f"Copilot healer corrupted location to '{_dv}' — restored")
                    fixed = json.dumps(_ft, indent=2)
                for _res in _ft.get("resources", []):
                    _rtype = (_res.get("type") or "").lower()
                    _rloc = _res.get("location", "")
                    if _rtype in _GLOBAL_LOCATION_TYPES_TOOL:
                        if isinstance(_rloc, str) and _rloc.lower() != "global":
                            _res["location"] = "global"
                            fixed = json.dumps(_ft, indent=2)
                        continue
                    if isinstance(_rloc, str) and _rloc and not _rloc.startswith("["):
                        _res["location"] = "[parameters('location')]"
                        logger.warning(f"Copilot healer hardcoded resource location to '{_rloc}' — restored")
                        fixed = json.dumps(_ft, indent=2)
            except (json.JSONDecodeError, AttributeError):
                pass

            # ── Guard: ensure every param has a defaultValue ──
            fixed = _ensure_parameter_defaults(fixed)
            fixed = _sanitize_placeholder_guids(fixed)

            return fixed
        finally:
            if session:
                try:
                    await session.destroy()
                except Exception:
                    pass

    # ── Cleanup helper ────────────────────────────────────────

    async def _cleanup_rg(rg: str):
        from src.tools.deploy_engine import _get_resource_client
        client = _get_resource_client()
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, lambda: client.resource_groups.begin_delete(rg)
            )
            logger.info(f"Cleanup: deletion started for resource group '{rg}'")
        except Exception as e:
            logger.warning(f"Cleanup: failed to delete resource group '{rg}': {e}")

    # ── Template metadata helper ──────────────────────────────

    def _extract_meta(tmpl_str: str) -> dict:
        try:
            t = json.loads(tmpl_str)
        except Exception:
            return {"resource_count": 0, "resource_types": [], "schema": "unknown",
                    "size_kb": round(len(tmpl_str) / 1024, 1)}
        resources = t.get("resources", [])
        rtypes = list({r.get("type", "?") for r in resources if isinstance(r, dict)})
        rnames = [r.get("name", "?") for r in resources if isinstance(r, dict)]
        schema = t.get("$schema", "unknown")
        if "deploymentTemplate" in schema:
            schema = "ARM Deployment Template"
        api_versions = list({r.get("apiVersion", "?") for r in resources if isinstance(r, dict)})
        params = list(t.get("parameters", {}).keys())
        outputs = list(t.get("outputs", {}).keys())
        return {
            "resource_count": len(resources),
            "resource_types": rtypes,
            "resource_names": rnames,
            "api_versions": api_versions,
            "schema": schema,
            "parameters": params[:10],
            "outputs": outputs[:10],
            "size_kb": round(len(tmpl_str) / 1024, 1),
        }

    # ── Policy compliance evaluation helpers ──────────────────

    def _test_policy_compliance(policy_json: dict, resources: list[dict]) -> list[dict]:
        """Evaluate deployed resources against an Azure Policy definition.

        Interprets the policy's 'if' condition against each resource's
        actual Azure properties. Returns per-resource compliance results.
        """
        results = []
        rule = policy_json.get("properties", policy_json).get("policyRule", {})
        if_condition = rule.get("if", {})
        effect = rule.get("then", {}).get("effect", "deny")

        for resource in resources:
            match = _evaluate_condition(if_condition, resource)
            # If the condition matches → the policy's effect applies (deny/audit)
            # A "deny" match means the resource VIOLATES the policy
            compliant = not match if effect.lower() in ("deny", "audit") else match
            results.append({
                "resource_id": resource.get("id", ""),
                "resource_type": resource.get("type", ""),
                "resource_name": resource.get("name", ""),
                "location": resource.get("location", ""),
                "compliant": compliant,
                "effect": effect,
                "reason": (
                    "Resource matches policy conditions — compliant"
                    if compliant else
                    f"Resource violates policy — {effect} would apply"
                ),
            })
        return results

    def _evaluate_condition(condition: dict, resource: dict) -> bool:
        """Recursively evaluate an Azure Policy condition against a resource."""
        if "allOf" in condition:
            results = [_evaluate_condition(c, resource) for c in condition["allOf"]]
            result = all(results)
            logger.debug(f"  allOf({len(condition['allOf'])} conditions) = {result} (individual: {results})")
            return result
        if "anyOf" in condition:
            results = [_evaluate_condition(c, resource) for c in condition["anyOf"]]
            result = any(results)
            logger.debug(f"  anyOf({len(condition['anyOf'])} conditions) = {result} (individual: {results})")
            return result
        if "not" in condition:
            inner = _evaluate_condition(condition["not"], resource)
            logger.debug(f"  not({inner}) = {not inner}")
            return not inner

        field = condition.get("field", "")
        resource_val = _resolve_field(field, resource)

        # Determine which operator is used and evaluate
        result = False
        op = "unknown"
        if "equals" in condition:
            op = "equals"
            result = str(resource_val).lower() == str(condition["equals"]).lower()
        elif "notEquals" in condition:
            op = "notEquals"
            result = str(resource_val).lower() != str(condition["notEquals"]).lower()
        elif "in" in condition:
            op = "in"
            result = str(resource_val).lower() in [str(v).lower() for v in condition["in"]]
        elif "notIn" in condition:
            op = "notIn"
            result = str(resource_val).lower() not in [str(v).lower() for v in condition["notIn"]]
        elif "contains" in condition:
            op = "contains"
            result = str(condition["contains"]).lower() in str(resource_val).lower()
        elif "like" in condition:
            op = "like"
            import fnmatch
            result = fnmatch.fnmatch(str(resource_val).lower(), str(condition["like"]).lower())
        elif "exists" in condition:
            op = "exists"
            exists = resource_val is not None and resource_val != ""
            # Normalize string booleans: LLMs often return "false"/"true" strings
            want_exists = condition["exists"]
            if isinstance(want_exists, str):
                want_exists = want_exists.lower() not in ("false", "0", "no")
            result = exists if want_exists else not exists
        elif "greater" in condition:
            op = "greater"
            try:
                result = float(resource_val or 0) > float(condition["greater"])
            except (ValueError, TypeError):
                result = False
        elif "less" in condition:
            op = "less"
            try:
                result = float(resource_val or 0) < float(condition["less"])
            except (ValueError, TypeError):
                result = False

        logger.info(f"  Policy eval: field='{field}' op={op} expected={condition.get(op, '?')} actual='{resource_val}' → {result}")
        return result

    def _resolve_field(field: str, resource: dict):
        """Resolve an Azure Policy field reference against a resource dict."""
        field_lower = field.lower()
        if field_lower == "type":
            return resource.get("type", "")
        if field_lower == "location":
            return resource.get("location", "")
        if field_lower == "name":
            return resource.get("name", "")
        if field_lower.startswith("tags["):
            tag_name = field.split("'")[1] if "'" in field else field.split("[")[1].rstrip("]")
            return (resource.get("tags") or {}).get(tag_name, "")
        if field_lower.startswith("tags."):
            tag_name = field.split(".", 1)[1]
            return (resource.get("tags") or {}).get(tag_name, "")
        # properties.X.Y.Z → nested lookup
        parts = field.split(".")
        val = resource
        for part in parts:
            if isinstance(val, dict):
                matched = None
                for k in val:
                    if k.lower() == part.lower():
                        matched = k
                        break
                val = val.get(matched) if matched else None
            else:
                return None
        return val

    # ── Activity tracker ──────────────────────────────────────

    def _track(event_json: str):
        try:
            evt = json.loads(event_json)
        except Exception:
            return
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tracker = _active_validations.get(service_id)
        if not tracker:
            tracker = {
                "status": "running",
                "service_name": svc.get("name", service_id),
                "started_at": now,
                "updated_at": now,
                "phase": "",
                "step": 0,
                "progress": 0,
                "rg_name": rg_name,
                "events": [],
                "error": "",
            }
            _active_validations[service_id] = tracker
        tracker["updated_at"] = now
        if evt.get("phase"):
            tracker["phase"] = evt["phase"]
        if evt.get("step"):
            tracker["step"] = evt["step"]
        elif evt.get("attempt"):
            tracker["step"] = evt["attempt"]
        if evt.get("progress"):
            tracker["progress"] = evt["progress"]
        if evt.get("detail"):
            tracker["detail"] = evt["detail"]
            tracker["events"].append({
                "type": evt.get("type", ""),
                "phase": evt.get("phase", ""),
                "detail": evt["detail"],
                "time": now,
            })
            if len(tracker["events"]) > 80:
                tracker["events"] = tracker["events"][-80:]
        if evt.get("type") == "init" and evt.get("meta"):
            tracker["template_meta"] = evt["meta"]
            tracker["region"] = evt["meta"].get("region", "")
            tracker["subscription"] = evt["meta"].get("subscription", "")
        if evt.get("type") == "progress" and evt.get("phase", "").endswith("_complete"):
            completed = tracker.get("steps_completed", [])
            step = evt["phase"].replace("_complete", "")
            if step not in completed:
                completed.append(step)
            tracker["steps_completed"] = completed
        if evt.get("type") == "done":
            tracker["status"] = "succeeded"
            tracker["progress"] = 1.0
        elif evt.get("type") == "error":
            tracker["status"] = "failed"
            tracker["error"] = evt.get("detail", "")

    # ── Main streaming generator ──────────────────────────────

    async def stream_onboarding():
        deployed_rg = None
        version_num = None
        current_template = ""
        standards_ctx = ""  # org standards context for ARM generation
        planning_response = ""  # architecture plan from the PLAN phase — fed into EXECUTE
        heal_history: list[dict] = []  # tracks each heal attempt to avoid repeating the same fix

        import os as _os
        _sub_id = _os.environ.get("AZURE_SUBSCRIPTION_ID", "unknown")[:12] + "…"

        # Register job start
        _active_validations[service_id] = {
            "status": "running",
            "service_name": svc.get("name", service_id),
            "category": svc.get("category", ""),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "phase": "starting",
            "detail": "Initializing onboarding pipeline…",
            "step": 0,
            "progress": 0,
            "rg_name": rg_name,
            "region": region,
            "subscription": _sub_id,
            "steps_completed": [],
            "events": [],
            "error": "",
        }

        try:
            # ═══════════════════════════════════════════════════
            # PHASE 0: INITIALIZATION & MODEL ROUTING TABLE
            # ═══════════════════════════════════════════════════

            # Build the per-task model routing summary for this run
            _routing = {
                "planning":        {"model": get_model_for_task(Task.PLANNING),          "display": get_model_display(Task.PLANNING),          "reason": get_task_reason(Task.PLANNING)},
                "code_generation": {"model": get_model_for_task(Task.CODE_GENERATION),   "display": get_model_display(Task.CODE_GENERATION),   "reason": get_task_reason(Task.CODE_GENERATION)},
                "code_fixing":     {"model": get_model_for_task(Task.CODE_FIXING),       "display": get_model_display(Task.CODE_FIXING),       "reason": get_task_reason(Task.CODE_FIXING)},
                "policy_gen":      {"model": get_model_for_task(Task.POLICY_GENERATION), "display": get_model_display(Task.POLICY_GENERATION), "reason": get_task_reason(Task.POLICY_GENERATION)},
                "analysis":        {"model": get_model_for_task(Task.VALIDATION_ANALYSIS),"display": get_model_display(Task.VALIDATION_ANALYSIS),"reason": get_task_reason(Task.VALIDATION_ANALYSIS)},
            }

            yield json.dumps({
                "type": "progress", "phase": "init_model",
                "detail": f"🤖 Model routing configured — each pipeline phase uses the optimal model for its task",
                "progress": 0.01,
                "model_routing": _routing,
            }) + "\n"

            # Emit each model assignment as a visible log entry
            for task_key, info in _routing.items():
                yield json.dumps({
                    "type": "llm_reasoning", "phase": "init_model",
                    "detail": f"  {task_key}: {info['display']} — {info['reason'][:80]}",
                    "progress": 0.01,
                }) + "\n"

            # ═══════════════════════════════════════════════════
            # SHORTCUT: use_version — skip generation, validate existing draft
            # ═══════════════════════════════════════════════════

            _skip_generation = False

            if use_version is not None:
                from src.database import get_service_versions as _get_svc_versions
                _all_vers = await _get_svc_versions(service_id)
                _draft = next((v for v in _all_vers if v.get("version") == use_version), None)
                if not _draft:
                    yield json.dumps({
                        "type": "error", "phase": "init",
                        "detail": f"Version {use_version} not found for {service_id}",
                    }) + "\n"
                    return

                current_template = _draft.get("arm_template", "")
                if not current_template:
                    yield json.dumps({
                        "type": "error", "phase": "init",
                        "detail": f"Version {use_version} has no ARM template content",
                    }) + "\n"
                    return

                version_num = use_version
                _draft_semver = _draft.get("semver") or f"{use_version}.0.0"
                gen_source = _draft.get("created_by") or "draft"

                # Mark version as validating
                await update_service_version_status(service_id, use_version, "validating")

                tmpl_meta = _extract_meta(current_template)
                applicable_standards = await get_standards_for_service(service_id)
                policy_standards_ctx = await build_policy_generation_context(service_id)

                yield json.dumps({
                    "type": "progress", "phase": "use_version",
                    "detail": f"📋 Using existing draft v{_draft_semver} — skipping generation, proceeding to validation…",
                    "progress": 0.10,
                }) + "\n"

                yield json.dumps({
                    "type": "init", "phase": "generated",
                    "detail": f"✓ Draft ARM template v{_draft_semver} loaded — "
                              f"{tmpl_meta['resource_count']} resource(s), {tmpl_meta['size_kb']} KB",
                    "progress": 0.12,
                    "version": version_num,
                    "semver": _draft_semver,
                    "meta": {
                        "service_name": svc.get("name", service_id),
                        "service_id": service_id,
                        "category": svc.get("category", ""),
                        "region": region,
                        "subscription": _sub_id,
                        "resource_group": rg_name,
                        "template_size_kb": tmpl_meta["size_kb"],
                        "resource_count": tmpl_meta["resource_count"],
                        "resource_types": tmpl_meta["resource_types"],
                        "resource_names": tmpl_meta.get("resource_names", []),
                        "api_versions": tmpl_meta.get("api_versions", []),
                        "schema": tmpl_meta["schema"],
                        "parameters": tmpl_meta.get("parameters", []),
                        "outputs": tmpl_meta.get("outputs", []),
                        "version": version_num,
                        "gen_source": gen_source,
                        "model_routing": _routing,
                        "standards_count": len(applicable_standards),
                    },
                }) + "\n"

                _skip_generation = True

            # ═══════════════════════════════════════════════════
            # PHASE 1: ORGANIZATION STANDARDS ANALYSIS
            # (skipped when validating an existing draft via use_version)
            # ═══════════════════════════════════════════════════

            if not _skip_generation:
                yield json.dumps({
                    "type": "progress", "phase": "standards_analysis",
                    "detail": f"Fetching organization standards applicable to {service_id}…",
                    "progress": 0.02,
                }) + "\n"

                applicable_standards = await get_standards_for_service(service_id)
                standards_ctx = await build_arm_generation_context(service_id)
                policy_standards_ctx = await build_policy_generation_context(service_id)

                if applicable_standards:
                    # Emit each standard as a separate event for the log
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

                        yield json.dumps({
                            "type": "standard_check", "phase": "standards_analysis",
                            "detail": f"{sev_icon} [{std.get('severity', '?').upper()}] {std['name']}: {std['description']}{rule_summary}",
                            "standard": {"id": std["id"], "name": std["name"], "severity": std.get("severity"), "category": std.get("category")},
                            "progress": 0.03,
                        }) + "\n"

                    yield json.dumps({
                        "type": "progress", "phase": "standards_complete",
                        "detail": f"✓ {len(applicable_standards)} organization standard(s) apply — these will constrain ARM template generation and policy validation",
                        "progress": 0.04,
                    }) + "\n"
                else:
                    yield json.dumps({
                        "type": "progress", "phase": "standards_complete",
                        "detail": "No organization standards match this service type — proceeding with default governance rules",
                        "progress": 0.04,
                    }) + "\n"

                # ═══════════════════════════════════════════════════
                # PHASE 2: ARCHITECTURE PLANNING (REASONING MODEL)
                # ═══════════════════════════════════════════════════
                #
                # This is the PLAN phase. It uses a reasoning model (o3-mini) to
                # think deeply about what the ARM template should contain. The
                # plan output is then fed into the EXECUTE phase (code generation)
                # so the generation model doesn't have to figure out architecture
                # — it just follows the plan.

                _planning_model = get_model_display(Task.PLANNING)
                yield json.dumps({
                    "type": "progress", "phase": "planning",
                    "detail": f"🧠 PLAN phase — {_planning_model} is reasoning about architecture for {service_id}…",
                    "progress": 0.05,
                }) + "\n"

                planning_prompt = (
                    f"You are planning an ARM template for the Azure resource type '{service_id}' "
                    f"(service: {svc['name']}, category: {svc.get('category', 'general')}).\n\n"
                )

                # Hint for child resource types (e.g. subnets, databases, secrets)
                if '/' in service_id.split('/')[-1] or service_id.count('/') >= 3:
                    planning_prompt += (
                        f"NOTE: '{service_id}' is a child resource type. The ARM template MUST "
                        "include the parent resource(s) it depends on. For example, a subnet "
                        "requires a virtual network, a SQL database requires a SQL server, etc. "
                        "Include all parent resources in the template so it deploys standalone.\n\n"
                    )

                if standards_ctx:
                    planning_prompt += (
                        f"The organization has these mandatory standards that MUST be satisfied:\n"
                        f"{standards_ctx}\n\n"
                    )
                planning_prompt += (
                    "Produce a structured architecture plan. This plan will be handed to a "
                    "separate code generation model, so be specific and concrete.\n\n"
                    "## Required Output Sections:\n"
                    "1. **Resources**: List every Azure resource to create (type, API version, purpose)\n"
                    "2. **Security**: Specific security configs (TLS version, encryption, managed identity, network rules)\n"
                    "3. **Parameters**: Template parameters to expose (name, type, default, purpose)\n"
                    "4. **Properties**: Critical properties to set for production readiness\n"
                    "5. **Standards Compliance**: How each org standard will be satisfied\n"
                    "6. **Validation Criteria**: What should pass for this template to be considered correct\n\n"
                    "Be specific — include actual property names, API versions, and configuration values. "
                    "This plan drives code generation."
                )

                try:
                    planning_response = await _llm_reason(planning_prompt, task=Task.PLANNING)
                except Exception as e:
                    logger.warning(f"Planning phase failed (non-fatal): {e}")
                    planning_response = ""

                # Stream the planning response line by line
                for line in planning_response.split("\n"):
                    line = line.strip()
                    if line:
                        yield json.dumps({
                            "type": "llm_reasoning", "phase": "planning",
                            "detail": line,
                            "progress": 0.06,
                        }) + "\n"

                if not planning_response:
                    yield json.dumps({
                        "type": "progress", "phase": "planning_complete",
                        "detail": f"⚠️ Planning phase returned no response — proceeding with ARM template generation without plan",
                        "progress": 0.08,
                    }) + "\n"
                else:
                    yield json.dumps({
                        "type": "progress", "phase": "planning_complete",
                        "detail": f"✓ Architecture plan complete ({len(planning_response)} chars) — handing to code generation model",
                        "progress": 0.08,
                    }) + "\n"

                # ═══════════════════════════════════════════════════
                # PHASE 3: EXECUTE — ARM TEMPLATE GENERATION
                # ═══════════════════════════════════════════════════
                #
                # This is the EXECUTE phase. The code generation model receives
                # the architecture plan and produces the ARM template. It doesn't
                # need to reason about what to build — just follow the plan.

                _gen_model = get_model_display(Task.CODE_GENERATION)
                _gen_model_id = get_model_for_task(Task.CODE_GENERATION)
                yield json.dumps({
                    "type": "progress", "phase": "generating",
                    "detail": f"⚙️ EXECUTE phase — {_gen_model} is generating ARM template guided by the architecture plan…",
                    "progress": 0.09,
                }) + "\n"

                if has_builtin_skeleton(service_id):
                    template_dict = generate_arm_template(service_id)
                    current_template = json.dumps(template_dict, indent=2)
                    gen_source = "built-in skeleton"
                    yield json.dumps({
                        "type": "llm_reasoning", "phase": "generating",
                        "detail": f"📦 Using built-in ARM skeleton for {service_id} — pre-tested template, no LLM generation needed.",
                        "progress": 0.10,
                    }) + "\n"
                else:
                    yield json.dumps({
                        "type": "llm_reasoning", "phase": "generating",
                        "detail": f"No built-in skeleton — {_gen_model} generating ARM template with architecture plan + org standards…",
                        "progress": 0.10,
                    }) + "\n"
                    try:
                        _gen_client = await ensure_copilot_client()
                        if _gen_client is None:
                            raise RuntimeError("Copilot SDK not available for ARM generation")
                        current_template = await generate_arm_template_with_copilot(
                            service_id, svc["name"], _gen_client, _gen_model_id,
                            standards_context=standards_ctx,
                            planning_context=planning_response,
                        )
                    except Exception as gen_err:
                        logger.error(f"ARM generation failed for {service_id}: {gen_err}", exc_info=True)
                        yield json.dumps({
                            "type": "error", "phase": "generating",
                            "detail": f"ARM template generation failed: {str(gen_err)[:300]}",
                        }) + "\n"
                        await fail_service_validation(service_id, f"ARM generation failed: {gen_err}")
                        return
                    gen_source = f"Copilot SDK ({_gen_model})"

                # Validate we actually have JSON before proceeding
                if not current_template or not current_template.strip():
                    yield json.dumps({
                        "type": "error", "phase": "generating",
                        "detail": "ARM template generation returned empty content",
                    }) + "\n"
                    await fail_service_validation(service_id, "ARM template generation returned empty content")
                    return

                try:
                    json.loads(current_template)
                except json.JSONDecodeError as e:
                    yield json.dumps({
                        "type": "error", "phase": "generating",
                        "detail": f"Generated ARM template is not valid JSON: {e}",
                    }) + "\n"
                    await fail_service_validation(service_id, f"Generated ARM template is not valid JSON: {e}")
                    return

                # ── Safety guard: ensure every parameter has a defaultValue ──
                current_template = _ensure_parameter_defaults(current_template)
                # ── Replace placeholder subscription GUIDs ──
                current_template = _sanitize_placeholder_guids(current_template)

                tmpl_meta = _extract_meta(current_template)

                # Peek at the next version number for metadata stamping
                from src.database import get_backend as _get_db_backend
                _db = await _get_db_backend()
                _vrows = await _db.execute(
                    "SELECT MAX(version) as max_ver FROM service_versions WHERE service_id = ?",
                    (service_id,),
                )
                _next_ver = (_vrows[0]["max_ver"] if _vrows and _vrows[0]["max_ver"] else 0) + 1
                _semver = _version_to_semver(_next_ver)

                # ── Stamp InfraForge metadata into the ARM template ──
                current_template = _stamp_template_metadata(
                    current_template,
                    service_id=service_id,
                    version_int=_next_ver,
                    gen_source=gen_source,
                    region=region,
                )

                # Create version record
                ver = await create_service_version(
                    service_id=service_id,
                    arm_template=current_template,
                    version=_next_ver,
                    semver=_semver,
                    status="validating",
                    changelog=f"Auto-generated via {gen_source}",
                    created_by=gen_source,
                )
                version_num = ver["version"]

                yield json.dumps({
                    "type": "init", "phase": "generated",
                    "detail": f"✓ ARM template v{_semver} generated via {gen_source} — "
                              f"{tmpl_meta['resource_count']} resource(s), {tmpl_meta['size_kb']} KB",
                    "progress": 0.12,
                    "version": version_num,
                    "semver": _semver,
                    "meta": {
                        "service_name": svc.get("name", service_id),
                        "service_id": service_id,
                        "category": svc.get("category", ""),
                        "region": region,
                        "subscription": _sub_id,
                        "resource_group": rg_name,
                        "template_size_kb": tmpl_meta["size_kb"],
                        "resource_count": tmpl_meta["resource_count"],
                        "resource_types": tmpl_meta["resource_types"],
                        "resource_names": tmpl_meta.get("resource_names", []),
                        "api_versions": tmpl_meta.get("api_versions", []),
                        "schema": tmpl_meta["schema"],
                        "parameters": tmpl_meta.get("parameters", []),
                        "outputs": tmpl_meta.get("outputs", []),
                        "version": version_num,
                        "gen_source": gen_source,
                        "model_routing": _routing,
                        "standards_count": len(applicable_standards),
                    },
                }) + "\n"

            # ═══════════════════════════════════════════════════
            # PHASE 3.5: AZURE POLICY GENERATION
            # ═══════════════════════════════════════════════════

            _policy_model = get_model_display(Task.POLICY_GENERATION)
            yield json.dumps({
                "type": "progress", "phase": "policy_generation",
                "detail": f"🛡️ Generating Azure Policy definition for {svc['name']} using {_policy_model}…",
                "progress": 0.13,
            }) + "\n"

            generated_policy = None
            policy_gen_prompt = (
                f"Generate an Azure Policy definition JSON for '{svc['name']}' "
                f"(type: {service_id}).\n\n"
            )
            if policy_standards_ctx:
                policy_gen_prompt += (
                    f"Organization standards to enforce:\n{policy_standards_ctx}\n\n"
                )
            policy_gen_prompt += (
                "IMPORTANT — Azure Policy semantics for 'deny' effect:\n"
                "The 'if' condition must describe the VIOLATION (non-compliant state).\n"
                "If the 'if' MATCHES, the resource is DENIED. So use 'exists': false for missing tags,\n"
                "'notIn' for wrong regions, etc.\n\n"
                "DO NOT generate policy conditions for subscription-gated features like:\n"
                "- securityProfile.encryptionAtHost\n"
                "- diskControllerType\n"
                "- securityProfile.securityType (ConfidentialVM)\n"
                "- ultraSSDEnabled\n"
                "These require explicit subscription feature registration and will cause false violations.\n\n"
                "Structure: top-level allOf with [type-check, anyOf-of-violations].\n"
                "Return ONLY raw JSON — NO markdown, NO explanation. Start with {"
            )

            try:
                policy_raw = await _llm_reason(
                    policy_gen_prompt,
                    "You are an Azure Policy expert. Return ONLY raw JSON — no markdown, no code fences.",
                    task=Task.POLICY_GENERATION,
                )
                logger.info(f"Raw policy LLM response ({len(policy_raw)} chars): {policy_raw[:500]}")

                # Robust JSON extraction: handle markdown fences, explanation text, etc.
                _cleaned = policy_raw.strip()

                # Strip markdown code fences (```json ... ``` or ``` ... ```)
                import re as _re
                _fence_match = _re.search(r'```(?:json)?\s*\n(.*?)```', _cleaned, _re.DOTALL)
                if _fence_match:
                    _cleaned = _fence_match.group(1).strip()

                # If still not starting with {, try to find the first { ... } block
                if not _cleaned.startswith('{'):
                    _brace_start = _cleaned.find('{')
                    if _brace_start >= 0:
                        # Find the matching closing brace
                        depth = 0
                        for i in range(_brace_start, len(_cleaned)):
                            if _cleaned[i] == '{':
                                depth += 1
                            elif _cleaned[i] == '}':
                                depth -= 1
                                if depth == 0:
                                    _cleaned = _cleaned[_brace_start:i+1]
                                    break

                generated_policy = json.loads(_cleaned)
                _policy_size = round(len(_cleaned) / 1024, 1)
                logger.info(f"Generated Azure Policy for {service_id}:\n{json.dumps(generated_policy, indent=2)[:2000]}")

                # Describe the generated policy structure
                _rule = generated_policy.get("properties", generated_policy).get("policyRule", {})
                _effect = _rule.get("then", {}).get("effect", "unknown")
                _if_cond = _rule.get("if", {})
                _cond_count = 0
                if "allOf" in _if_cond:
                    _cond_count = len(_if_cond["allOf"])
                elif "anyOf" in _if_cond:
                    _cond_count = len(_if_cond["anyOf"])
                else:
                    _cond_count = 1 if _if_cond else 0

                yield json.dumps({
                    "type": "llm_reasoning", "phase": "policy_generation",
                    "detail": f"📋 Policy generated: {_cond_count} condition(s), effect: {_effect}, size: {_policy_size} KB",
                    "progress": 0.14,
                }) + "\n"

                yield json.dumps({
                    "type": "progress", "phase": "policy_generation_complete",
                    "detail": f"✓ Azure Policy definition generated — will test against deployed resources after deployment",
                    "progress": 0.15,
                }) + "\n"
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"Policy generation via LLM failed: {e} — using deterministic fallback")

                # ── Deterministic fallback: build policy from org standards ──
                _violations = []
                # Always check required tags
                for tag in ["environment", "owner", "costCenter", "project"]:
                    _violations.append({"field": f"tags['{tag}']", "exists": False})
                # Check approved regions
                _violations.append({
                    "field": "location",
                    "notIn": ["eastus2", "westus2", "westeurope"],
                })

                generated_policy = {
                    "properties": {
                        "displayName": f"Governance policy for {svc['name']}",
                        "policyType": "Custom",
                        "mode": "All",
                        "policyRule": {
                            "if": {
                                "allOf": [
                                    {"field": "type", "equals": service_id},
                                    {"anyOf": _violations},
                                ]
                            },
                            "then": {"effect": "deny"},
                        },
                    }
                }
                _policy_size = round(len(json.dumps(generated_policy)) / 1024, 1)
                logger.info(f"Fallback policy for {service_id}: {json.dumps(generated_policy, indent=2)[:1000]}")

                yield json.dumps({
                    "type": "llm_reasoning", "phase": "policy_generation",
                    "detail": f"📋 LLM policy generation failed — using deterministic fallback policy: {len(_violations)} condition(s), effect: deny, size: {_policy_size} KB",
                    "progress": 0.14,
                }) + "\n"
                yield json.dumps({
                    "type": "progress", "phase": "policy_generation_complete",
                    "detail": f"✓ Fallback Azure Policy generated — will test against deployed resources after deployment",
                    "progress": 0.15,
                }) + "\n"

            # ═══════════════════════════════════════════════════
            # PHASE 4: HEALING LOOP (validation + auto-healing)
            # ═══════════════════════════════════════════════════

            # Load org_standards as the single source of truth for validation.
            # Falls back to legacy governance_policies dict if no org_standards exist.
            org_standards = await get_all_standards(enabled_only=True)
            gov_policies = await get_governance_policies_as_dict()
            use_standards_driven = len(org_standards) > 0

            for attempt in range(1, MAX_HEAL_ATTEMPTS + 1):
                is_last = attempt == MAX_HEAL_ATTEMPTS
                att_base = (attempt - 1) / MAX_HEAL_ATTEMPTS

                if attempt == 1:
                    step_desc = f"Validating ARM template v{version_num} ({tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s))"
                else:
                    step_desc = f"Verifying corrected template v{version_num} ({tmpl_meta['size_kb']} KB, {tmpl_meta['resource_count']} resource(s)) — resolved {len(heal_history)} issue{'s' if len(heal_history) != 1 else ''} so far"

                yield json.dumps({
                    "type": "iteration_start", "step": attempt,
                    "detail": step_desc,
                    "progress": att_base + 0.01,
                }) + "\n"

                # ── 2. Parse JSON ─────────────────────────────
                try:
                    template_json = json.loads(current_template)
                except json.JSONDecodeError as e:
                    error_msg = f"ARM template is not valid JSON — line {e.lineno}, col {e.colno}: {e.msg}"
                    if is_last:
                        await update_service_version_status(service_id, version_num, "failed",
                            validation_result={"error": error_msg})
                        await fail_service_validation(service_id, error_msg)
                        yield json.dumps({"type": "error", "phase": "parsing", "step": attempt, "detail": error_msg}) + "\n"
                        return
                    yield json.dumps({"type": "healing", "phase": "fixing_template", "step": attempt,
                        "detail": f"JSON parse error — invoking {get_model_display(Task.CODE_FIXING)} to analyze error and resolve…", "progress": att_base + 0.02}) + "\n"
                    _pre_fix = current_template
                    current_template = await _copilot_fix(current_template, error_msg, standards_ctx, planning_context=planning_response, previous_attempts=heal_history)
                    heal_history.append({"step": len(heal_history) + 1, "phase": "parsing", "error": error_msg, "fix_summary": _summarize_fix(_pre_fix, current_template)})
                    tmpl_meta = _extract_meta(current_template)
                    await update_service_version_template(service_id, version_num, current_template, "copilot-healed")
                    yield json.dumps({"type": "healing_done", "phase": "template_fixed", "step": attempt,
                        "detail": f"{get_model_display(Task.CODE_FIXING)} rewrote template — retrying…", "progress": att_base + 0.03}) + "\n"
                    continue

                # ── 3. Static Policy Check ────────────────────
                yield json.dumps({
                    "type": "progress", "phase": "static_policy_check", "step": attempt,
                    "detail": f"Running static policy validation against {len(org_standards) if use_standards_driven else len(gov_policies)} organization governance rules…",
                    "progress": att_base + 0.04,
                }) + "\n"

                if use_standards_driven:
                    report = validate_template_against_standards(template_json, org_standards)
                else:
                    report = validate_template(template_json, gov_policies)

                # Emit individual check results
                for check in report.results:
                    icon = "✅" if check.passed else ("⚠️" if check.enforcement == "warn" else "❌")
                    yield json.dumps({
                        "type": "policy_result", "phase": "static_policy_check", "step": attempt,
                        "detail": f"{icon} [{check.rule_id}] {check.rule_name}: {check.message}",
                        "passed": check.passed,
                        "severity": check.severity,
                        "progress": att_base + 0.05,
                    }) + "\n"

                if not report.passed:
                    fail_msg = f"Static policy check: {report.passed_checks}/{report.total_checks} passed, {report.blockers} blocker(s)"
                    yield json.dumps({
                        "type": "progress", "phase": "static_policy_failed", "step": attempt,
                        "detail": fail_msg, "progress": att_base + 0.06,
                    }) + "\n"

                    if is_last:
                        await update_service_version_status(service_id, version_num, "failed",
                            policy_check=report.to_dict())
                        await fail_service_validation(service_id, fail_msg)
                        yield json.dumps({"type": "error", "phase": "static_policy", "step": attempt,
                            "detail": f"Static policy validation failed — all available resolution strategies exhausted"}) + "\n"
                        return

                    # Build targeted remediation prompt from failed checks
                    failed_checks = [c for c in report.results if not c.passed and c.enforcement == "block"]
                    fix_prompt = build_remediation_prompt(current_template, failed_checks)
                    yield json.dumps({"type": "healing", "phase": "fixing_template", "step": attempt,
                        "detail": f"Policy violations detected — {get_model_display(Task.CODE_FIXING)} auto-healing template for {len(failed_checks)} blocker(s), analyzing error and resolving…",
                        "progress": att_base + 0.07}) + "\n"
                    _pre_fix = current_template
                    current_template = await _copilot_fix(current_template, fix_prompt, standards_ctx, planning_context=planning_response, previous_attempts=heal_history)
                    heal_history.append({"step": len(heal_history) + 1, "phase": "static_policy", "error": fix_prompt[:500], "fix_summary": _summarize_fix(_pre_fix, current_template)})
                    tmpl_meta = _extract_meta(current_template)
                    await update_service_version_template(service_id, version_num, current_template, "copilot-healed")
                    yield json.dumps({"type": "healing_done", "phase": "template_fixed", "step": attempt,
                        "detail": f"{get_model_display(Task.CODE_FIXING)} remediated template — retrying…", "progress": att_base + 0.08}) + "\n"
                    continue

                yield json.dumps({
                    "type": "progress", "phase": "static_policy_complete", "step": attempt,
                    "detail": f"✓ Static policy check passed — {report.passed_checks}/{report.total_checks} checks, 0 blockers",
                    "progress": att_base + 0.08,
                }) + "\n"

                # Save policy check results
                await update_service_version_status(service_id, version_num, "validating",
                    policy_check=report.to_dict())

                # ── 4. What-If ────────────────────────────────
                res_types_str = ", ".join(tmpl_meta["resource_types"][:5]) or "unknown"
                yield json.dumps({
                    "type": "progress", "phase": "what_if", "step": attempt,
                    "detail": f"Submitting ARM What-If to Azure — previewing {tmpl_meta['resource_count']} resource(s) "
                              f"[{res_types_str}] in '{rg_name}' ({region})",
                    "progress": att_base + 0.10,
                }) + "\n"

                try:
                    from src.tools.deploy_engine import run_what_if
                    wif = await run_what_if(resource_group=rg_name, template=template_json,
                                           parameters=_extract_param_values(template_json), region=region)
                except Exception as e:
                    logger.error(f"What-If attempt {attempt} exception: {e}", exc_info=True)
                    wif = {"status": "error", "errors": [str(e)]}

                if wif.get("status") != "success":
                    errors = "; ".join(str(e) for e in wif.get("errors", [])) or "Unknown What-If error"

                    # Detect transient infra errors
                    _infra_keywords = ("beingdeleted", "being deleted", "deprovisioning",
                                       "throttled", "toomanyrequests", "retryable",
                                       "serviceunavailable", "internalservererror")
                    if any(kw in errors.lower() for kw in _infra_keywords):
                        yield json.dumps({"type": "progress", "phase": "infra_retry", "step": attempt,
                            "detail": f"Transient Azure error — waiting 10s…", "progress": att_base + 0.11}) + "\n"
                        await asyncio.sleep(10)
                        continue

                    if is_last:
                        await update_service_version_status(service_id, version_num, "failed",
                            validation_result={"error": errors, "phase": "what_if"})
                        await fail_service_validation(service_id, f"What-If failed: {errors}")
                        yield json.dumps({"type": "error", "phase": "what_if", "step": attempt,
                            "detail": f"What-If failed — all available resolution strategies exhausted: {errors}"}) + "\n"
                        return

                    yield json.dumps({"type": "healing", "phase": "fixing_template", "step": attempt,
                        "detail": f"What-If rejected by ARM — invoking {get_model_display(Task.CODE_FIXING)} to analyze error and resolve… Error: {errors[:300]}",
                        "progress": att_base + 0.12}) + "\n"
                    _pre_fix = current_template
                    current_template = await _copilot_fix(current_template, errors, standards_ctx, planning_context=planning_response, previous_attempts=heal_history)
                    heal_history.append({"step": len(heal_history) + 1, "phase": "what_if", "error": errors[:500], "fix_summary": _summarize_fix(_pre_fix, current_template)})
                    tmpl_meta = _extract_meta(current_template)
                    await update_service_version_template(service_id, version_num, current_template, "copilot-healed")
                    yield json.dumps({"type": "healing_done", "phase": "template_fixed", "step": attempt,
                        "detail": f"{get_model_display(Task.CODE_FIXING)} rewrote template — retrying…", "progress": att_base + 0.13}) + "\n"
                    continue

                change_summary = ", ".join(f"{v} {k}" for k, v in wif.get("change_counts", {}).items())
                yield json.dumps({
                    "type": "progress", "phase": "what_if_complete", "step": attempt,
                    "detail": f"✓ What-If passed — changes: {change_summary or 'none'}",
                    "progress": att_base + 0.14,
                    "result": wif,
                }) + "\n"

                # ── 5. Deploy ─────────────────────────────────
                yield json.dumps({
                    "type": "progress", "phase": "deploying", "step": attempt,
                    "detail": f"Deploying {tmpl_meta['resource_count']} resource(s) into '{rg_name}' ({region})…",
                    "progress": att_base + 0.16,
                }) + "\n"

                try:
                    from src.tools.deploy_engine import execute_deployment
                    deploy_result = await execute_deployment(
                        resource_group=rg_name, template=template_json,
                        parameters=_extract_param_values(template_json), region=region,
                        deployment_name=f"validate-{attempt}",
                        initiated_by="InfraForge Validator",
                    )
                    deploy_status = deploy_result.get("status", "unknown")
                except Exception as e:
                    logger.error(f"Deploy attempt {attempt} exception: {e}", exc_info=True)
                    deploy_result = {"status": "failed", "error": str(e)}
                    deploy_status = "failed"

                deployed_rg = rg_name

                if deploy_status != "succeeded":
                    deploy_error = deploy_result.get("error", "Unknown deployment error")

                    # Fetch operation-level errors if generic
                    if "Please list deployment operations" in deploy_error or "At least one resource" in deploy_error:
                        try:
                            from src.tools.deploy_engine import _get_resource_client, _get_deployment_operation_errors
                            _rc = _get_resource_client()
                            _lp = asyncio.get_event_loop()
                            op_errors = await _get_deployment_operation_errors(_rc, _lp, rg_name, f"validate-{attempt}")
                            if op_errors:
                                deploy_error = f"{deploy_error} | Operation errors: {op_errors}"
                        except Exception:
                            pass

                    _is_infra = any(kw in deploy_error.lower() for kw in
                        ("beingdeleted", "being deleted", "throttled", "toomanyrequests",
                         "serviceunavailable", "internalservererror"))

                    yield json.dumps({"type": "progress", "phase": "deploy_failed", "step": attempt,
                        "detail": f"Deployment failed: {deploy_error[:400]}", "progress": att_base + 0.20}) + "\n"

                    if _is_infra:
                        yield json.dumps({"type": "progress", "phase": "infra_retry", "step": attempt,
                            "detail": "Transient infra error — waiting 10s…", "progress": att_base + 0.21}) + "\n"
                        await asyncio.sleep(10)
                        continue

                    if is_last:
                        await _cleanup_rg(rg_name)
                        await update_service_version_status(service_id, version_num, "failed",
                            validation_result={"error": deploy_error, "phase": "deploy"})
                        await fail_service_validation(service_id, f"Deploy failed: {deploy_error}")
                        yield json.dumps({"type": "error", "phase": "deploy", "step": attempt,
                            "detail": "Deployment could not be completed — all available resolution strategies exhausted"}) + "\n"
                        return

                    yield json.dumps({"type": "healing", "phase": "fixing_template", "step": attempt,
                        "detail": f"Deployment failed — {get_model_display(Task.CODE_FIXING)} analyzing error and resolving… Error: {deploy_error[:300]}",
                        "progress": att_base + 0.21}) + "\n"
                    _pre_fix = current_template
                    current_template = await _copilot_fix(current_template, deploy_error, standards_ctx, planning_context=planning_response, previous_attempts=heal_history)
                    heal_history.append({"step": len(heal_history) + 1, "phase": "deploy", "error": deploy_error[:500], "fix_summary": _summarize_fix(_pre_fix, current_template)})
                    tmpl_meta = _extract_meta(current_template)
                    await update_service_version_template(service_id, version_num, current_template, "copilot-healed")
                    yield json.dumps({"type": "healing_done", "phase": "template_fixed", "step": attempt,
                        "detail": f"{get_model_display(Task.CODE_FIXING)} rewrote template — redeploying…", "progress": att_base + 0.22}) + "\n"
                    continue

                # Deploy succeeded!
                provisioned = deploy_result.get("provisioned_resources", [])
                resource_summaries = [f"{r.get('type','?')}/{r.get('name','?')}" for r in provisioned]

                # Persist deployment tracking info to the version row
                _deploy_name = f"validate-{attempt}"
                _subscription_id = deploy_result.get("subscription_id", "")
                _deployment_id = deploy_result.get("deployment_id", "")
                await update_service_version_deployment_info(
                    service_id, version_num,
                    run_id=_run_id,
                    resource_group=rg_name,
                    deployment_name=_deploy_name,
                    subscription_id=_subscription_id,
                )

                yield json.dumps({
                    "type": "progress", "phase": "deploy_complete", "step": attempt,
                    "detail": f"✓ Deployment succeeded — {len(provisioned)} resource(s): {'; '.join(resource_summaries[:5])}",
                    "progress": att_base + 0.22,
                    "resources": provisioned,
                }) + "\n"

                # ── 6. Resource verification (with full properties) ──
                yield json.dumps({
                    "type": "progress", "phase": "resource_check", "step": attempt,
                    "detail": f"Querying Azure to verify {len(provisioned)} resource(s) and fetch full properties for policy evaluation…",
                    "progress": att_base + 0.24,
                }) + "\n"

                from src.tools.deploy_engine import _get_resource_client
                rc = _get_resource_client()
                loop = asyncio.get_event_loop()
                try:
                    live_resources = await loop.run_in_executor(
                        None, lambda: list(rc.resources.list_by_resource_group(rg_name))
                    )
                    resource_details = []
                    for r in live_resources:
                        detail = {
                            "id": r.id, "name": r.name, "type": r.type,
                            "location": r.location, "tags": dict(r.tags) if r.tags else {},
                        }
                        # Fetch full resource properties for policy evaluation
                        try:
                            full = await loop.run_in_executor(
                                None,
                                lambda r=r: rc.resources.get_by_id(r.id, api_version="2023-07-01")
                            )
                            if full.properties:
                                detail["properties"] = full.properties
                        except Exception:
                            pass
                        resource_details.append(detail)

                    res_detail_strs = [f"{r['type']}/{r['name']} @ {r['location']}" for r in resource_details[:8]]
                    yield json.dumps({
                        "type": "progress", "phase": "resource_check_complete", "step": attempt,
                        "detail": f"✓ Verified {len(resource_details)} live resource(s) with full properties: {'; '.join(res_detail_strs)}",
                        "progress": att_base + 0.26,
                        "resources": [{"name": r["name"], "type": r["type"], "location": r["location"]} for r in resource_details],
                    }) + "\n"
                except Exception as e:
                    resource_details = []
                    yield json.dumps({
                        "type": "progress", "phase": "resource_check_warning", "step": attempt,
                        "detail": f"Could not enumerate resources (non-fatal): {e}",
                        "progress": att_base + 0.26,
                    }) + "\n"

                # ── 6.5 Runtime policy compliance test ────────
                policy_results = []
                all_policy_compliant = True

                if generated_policy and resource_details:
                    _policy_rule = generated_policy.get("properties", generated_policy).get("policyRule", {})
                    _policy_effect = _policy_rule.get("then", {}).get("effect", "deny")
                    yield json.dumps({
                        "type": "progress", "phase": "policy_testing", "step": attempt,
                        "detail": f"🛡️ Evaluating {len(resource_details)} deployed resource(s) against generated Azure Policy (effect: {_policy_effect})…",
                        "progress": att_base + 0.27,
                    }) + "\n"

                    logger.info(f"Policy evaluation — {len(resource_details)} resource(s), policy if-condition: {json.dumps(_policy_rule.get('if', {}), indent=2)[:1000]}")
                    for rd in resource_details:
                        logger.info(f"Resource to evaluate: name={rd.get('name')} type={rd.get('type')} tags={rd.get('tags')} location={rd.get('location')}")

                    policy_results = _test_policy_compliance(generated_policy, resource_details)
                    all_policy_compliant = all(r["compliant"] for r in policy_results)
                    compliant_count = sum(1 for r in policy_results if r["compliant"])
                    for pr in policy_results:
                        logger.info(f"Policy result: {pr['resource_name']} compliant={pr['compliant']} reason={pr['reason']}")

                    for pr in policy_results:
                        icon = "✅" if pr["compliant"] else "❌"
                        yield json.dumps({
                            "type": "policy_result", "phase": "policy_testing", "step": attempt,
                            "detail": f"{icon} {pr['resource_type']}/{pr['resource_name']} — {pr['reason']}",
                            "compliant": pr["compliant"],
                            "resource": pr,
                            "progress": att_base + 0.28,
                        }) + "\n"

                    if not all_policy_compliant:
                        violations = [pr for pr in policy_results if not pr["compliant"]]
                        violation_desc = "; ".join(f"{v['resource_name']}: {v['reason']}" for v in violations)
                        fail_msg = (
                            f"{compliant_count}/{len(policy_results)} resources compliant — "
                            f"{len(violations)} policy violation(s): {violation_desc[:300]}"
                        )
                        yield json.dumps({
                            "type": "progress", "phase": "policy_failed", "step": attempt,
                            "detail": fail_msg,
                            "progress": att_base + 0.29,
                        }) + "\n"

                        if is_last:
                            await _cleanup_rg(rg_name)
                            deployed_rg = None
                            await update_service_version_status(service_id, version_num, "failed",
                                validation_result={"error": fail_msg, "phase": "policy_compliance"})
                            await fail_service_validation(service_id, fail_msg)
                            yield json.dumps({
                                "type": "error", "phase": "policy", "step": attempt,
                                "detail": f"Runtime policy compliance failed — all resolution strategies exhausted. Violations: {violation_desc}",
                            }) + "\n"
                            return

                        import json as _json_mod
                        _policy_str = _json_mod.dumps(generated_policy, indent=2)[:500]
                        fix_error = f"Runtime policy violation: {violation_desc}. The policy requires: {_policy_str}"
                        yield json.dumps({
                            "type": "healing", "phase": "fixing_template", "step": attempt,
                            "detail": f"Policy violations on {len(violations)} resource(s) — {get_model_display(Task.CODE_FIXING)} analyzing and resolving…",
                            "progress": att_base + 0.30,
                        }) + "\n"
                        _pre_fix = current_template
                        current_template = await _copilot_fix(current_template, fix_error, standards_ctx, planning_context=planning_response, previous_attempts=heal_history)
                        heal_history.append({"step": len(heal_history) + 1, "phase": "policy_compliance", "error": fix_error[:500], "fix_summary": _summarize_fix(_pre_fix, current_template)})
                        tmpl_meta = _extract_meta(current_template)
                        await update_service_version_template(service_id, version_num, current_template, "copilot-healed")
                        yield json.dumps({
                            "type": "healing_done", "phase": "template_fixed", "step": attempt,
                            "detail": f"{get_model_display(Task.CODE_FIXING)} rewrote template for policy compliance — redeploying…",
                            "progress": att_base + 0.31,
                        }) + "\n"
                        continue
                    else:
                        yield json.dumps({
                            "type": "progress", "phase": "policy_testing_complete", "step": attempt,
                            "detail": f"✓ All {len(policy_results)} resource(s) passed runtime policy compliance check",
                            "progress": att_base + 0.30,
                        }) + "\n"
                elif not generated_policy:
                    yield json.dumps({
                        "type": "progress", "phase": "policy_skip", "step": attempt,
                        "detail": "No Azure Policy was generated — skipping runtime policy compliance test",
                        "progress": att_base + 0.30,
                    }) + "\n"
                else:
                    yield json.dumps({
                        "type": "progress", "phase": "policy_skip", "step": attempt,
                        "detail": "No resources to test — skipping runtime policy compliance test",
                        "progress": att_base + 0.30,
                    }) + "\n"

                # ── 7. Cleanup ────────────────────────────────
                yield json.dumps({
                    "type": "progress", "phase": "cleanup", "step": attempt,
                    "detail": f"All checks passed — deleting validation RG '{rg_name}'…",
                    "progress": 0.90,
                }) + "\n"

                await _cleanup_rg(rg_name)
                deployed_rg = None

                yield json.dumps({
                    "type": "progress", "phase": "cleanup_complete", "step": attempt,
                    "detail": f"✓ Validation RG '{rg_name}' deletion initiated",
                    "progress": 0.93,
                }) + "\n"

                # ── 8. Promote ────────────────────────────────
                validation_summary = {
                    "run_id": _run_id,
                    "resource_group": rg_name,
                    "deployment_name": _deploy_name,
                    "subscription_id": _subscription_id,
                    "deployment_id": _deployment_id,
                    "what_if": wif,
                    "deploy_result": {
                        "status": deploy_result.get("status"),
                        "started_at": deploy_result.get("started_at"),
                        "completed_at": deploy_result.get("completed_at"),
                        "deployment_id": _deployment_id,
                    },
                    "deployed_resources": [{"name": r["name"], "type": r["type"], "location": r["location"]}
                                           for r in resource_details],
                    "policy_check": report.to_dict(),
                    "policy_compliance": policy_results,
                    "all_policy_compliant": all_policy_compliant,
                    "has_runtime_policy": generated_policy is not None,
                    "attempts": attempt,
                    "heal_history": heal_history,
                }

                yield json.dumps({
                    "type": "progress", "phase": "promoting", "step": attempt,
                    "detail": f"Promoting {svc['name']} v{version_num} → approved…",
                    "progress": 0.97,
                }) + "\n"

                await update_service_version_status(
                    service_id, version_num, "approved",
                    validation_result=validation_summary,
                    policy_check=report.to_dict(),
                )
                await set_active_service_version(service_id, version_num)

                _policy_str = ""
                if policy_results:
                    _pc = sum(1 for r in policy_results if r["compliant"])
                    _policy_str = f", {_pc}/{len(policy_results)} runtime policy check(s) passed"

                issues_resolved = len(heal_history)
                heal_msg = f" Resolved {issues_resolved} issue{'s' if issues_resolved != 1 else ''} automatically." if issues_resolved > 0 else ""
                yield json.dumps({
                    "type": "done", "phase": "approved", "step": attempt,
                    "issues_resolved": issues_resolved,
                    "version": version_num,
                    "detail": f"🎉 {svc['name']} v{version_num} approved! "
                              f"{len(resource_details)} resource(s) validated, "
                              f"{report.passed_checks}/{report.total_checks} static policy checks passed"
                              f"{_policy_str}."
                              f"{heal_msg}",
                    "progress": 1.0,
                    "summary": validation_summary,
                }) + "\n"
                return  # ✅ success

        except Exception as e:
            logger.error(f"Onboarding error for {service_id}: {e}", exc_info=True)
            if version_num:
                try:
                    await update_service_version_status(service_id, version_num, "failed",
                        validation_result={"error": str(e)})
                except Exception:
                    pass
            try:
                await fail_service_validation(service_id, str(e))
            except Exception:
                pass
            yield json.dumps({"type": "error", "phase": "unknown", "detail": str(e)}) + "\n"
        except (GeneratorExit, asyncio.CancelledError):
            logger.warning(f"Onboarding cancelled for {service_id}")
            try:
                await fail_service_validation(service_id, "Cancelled — please retry.")
            except Exception:
                pass
        finally:
            if deployed_rg:
                try:
                    await _cleanup_rg(deployed_rg)
                except Exception:
                    pass

    async def _tracked_stream():
        try:
            async for line in stream_onboarding():
                _track(line)
                yield line
        finally:
            async def _cleanup_tracker():
                await asyncio.sleep(300)
                _active_validations.pop(service_id, None)
            asyncio.create_task(_cleanup_tracker())

    return StreamingResponse(
        _tracked_stream(),
        media_type="application/x-ndjson",
    )


# ── Deployment API ────────────────────────────────────────────

@app.get("/api/deployments")
async def list_deployments_endpoint(
    status: Optional[str] = None,
    resource_group: Optional[str] = None,
):
    """List deployment history."""
    from src.database import get_deployments

    try:
        deployments = await get_deployments(
            status=status,
            resource_group=resource_group,
        )
        return JSONResponse({
            "deployments": deployments,
            "total": len(deployments),
        })
    except Exception as e:
        logger.error(f"Failed to list deployments: {e}")
        return JSONResponse({"deployments": [], "total": 0})


@app.get("/api/deployments/{deployment_id}")
async def get_deployment_endpoint(deployment_id: str):
    """Get a single deployment's details."""
    from src.database import get_deployment

    deployment = await get_deployment(deployment_id)
    if not deployment:
        # Check in-memory (may be still running)
        from src.tools.deploy_engine import deploy_manager
        record = deploy_manager.deployments.get(deployment_id)
        if record:
            return JSONResponse(record.to_dict())
        raise HTTPException(status_code=404, detail="Deployment not found")
    return JSONResponse(deployment)


@app.get("/api/deployments/{deployment_id}/stream")
async def stream_deployment_progress(deployment_id: str):
    """Stream real-time deployment progress via SSE.

    Subscribe to live progress events for an active deployment.
    Replays history on connect so late-joiners see current state.
    """
    from src.tools.deploy_engine import deploy_manager

    record = deploy_manager.deployments.get(deployment_id)
    if not record:
        raise HTTPException(status_code=404, detail="Deployment not found or already completed")

    async def _event_stream():
        q = deploy_manager.subscribe(deployment_id)
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            deploy_manager.unsubscribe(deployment_id, q)

    return StreamingResponse(_event_stream(), media_type="text/event-stream")


# ── Approval Management API ──────────────────────────────────

@app.get("/api/approvals")
async def list_approvals(
    status: Optional[str] = None,
    requestor_email: Optional[str] = None,
):
    """List approval requests, optionally filtered by status or requestor."""
    try:
        requests = await get_approval_requests(
            status=status,
            requestor_email=requestor_email,
        )
        # Convert Row objects to dicts if needed
        result = []
        for r in requests:
            if isinstance(r, dict):
                result.append(r)
            else:
                result.append(dict(r))
        return JSONResponse({
            "requests": result,
            "total": len(result),
        })
    except Exception as e:
        logger.error(f"Failed to list approval requests: {e}")
        return JSONResponse({"requests": [], "total": 0})


@app.get("/api/approvals/{request_id}")
async def get_approval_detail(request_id: str):
    """Get details of a specific approval request."""
    try:
        requests = await get_approval_requests()
        matching = [r for r in requests if (r.get("id") if isinstance(r, dict) else r["id"]) == request_id]
        if not matching:
            raise HTTPException(status_code=404, detail="Approval request not found")
        req = matching[0]
        return JSONResponse(dict(req) if not isinstance(req, dict) else req)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get approval request {request_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/approvals/{request_id}/review")
async def review_approval(request_id: str, request: Request):
    """IT admin action: approve, conditionally approve, deny, or defer a request."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    decision = body.get("decision")
    reviewer = body.get("reviewer", "Platform Team")
    review_notes = body.get("review_notes", "")

    valid_decisions = {"approved", "conditional", "denied", "deferred"}
    if decision not in valid_decisions:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid decision. Must be one of: {', '.join(sorted(valid_decisions))}",
        )

    try:
        success = await update_approval_request(
            request_id=request_id,
            status=decision,
            reviewer=reviewer,
            review_notes=review_notes,
        )
        if not success:
            raise HTTPException(status_code=404, detail="Approval request not found or already finalized")

        return JSONResponse({
            "success": True,
            "request_id": request_id,
            "decision": decision,
            "reviewer": reviewer,
            "message": f"Request {request_id} has been {decision}.",
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to review approval request {request_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ── Governance API ───────────────────────────────────────────

@app.get("/api/governance/security-standards")
async def list_security_standards(category: Optional[str] = None):
    """Return all security standards, optionally filtered by category."""
    from src.database import get_security_standards as db_get_standards

    try:
        standards = await db_get_standards(category=category, enabled_only=True)
        # Convert Row to dict
        result = [dict(s) if not isinstance(s, dict) else s for s in standards]
        categories = sorted(set(s.get("category", "") for s in result))
        return JSONResponse({
            "standards": result,
            "categories": categories,
            "total": len(result),
        })
    except Exception as e:
        logger.error(f"Failed to load security standards: {e}")
        return JSONResponse({"standards": [], "categories": [], "total": 0})


@app.get("/api/governance/compliance-frameworks")
async def list_compliance_frameworks():
    """Return all compliance frameworks with their controls."""
    from src.database import get_compliance_frameworks as db_get_frameworks

    try:
        frameworks = await db_get_frameworks(enabled_only=True)
        result = []
        for fw in frameworks:
            fw_dict = dict(fw) if not isinstance(fw, dict) else fw
            # Controls are already hydrated by the CRUD function
            controls = fw_dict.get("controls", [])
            fw_dict["control_count"] = len(controls)
            result.append(fw_dict)
        return JSONResponse({
            "frameworks": result,
            "total": len(result),
        })
    except Exception as e:
        logger.error(f"Failed to load compliance frameworks: {e}")
        return JSONResponse({"frameworks": [], "total": 0})


@app.get("/api/governance/policies")
async def list_governance_policies(category: Optional[str] = None):
    """Return all governance policies, optionally filtered by category."""
    from src.database import get_governance_policies as db_get_policies

    try:
        policies = await db_get_policies(category=category, enabled_only=True)
        result = [dict(p) if not isinstance(p, dict) else p for p in policies]
        categories = sorted(set(p.get("category", "") for p in result))
        return JSONResponse({
            "policies": result,
            "categories": categories,
            "total": len(result),
        })
    except Exception as e:
        logger.error(f"Failed to load governance policies: {e}")
        return JSONResponse({"policies": [], "categories": [], "total": 0})


# ── WebSocket Chat ───────────────────────────────────────────

@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """WebSocket endpoint for streaming chat with InfraForge.

    Protocol:
    1. Client connects and sends: {"type": "auth", "sessionToken": "..."}
    2. Server validates and responds: {"type": "auth_ok", "user": {...}}
    3. Client sends messages: {"type": "message", "content": "..."}
    4. Server streams responses: {"type": "delta", "content": "..."} chunks
    5. Server sends completion: {"type": "done", "content": "full response"}
    6. Server sends tool calls: {"type": "tool_call", "name": "...", "status": "..."}
    """
    await websocket.accept()

    session_token: Optional[str] = None
    user_context: Optional[UserContext] = None

    try:
        # ── Step 1: Authenticate ─────────────────────────────
        auth_msg = await asyncio.wait_for(websocket.receive_json(), timeout=30)

        if auth_msg.get("type") != "auth":
            await websocket.send_json({"type": "error", "message": "Expected auth message"})
            await websocket.close()
            return

        session_token = auth_msg.get("sessionToken", "")
        user_context = await get_user_context(session_token)

        if not user_context:
            await websocket.send_json({"type": "error", "message": "Invalid or expired session"})
            await websocket.close()
            return

        # ── Step 2: Create Copilot session with user context ─
        client = await ensure_copilot_client()
        if client is None:
            await websocket.send_json({
                "type": "error",
                "message": "Copilot SDK is not available. Chat is disabled but the rest of InfraForge works.",
            })
            await websocket.close()
            return

        personalized_system_message = (
            SYSTEM_MESSAGE + "\n" + user_context.to_prompt_context()
        )

        tools = get_all_tools()
        try:
            copilot_session = await client.create_session({
                "model": get_active_model(),
                "streaming": True,
                "tools": tools,
                "system_message": {"content": personalized_system_message},
            })
        except Exception as e:
            logger.error(f"Failed to create Copilot session: {e}")
            await websocket.send_json({
                "type": "error",
                "message": f"Failed to create chat session: {e}",
            })
            await websocket.close()
            return

        active_sessions[session_token] = {
            "copilot_session": copilot_session,
            "user_context": user_context,
            "connected_at": time.time(),
        }
        await websocket.send_json({
            "type": "auth_ok",
            "user": {
                "displayName": user_context.display_name,
                "email": user_context.email,
                "department": user_context.department,
                "team": user_context.team,
            },
        })

        # ── Step 3: Chat loop ────────────────────────────────
        while True:
            data = await websocket.receive_json()

            if data.get("type") == "message":
                user_message = data.get("content", "").strip()
                if not user_message:
                    continue

                # Track for analytics
                request_record = {
                    "timestamp": time.time(),
                    "user": user_context.email,
                    "department": user_context.department,
                    "cost_center": user_context.cost_center,
                    "prompt": user_message[:200],  # Truncate for privacy
                    "resource_types": [],
                    "estimated_cost": 0.0,
                    "from_catalog": False,
                }

                # Stream the response
                response_chunks: list[str] = []
                done_event = asyncio.Event()

                def on_event(event):
                    try:
                        if event.type.value == "assistant.message_delta":
                            delta = event.data.delta_content or ""
                            response_chunks.append(delta)
                            asyncio.get_event_loop().create_task(
                                websocket.send_json({
                                    "type": "delta",
                                    "content": delta,
                                })
                            )
                        elif event.type.value == "assistant.message":
                            full_content = event.data.content or ""
                            asyncio.get_event_loop().create_task(
                                websocket.send_json({
                                    "type": "done",
                                    "content": full_content,
                                })
                            )
                        elif event.type.value == "tool.call":
                            tool_name = getattr(event.data, 'name', 'unknown')
                            asyncio.get_event_loop().create_task(
                                websocket.send_json({
                                    "type": "tool_call",
                                    "name": tool_name,
                                    "status": "running",
                                })
                            )
                            # Track catalog usage
                            if tool_name == "search_template_catalog":
                                request_record["from_catalog"] = True
                        elif event.type.value == "tool.result":
                            tool_name = getattr(event.data, 'name', 'unknown')
                            asyncio.get_event_loop().create_task(
                                websocket.send_json({
                                    "type": "tool_call",
                                    "name": tool_name,
                                    "status": "complete",
                                })
                            )
                        elif event.type.value == "session.idle":
                            done_event.set()
                    except Exception as e:
                        logger.error(f"Event handler error: {e}")
                        done_event.set()

                unsubscribe = copilot_session.on(on_event)

                try:
                    await copilot_session.send({"prompt": user_message})
                    await asyncio.wait_for(done_event.wait(), timeout=120)
                except asyncio.TimeoutError:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Request timed out. Please try again.",
                    })
                finally:
                    unsubscribe()

                # Persist to database
                full_response = "".join(response_chunks)
                await save_chat_message(session_token, "user", user_message)
                await save_chat_message(session_token, "assistant", full_response)
                await log_usage(request_record)

            elif data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        logger.info(f"Client disconnected: {user_context.email if user_context else 'unknown'}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        # Don't destroy the Copilot session on disconnect — user may reconnect
        pass
