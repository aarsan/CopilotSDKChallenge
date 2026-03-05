"""
InfraForge Pipeline Helpers — shared utilities for all pipeline handlers.

Extracted from ``web.py`` to eliminate duplication across the service
onboarding, template validation, and template deploy pipelines.

All helpers are importable — they no longer rely on endpoint closures.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("infraforge.pipeline")

# ══════════════════════════════════════════════════════════════
# PARAMETER DEFAULTS
# ══════════════════════════════════════════════════════════════

def _build_param_defaults() -> dict[str, object]:
    """Build parameter defaults using real Azure context where possible."""
    sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "00000000-0000-0000-0000-000000000000")
    return {
        "resourceName": "infraforge-resource",
        "location": "[resourceGroup().location]",
        "environment": "dev",
        "projectName": "infraforge",
        "ownerEmail": "platform-team@company.com",
        "costCenter": "IT-0001",
        "subscriptionId": sub_id,
        "subscription_id": sub_id,
        "targetSubscriptionId": sub_id,
        "linkedSubscriptionId": sub_id,
        "remoteSubscriptionId": sub_id,
        "peerSubscriptionId": sub_id,
        "vnetName": "infraforge-vnet",
        "subnetName": "default",
        "nsgName": "infraforge-nsg",
        "storageAccountName": "ifrgvalidation",
        "keyVaultName": "infraforge-kv",
        "dnsZoneName": "infraforge-demo.com",
        "dnszones": "infraforge-demo.com",
        "dnsZone": "infraforge-demo.com",
        "zoneName": "infraforge-demo.com",
        "domainName": "infraforge-demo.com",
        "domain": "infraforge-demo.com",
        "hostName": "app.infraforge-demo.com",
        "fqdn": "app.infraforge-demo.com",
        "sharedKey": "InfraForgeVal1dation!",
        "adminPassword": "InfraForge#Val1d!",
        "adminUsername": "azureadmin",
        "sshPublicKey": "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC7 validation-only@infraforge",
    }

PARAM_DEFAULTS: dict[str, object] = _build_param_defaults()


def _constrained_fallback(pname: str, pdef: dict) -> object:
    """Generate a fallback value that respects ARM parameter constraints.

    Checks ``allowedValues``, ``type``, ``maxLength``, and ``minLength``
    so the generated placeholder won't immediately fail Azure validation.
    """
    # --- allowedValues: just pick the first one --------------------------
    allowed = pdef.get("allowedValues")
    if isinstance(allowed, list) and allowed:
        return allowed[0]

    # --- type-aware defaults ---------------------------------------------
    ptype = (pdef.get("type") or "string").lower()
    if ptype == "int":
        min_v = pdef.get("minValue", 1)
        return min_v if isinstance(min_v, int) else 1
    if ptype == "bool":
        return False
    if ptype in ("array", "list"):
        return []
    if ptype == "object":
        return {}
    if ptype == "securestring":
        plow = pname.lower()
        if "ssh" in plow or "publickey" in plow:
            return "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC7 validation-only@infraforge"
        return "InfraForge#Val1d!"

    # --- string: respect maxLength / minLength ---------------------------
    plow = pname.lower()
    if any(k in plow for k in ("dns", "zone", "domain", "fqdn")):
        base = "infraforge-demo.com"
    elif "hostname" in plow:
        base = "app.infraforge-demo.com"
    elif plow.endswith("password") or plow.endswith("secret"):
        base = "InfraForge#Val1d!"
    elif plow.endswith("username"):
        base = "azureadmin"
    elif "sharedkey" in plow:
        base = "InfraForgeVal1dation!"
    else:
        base = f"ifrg-{pname}"

    max_len = pdef.get("maxLength")
    min_len = pdef.get("minLength")

    if isinstance(max_len, int) and len(base) > max_len:
        # Truncate to fit — keep a recognisable prefix
        base = base[:max_len]
    if isinstance(min_len, int) and len(base) < min_len:
        # Pad to meet minimum
        base = base + "x" * (min_len - len(base))

    return base


# Global-location resource types (DNS zones, CDN, etc.)
GLOBAL_LOCATION_TYPES = frozenset({
    "microsoft.network/dnszones",
    "microsoft.network/trafficmanagerprofiles",
    "microsoft.cdn/profiles",
    "microsoft.network/frontdoors",
    "microsoft.network/frontdoorwebapplicationfirewallpolicies",
})


# ══════════════════════════════════════════════════════════════
# PURE TEMPLATE TRANSFORMERS (no I/O, no LLM)
# ══════════════════════════════════════════════════════════════

def brief_azure_error(error_msg: str) -> str:
    """Convert a raw Azure ARM error into a one-line conversational brief."""
    code_match = re.search(r'\(([A-Za-z]+)\)', error_msg)
    code = code_match.group(1) if code_match else None

    _briefs = {
        "InvalidTemplate": "The ARM template has a structural issue",
        "InvalidTemplateDeployment": "One of the resource definitions has a configuration problem",
        "DeploymentFailed": "One or more resources couldn't be provisioned",
        "AccountNameInvalid": "A resource name doesn't meet Azure's naming requirements",
        "StorageAccountAlreadyTaken": "The storage account name is already in use globally",
        "InvalidResourceReference": "A resource dependency reference is pointing to something invalid",
        "LinkedAuthorizationFailed": "A cross-subscription resource reference needs authorization",
        "ResourceNotFound": "A referenced resource or dependency doesn't exist yet",
        "MissingRegistrationForType": "A resource provider hasn't been registered in the subscription",
        "InvalidApiVersionForResourceType": "The API version used for a resource type isn't supported",
        "BadRequest": "A resource property has an invalid value",
        "LocationNotAvailableForResourceType": "The resource type isn't available in the selected region",
        "SkuNotAvailable": "The requested SKU or tier isn't available in the selected region",
        "QuotaExceeded": "Hit a subscription quota or resource limit",
        "ConflictingUserInput": "Conflicting parameter values were provided",
        "InvalidParameter": "One of the parameter values is invalid",
        "PropertyChangeNotAllowed": "Tried to change a property that can't be modified after creation",
        "NoRegisteredProviderFound": "The resource provider isn't registered",
        "InvalidResourceType": "An unrecognized resource type was used in the template",
        "ParentResourceNotFound": "A parent resource this resource depends on wasn't found",
        "AnotherOperationInProgress": "Another operation is still running on the same resource",
        "InvalidRequestContent": "The template or parameters JSON structure is invalid",
        "ResourceGroupNotFound": "The target resource group doesn't exist",
        "AuthorizationFailed": "The deployment identity doesn't have permission for this operation",
        "RequestDisallowedByPolicy": "An Azure Policy is blocking this resource configuration",
    }

    if code and code in _briefs:
        return _briefs[code]

    clean = re.sub(r'[{}\[\]"]', '', error_msg)
    for sentence in clean.split("."):
        s = sentence.strip()
        if 20 < len(s) < 200:
            return s

    if code:
        return f"Azure returned a '{code}' error"
    return "The deployment encountered an issue"


def friendly_error(exc: Exception) -> str:
    """Convert raw Python exceptions into user-friendly messages for the UI."""
    msg = str(exc)
    ml = msg.lower()
    if "too many values to unpack" in ml or "not enough values" in ml:
        return "The AI auto-healer encountered an internal issue. Please retry — this is typically transient."
    if "pyodbc" in ml or ("sql" in ml and "timeout" in ml):
        return "Database connection timed out. Please wait a moment and retry."
    if "login timeout" in ml or "tcp provider" in ml:
        return "Database connection failed — the server may be temporarily unavailable. Please retry in a few seconds."
    if ("copilot" in ml or "sdk" in ml) and ("not available" in ml or "client" in ml):
        return "The AI service (Copilot SDK) is temporarily unavailable. Please retry."
    if "timeout" in ml or "timed out" in ml:
        return "The operation timed out. This can happen with complex templates — please retry."
    if "rate limit" in ml or "429" in msg:
        return "AI service rate limit reached. Please wait 30 seconds and retry."
    if "401" in msg or "unauthorized" in ml or "authentication" in ml:
        return "Authentication error with a backend service. Please refresh the page and retry."
    if len(msg) > 200:
        msg = msg[:200] + "…"
    return f"Onboarding encountered an unexpected error. Please retry. (Detail: {msg})"


def summarize_fix(before: str, after: str) -> str:
    """Produce a short summary of what changed between two ARM template strings."""
    if before == after:
        return "NO CHANGE (fix produced identical output)"
    try:
        b = json.loads(before)
        a = json.loads(after)
    except Exception:
        return f"Template text changed (before: {len(before)} chars → after: {len(after)} chars)"

    changes: list[str] = []
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

    b_apis = {r.get("type", "?"): r.get("apiVersion", "?") for r in b_res if isinstance(r, dict)}
    a_apis = {r.get("type", "?"): r.get("apiVersion", "?") for r in a_res if isinstance(r, dict)}
    for rt in set(b_apis) & set(a_apis):
        if b_apis[rt] != a_apis[rt]:
            changes.append(f"API version for {rt}: {b_apis[rt]} → {a_apis[rt]}")

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
        changes.append(f"template modified (size: {len(before)} → {len(after)} chars)")
    return "; ".join(changes[:5])


def ensure_parameter_defaults(template_json: str) -> str:
    """Ensure every parameter in an ARM template has a defaultValue.

    Uses ``_constrained_fallback`` so generated defaults respect
    ``maxLength``, ``minLength``, ``allowedValues``, and ``type``.
    """
    try:
        tmpl = json.loads(template_json)
    except (json.JSONDecodeError, TypeError):
        return template_json

    params = tmpl.get("parameters")
    if not params or not isinstance(params, dict):
        return template_json

    sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    patched = False
    for pname, pdef in params.items():
        if not isinstance(pdef, dict):
            continue
        if "defaultValue" not in pdef:
            dv = PARAM_DEFAULTS.get(pname)
            if dv is None:
                plow = pname.lower()
                if "subscri" in plow and sub_id:
                    dv = sub_id
                else:
                    dv = _constrained_fallback(pname, pdef)
            # Enforce maxLength even on PARAM_DEFAULTS values
            if isinstance(dv, str):
                max_len = pdef.get("maxLength")
                if isinstance(max_len, int) and len(dv) > max_len:
                    dv = dv[:max_len]
            pdef["defaultValue"] = dv
            patched = True

    if patched:
        return json.dumps(tmpl, indent=2)
    return template_json


def sanitize_placeholder_guids(template_json: str) -> str:
    """Replace placeholder/zero subscription GUIDs with the real subscription ID."""
    sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    if not sub_id:
        return template_json
    placeholder = "00000000-0000-0000-0000-000000000000"
    if placeholder not in template_json:
        return template_json
    logger.info("Replaced placeholder subscription GUID(s) with real subscription ID")
    return template_json.replace(placeholder, sub_id)


def sanitize_dns_zone_names(template_json: str) -> str:
    """Ensure DNS zone resources have valid FQDN names (at least 2 labels)."""
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
        name = res.get("name", "")
        if isinstance(name, str) and not name.startswith("[") and "." not in name:
            res["name"] = "infraforge-demo.com"
            patched = True
            logger.info(f"Fixed invalid DNS zone name '{name}' → 'infraforge-demo.com'")
        if isinstance(name, str) and name.startswith("[") and "parameters(" in name:
            m = re.search(r"parameters\(['\"](\w+)['\"]\)", name)
            if m:
                param_name = m.group(1)
                pdef = params.get(param_name, {})
                dv = pdef.get("defaultValue", "")
                if isinstance(dv, str) and dv and "." not in dv and not dv.startswith("["):
                    pdef["defaultValue"] = "infraforge-demo.com"
                    patched = True
                    logger.info(f"Fixed DNS zone param '{param_name}' default '{dv}' → 'infraforge-demo.com'")

    if patched:
        return json.dumps(tmpl, indent=2)
    return template_json


def sanitize_template(template_json: str) -> str:
    """Apply all template sanitization passes in the correct order."""
    result = ensure_parameter_defaults(template_json)
    result = sanitize_placeholder_guids(result)
    result = sanitize_dns_zone_names(result)
    return result


# ══════════════════════════════════════════════════════════════
# ARM COMPOSITION HELPERS
# ══════════════════════════════════════════════════════════════

_COMPOSE_STANDARD_PARAMS = {
    "resourceName", "location", "environment",
    "projectName", "ownerEmail", "costCenter",
}


def resolve_variables_for_composition(
    tpl: dict,
    suffix: str,
) -> tuple[dict, list[dict], dict, dict]:
    """Prepare a service ARM template for composition.

    ARM templates can use ``variables()`` references internally.  When
    multiple templates are merged into a single composed template, variable
    names can collide and the naive approach of ``composed["variables"] = {}``
    silently drops them, causing ``InvalidTemplate`` errors.

    This function resolves the problem by:

    1. Inlining simple variable values directly into resource bodies
       (e.g. ``[variables('vnetName')]`` → the literal value).
    2. Converting complex variable expressions to suffixed parameters
       so they don't collide across services.
    3. Remapping *all* parameter references with the suffix.
    4. Returning the processed (params, resources, outputs, variables)
       ready for merging.

    Returns:
        (extra_params, processed_resources, processed_outputs, resolved_variables)
    """
    src_params = tpl.get("parameters", {})
    src_vars = tpl.get("variables", {})
    src_resources = tpl.get("resources", [])
    src_outputs = tpl.get("outputs", {})

    # ── Build the non-standard parameter list ──
    all_non_standard = [
        pname for pname in src_params
        if pname not in _COMPOSE_STANDARD_PARAMS and pname != "resourceName"
    ]

    # ── Build parameter definitions for this service ──
    extra_params: dict = {}
    instance_name_param = f"resourceName{suffix}"
    extra_params[instance_name_param] = {
        "type": "string",
        "metadata": {"description": f"Name for this resource instance"},
    }

    for pname in all_non_standard:
        pdef = src_params.get(pname)
        if not pdef:
            continue
        suffixed = f"{pname}{suffix}"
        extra_params[suffixed] = dict(pdef)

    # ── Convert variables to parameters (suffixed) ──
    # This is the KEY fix: variables are promoted to parameters so they
    # survive composition.  Simple string/int values become defaultValues.
    vars_as_params: dict[str, str] = {}  # original var name → suffixed param name
    resolved_variables: dict = {}

    for vname, vval in src_vars.items():
        suffixed_param = f"{vname}{suffix}"
        vars_as_params[vname] = suffixed_param

        if isinstance(vval, str) and not vval.startswith("["):
            # Simple literal string — promote to parameter with default
            extra_params[suffixed_param] = {
                "type": "string",
                "defaultValue": vval,
                "metadata": {"description": f"Variable '{vname}' (auto-promoted)"},
            }
            resolved_variables[vname] = vval
        elif isinstance(vval, (int, float, bool)):
            ptype = "int" if isinstance(vval, int) else "string"
            extra_params[suffixed_param] = {
                "type": ptype,
                "defaultValue": vval,
                "metadata": {"description": f"Variable '{vname}' (auto-promoted)"},
            }
            resolved_variables[vname] = vval
        elif isinstance(vval, str) and vval.startswith("["):
            # ARM expression (e.g. [concat(...)]) — normally keep as a variable
            # in the resolved set; we'll add it to the composed variables dict.
            # EXCEPTION: utcNow() is only valid in parameter defaultValue
            # expressions, NOT in variables.  Promote it to a parameter.
            if "utcNow" in vval:
                extra_params[suffixed_param] = {
                    "type": "string",
                    "defaultValue": vval,
                    "metadata": {"description": f"Variable '{vname}' (promoted — utcNow only allowed in parameter defaults)"},
                }
                resolved_variables[vname] = vval
            else:
                # Store expression — will be remapped below after all
                # vars_as_params entries are built.
                resolved_variables[vname] = vval
        else:
            # Complex object/array — serialize as parameter default
            extra_params[suffixed_param] = {
                "type": "object" if isinstance(vval, dict) else "array" if isinstance(vval, list) else "string",
                "defaultValue": vval,
                "metadata": {"description": f"Variable '{vname}' (auto-promoted)"},
            }
            resolved_variables[vname] = vval

    # ── Remap references inside ARM expression variables ──
    # Expression variables (starting with '[') are stored raw above.  They
    # may reference other parameters or variables by their original names.
    # Those names need to be remapped to suffixed equivalents so the
    # composed template doesn't have dangling references.
    for vname in list(resolved_variables):
        vval = resolved_variables[vname]
        if not (isinstance(vval, str) and vval.startswith("[")):
            continue
        expr = vval

        # Remap non-standard parameter references
        for pname in all_non_standard:
            suffixed = f"{pname}{suffix}"
            expr = expr.replace(
                f"parameters('{pname}')",
                f"parameters('{suffixed}')",
            )

        # Remap variable references to their composed names
        for other_vname in vars_as_params:
            other_vval = src_vars.get(other_vname)
            _is_expr = isinstance(other_vval, str) and other_vval.startswith("[")
            _is_utc = _is_expr and "utcNow" in other_vval
            if _is_expr and not _is_utc:
                # Expression variable → stays as variable with suffix
                suffixed_var = f"{other_vname}{suffix}"
                expr = expr.replace(
                    f"variables('{other_vname}')",
                    f"variables('{suffixed_var}')",
                )
            else:
                # Literal/utcNow variable → promoted to parameter
                suffixed_param = vars_as_params[other_vname]
                expr = expr.replace(
                    f"variables('{other_vname}')",
                    f"parameters('{suffixed_param}')",
                )

        resolved_variables[vname] = expr

    # ── Process resources: remap parameter AND variable references ──
    processed_resources = []
    for res in src_resources:
        res_str = json.dumps(res)

        # Remap resourceName parameter
        res_str = res_str.replace(
            "[parameters('resourceName')]",
            f"[parameters('{instance_name_param}')]",
        )
        res_str = res_str.replace(
            "parameters('resourceName')",
            f"parameters('{instance_name_param}')",
        )

        # Remap all non-standard parameters
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

        # Remap variable references:
        # - Simple literals: replace variables('x') with parameters('x_suffix')
        # - ARM expressions: replace variables('x') with variables('x_suffix')
        # - utcNow expressions: promoted to parameters, so use parameters('x_suffix')
        for vname, suffixed_param in vars_as_params.items():
            vval = src_vars.get(vname)
            _is_expression = isinstance(vval, str) and vval.startswith("[")
            _is_utcnow = _is_expression and "utcNow" in vval
            if _is_expression and not _is_utcnow:
                # Expression variable — keep as variable but suffix the name
                suffixed_var = f"{vname}{suffix}"
                res_str = res_str.replace(
                    f"[variables('{vname}')]",
                    f"[variables('{suffixed_var}')]",
                )
                res_str = res_str.replace(
                    f"variables('{vname}')",
                    f"variables('{suffixed_var}')",
                )
            else:
                # Literal variable — replace with parameter reference
                res_str = res_str.replace(
                    f"[variables('{vname}')]",
                    f"[parameters('{suffixed_param}')]",
                )
                res_str = res_str.replace(
                    f"variables('{vname}')",
                    f"parameters('{suffixed_param}')",
                )

        processed_resources.append(json.loads(res_str))

    # ── Process outputs similarly ──
    processed_outputs = {}
    for oname, odef in src_outputs.items():
        out_name = f"{oname}{suffix}"
        out_str = json.dumps(odef)

        out_str = out_str.replace(
            "[parameters('resourceName')]",
            f"[parameters('{instance_name_param}')]",
        )
        out_str = out_str.replace(
            "parameters('resourceName')",
            f"parameters('{instance_name_param}')",
        )

        for pname in all_non_standard:
            suffixed = f"{pname}{suffix}"
            out_str = out_str.replace(
                f"[parameters('{pname}')]",
                f"[parameters('{suffixed}')]",
            )
            out_str = out_str.replace(
                f"parameters('{pname}')",
                f"parameters('{suffixed}')",
            )

        for vname, suffixed_param in vars_as_params.items():
            vval = src_vars.get(vname)
            if isinstance(vval, str) and vval.startswith("["):
                suffixed_var = f"{vname}{suffix}"
                out_str = out_str.replace(
                    f"[variables('{vname}')]",
                    f"[variables('{suffixed_var}')]",
                )
                out_str = out_str.replace(
                    f"variables('{vname}')",
                    f"variables('{suffixed_var}')",
                )
            else:
                out_str = out_str.replace(
                    f"[variables('{vname}')]",
                    f"[parameters('{suffixed_param}')]",
                )
                out_str = out_str.replace(
                    f"variables('{vname}')",
                    f"parameters('{suffixed_param}')",
                )

        processed_outputs[out_name] = json.loads(out_str)

    return extra_params, processed_resources, processed_outputs, resolved_variables


def build_composed_variables(
    all_resolved: dict[str, dict],
) -> dict:
    """Build the composed variables dict from resolved expressions.

    Only ARM expression variables (starting with '[') need to remain as
    variables in the composed template.  Literals have been promoted to
    parameters.  This also remaps parameter references within expression
    variables to their suffixed equivalents.

    Args:
        all_resolved: dict of suffix → {var_name: var_value} from each
                      service's resolve_variables_for_composition call.

    Returns:
        Combined variables dict for the composed template.
    """
    combined_vars: dict = {}
    for suffix, var_map in all_resolved.items():
        for vname, vval in var_map.items():
            if isinstance(vval, str) and vval.startswith("["):
                # utcNow() expressions are promoted to parameters (ARM only
                # allows utcNow in parameter defaults, not variables).
                if "utcNow" in vval:
                    continue
                suffixed_var = f"{vname}{suffix}"
                # Remap parameter references within the expression
                expr = vval
                # This is a best-effort remap — handles common patterns
                combined_vars[suffixed_var] = expr
    return combined_vars


def validate_arm_references(template: dict) -> list[str]:
    """Pre-deploy structural validation of an ARM template.

    Checks that all variables() and parameters() references resolve to
    defined names.  Returns a list of error strings (empty = valid).

    This catches composition bugs BEFORE hitting Azure, avoiding wasted
    deployment attempts and healing cycles.
    """
    import re as _re

    errors: list[str] = []
    params = set(template.get("parameters", {}).keys())
    variables = set(template.get("variables", {}).keys())

    tmpl_str = json.dumps(template)

    # Find all variable references
    var_refs = set(_re.findall(r"variables\(['\"](\w+)['\"]\)", tmpl_str))
    for vref in var_refs:
        if vref not in variables:
            errors.append(
                f"Missing variable '{vref}' — referenced in template but not defined in variables section"
            )

    # Find all parameter references
    param_refs = set(_re.findall(r"parameters\(['\"](\w+)['\"]\)", tmpl_str))
    for pref in param_refs:
        if pref not in params:
            errors.append(
                f"Missing parameter '{pref}' — referenced in template but not defined in parameters section"
            )

    return errors


def version_to_semver(version_int: int) -> str:
    """Convert an integer version number to semver format (N → N.0.0)."""
    return f"{version_int}.0.0"


def stamp_template_metadata(
    template_json: str,
    *,
    service_id: str,
    version_int: int,
    semver: str | None = None,
    gen_source: str = "unknown",
    region: str = "eastus2",
) -> str:
    """Embed InfraForge provenance metadata into an ARM template."""
    try:
        tmpl = json.loads(template_json)
    except (json.JSONDecodeError, TypeError):
        return template_json

    if not semver:
        semver = version_to_semver(version_int)

    tmpl["contentVersion"] = semver
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


def extract_param_values(template: dict) -> dict:
    """Extract explicit parameter values from a template's defaultValues.

    Skips location and ARM expressions (``[...]``).
    Uses ``_constrained_fallback`` to generate values that respect
    ``maxLength``, ``minLength``, ``allowedValues``, and ``type``.
    """
    params = template.get("parameters", {})
    values: dict[str, object] = {}
    for pname, pdef in params.items():
        if not isinstance(pdef, dict):
            continue
        dv = pdef.get("defaultValue")
        if dv is None:
            dv = PARAM_DEFAULTS.get(pname)
        if dv is None:
            dv = _constrained_fallback(pname, pdef)
        # Enforce maxLength even on existing defaults / PARAM_DEFAULTS
        if isinstance(dv, str):
            max_len = pdef.get("maxLength")
            if isinstance(max_len, int) and len(dv) > max_len:
                dv = dv[:max_len]
        if isinstance(dv, str) and dv.startswith("["):
            continue
        values[pname] = dv
    return values


def extract_meta(tmpl_str: str) -> dict:
    """Parse ARM template JSON and return metadata dict."""
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


def get_resource_type_hints(res_types: set[str]) -> str:
    """Return Azure-specific deployment knowledge for resource types."""
    _HINTS = {
        "microsoft.network/virtualnetworks/subnets": (
            "SUBNETS: Subnets can be deployed in two ways:\n"
            "  (a) As a nested 'subnets' array property INSIDE the VNet resource — "
            "simpler, avoids dependency issues, recommended for single-template deploys.\n"
            "  (b) As a separate child resource of type 'Microsoft.Network/virtualNetworks/subnets' — "
            "requires an explicit dependsOn on the parent VNet and correct 'name' format: "
            "'vnetName/subnetName' (NOT just 'subnetName').\n"
            "Common failures: address space conflicts (subnet prefix must be within VNet's "
            "address space), missing NSG/route table references, duplicate subnet names."
        ),
        "microsoft.network/virtualnetworks": (
            "VNETS: addressPrefixes is required. If subnets are defined, each subnet's "
            "addressPrefix must fall within the VNet's address space. Don't overlap subnets. "
            "For simple templates, define subnets inline in the 'subnets' property array."
        ),
        "microsoft.network/networksecuritygroups": (
            "NSGS: Security rules need unique priorities (100-4096). 'direction' must be "
            "'Inbound' or 'Outbound'. 'access' must be 'Allow' or 'Deny'. 'protocol' "
            "must be 'Tcp', 'Udp', 'Icmp', or '*'. Use '*' for sourceAddressPrefix to "
            "mean any source."
        ),
        "microsoft.keyvault/vaults": (
            "KEY VAULT: Requires 'tenantId' (use [subscription().tenantId]). "
            "accessPolicies or enableRbacAuthorization required. Name must be globally "
            "unique (3-24 chars, alphanumeric + hyphens). Enable soft delete and purge "
            "protection for production."
        ),
        "microsoft.storage/storageaccounts": (
            "STORAGE: Name MUST be 3-24 lowercase alphanumeric (NO hyphens, NO underscores). "
            "Globally unique. 'kind' is required: 'StorageV2' is recommended. "
            "'sku.name' is required: 'Standard_LRS', 'Standard_GRS', etc."
        ),
        "microsoft.web/sites": (
            "APP SERVICE: Requires a 'serverFarmId' pointing to an App Service Plan. "
            "If the plan doesn't exist in the template, add a Microsoft.Web/serverfarms resource. "
            "Use 'siteConfig' for runtime settings."
        ),
        "microsoft.containerservice/managedclusters": (
            "AKS: 'agentPoolProfiles' array is required with at least one pool. "
            "Each pool needs 'name', 'count', 'vmSize', 'mode' ('System' for the first). "
            "dnsPrefix is required and must be unique."
        ),
        "microsoft.sql/servers": (
            "SQL SERVER: 'administratorLogin' and 'administratorLoginPassword' are required "
            "unless using AAD-only auth. Server name must be globally unique, lowercase."
        ),
        "microsoft.network/applicationgateways": (
            "APP GATEWAY: Complex resource with many required sub-blocks: "
            "gatewayIPConfigurations, frontendIPConfigurations, frontendPorts, "
            "backendAddressPools, backendHttpSettingsCollection, httpListeners, "
            "requestRoutingRules. Requires an existing subnet (not the same as any "
            "other resource's subnet). Use a dedicated 'AppGatewaySubnet'."
        ),
    }
    hints = []
    for rt in res_types:
        rt_lower = rt.lower()
        if rt_lower in _HINTS:
            hints.append(_HINTS[rt_lower])
        if "/" in rt_lower:
            parent = "/".join(rt_lower.rsplit("/", 1)[:-1])
            if parent in _HINTS and _HINTS[parent] not in hints:
                hints.append(_HINTS[parent])
    return "\n\n".join(hints)


# ══════════════════════════════════════════════════════════════
# POLICY COMPLIANCE EVALUATION
# ══════════════════════════════════════════════════════════════

def test_policy_compliance(policy_json: dict, resources: list[dict]) -> list[dict]:
    """Evaluate deployed resources against an Azure Policy definition."""
    results = []
    rule = policy_json.get("properties", policy_json).get("policyRule", {})
    if_condition = rule.get("if", {})
    effect = rule.get("then", {}).get("effect", "deny")

    for resource in resources:
        match = _evaluate_condition(if_condition, resource)
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
        return all(_evaluate_condition(c, resource) for c in condition["allOf"])
    if "anyOf" in condition:
        return any(_evaluate_condition(c, resource) for c in condition["anyOf"])
    if "not" in condition:
        return not _evaluate_condition(condition["not"], resource)

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
        return fnmatch.fnmatch(str(resource_val).lower(), str(condition["like"]).lower())
    if "exists" in condition:
        exists = resource_val is not None and resource_val != ""
        want_exists = condition["exists"]
        if isinstance(want_exists, str):
            want_exists = want_exists.lower() not in ("false", "0", "no")
        return exists if want_exists else not exists
    if "greater" in condition:
        try:
            return float(resource_val or 0) > float(condition["greater"])
        except (ValueError, TypeError):
            return False
    if "less" in condition:
        try:
            return float(resource_val or 0) < float(condition["less"])
        except (ValueError, TypeError):
            return False
    return False


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


# ══════════════════════════════════════════════════════════════
# ASYNC TEMPLATE TRANSFORMERS
# ══════════════════════════════════════════════════════════════

async def inject_standard_tags(template_json: str, service_id: str = "*") -> str:
    """Inject org-standard-required tags into every ARM resource."""
    from src.standards import get_all_standards

    try:
        tmpl = json.loads(template_json)
    except (json.JSONDecodeError, TypeError):
        return template_json

    resources = tmpl.get("resources")
    if not resources or not isinstance(resources, list):
        return template_json

    all_standards = await get_all_standards(enabled_only=True)
    required_tags: set[str] = set()
    for std in all_standards:
        rule = std.get("rule", {})
        if rule.get("type") != "tags":
            continue
        scope = std.get("scope", "*")
        if scope != "*" and service_id != "*":
            if not fnmatch.fnmatch(service_id.lower(), scope.lower()):
                continue
        tags_list = rule.get("required_tags", [])
        if isinstance(tags_list, str):
            tags_list = tags_list.split()
        required_tags.update(tags_list)

    if not required_tags:
        return template_json

    tag_defaults = {
        "environment": "[parameters('environment')]",
        "owner": "[parameters('ownerEmail')]",
        "costcenter": "[parameters('costCenter')]",
        "project": "[parameters('projectName')]",
        "managedby": "InfraForge",
        "createdby": "InfraForge",
        "createddate": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "dataclassification": "internal",
        "expirydate": "2027-12-31",
        "supportcontact": "[parameters('ownerEmail')]",
        "team": "[parameters('projectName')]",
    }

    patched = False
    for res in resources:
        if not isinstance(res, dict):
            continue
        tags = res.get("tags")
        if tags is None:
            tags = {}
            res["tags"] = tags
        if not isinstance(tags, dict):
            continue
        existing_lower = {k.lower(): k for k in tags}
        for req_tag in required_tags:
            if req_tag.lower() not in existing_lower:
                default_val = tag_defaults.get(req_tag.lower(), f"TBD-{req_tag}")
                tags[req_tag] = default_val
                patched = True

    if patched:
        logger.info("Injected org-standard-required tags into ARM template resources")
        return json.dumps(tmpl, indent=2)
    return template_json


async def cleanup_rg(rg: str) -> None:
    """Fire-and-forget deletion of a resource group."""
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


# ══════════════════════════════════════════════════════════════
# LLM LOCATION GUARD
# ══════════════════════════════════════════════════════════════

def guard_locations(fixed: str) -> str:
    """Ensure healer didn't corrupt location parameters or resource locations.

    Restores ``[resourceGroup().location]`` for non-global resources and
    enforces ``"global"`` for types that require it.
    """
    try:
        ft = json.loads(fixed)
    except (json.JSONDecodeError, AttributeError):
        return fixed

    changed = False
    params = ft.get("parameters", {})
    loc = params.get("location", {})
    dv = loc.get("defaultValue", "")
    if isinstance(dv, str) and dv and not dv.startswith("["):
        loc["defaultValue"] = "[resourceGroup().location]"
        changed = True

    for res in ft.get("resources", []):
        rtype = (res.get("type") or "").lower()
        rloc = res.get("location", "")
        if rtype in GLOBAL_LOCATION_TYPES:
            if isinstance(rloc, str) and rloc.lower() != "global":
                res["location"] = "global"
                changed = True
            continue
        if isinstance(rloc, str) and rloc and not rloc.startswith("["):
            res["location"] = "[parameters('location')]"
            changed = True

    if changed:
        return json.dumps(ft, indent=2)
    return fixed


# ══════════════════════════════════════════════════════════════
# LLM HEALERS
# ══════════════════════════════════════════════════════════════

async def copilot_heal_template(
    content: str,
    error: str,
    previous_attempts: list[dict] | None = None,
    parameters: dict | None = None,
    standards_ctx: str | None = None,
    planning_ctx: str | None = None,
    resource_type_hints: str | None = None,
) -> str:
    """Single-phase LLM healer for ARM templates.

    Used by template validation and deploy pipelines.

    Optional context parameters (``standards_ctx``, ``planning_ctx``,
    ``resource_type_hints``) enrich the prompt with organizational
    standards and architecture intent so the healer makes better
    decisions — matching the context available to the two-phase healer.
    """
    from src.agents import TEMPLATE_HEALER
    from src.copilot_helpers import copilot_send
    from src.model_router import get_model_for_task

    steps_taken = len(previous_attempts) if previous_attempts else 0

    prompt = (
        "The following ARM template failed Azure deployment.\n\n"
        f"--- ERROR ---\n{error}\n--- END ERROR ---\n\n"
        f"--- CURRENT TEMPLATE ---\n{content}\n--- END TEMPLATE ---\n\n"
    )

    if standards_ctx:
        prompt += (
            "--- ORGANIZATIONAL STANDARDS ---\n"
            f"{standards_ctx[:3000]}\n"
            "--- END STANDARDS ---\n\n"
        )
    if planning_ctx:
        prompt += (
            "--- ARCHITECTURE PLAN ---\n"
            f"{planning_ctx[:2000]}\n"
            "--- END PLAN ---\n\n"
        )
    if resource_type_hints:
        prompt += (
            "--- RESOURCE-TYPE HINTS ---\n"
            f"{resource_type_hints[:2000]}\n"
            "--- END HINTS ---\n\n"
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

    # Late imports to avoid circular dependency at module load
    _client = None
    try:
        # Import ensure_copilot_client from web to get the singleton
        from src.web import ensure_copilot_client
        _client = await ensure_copilot_client()
    except ImportError:
        pass

    if _client is None:
        raise RuntimeError("Copilot SDK not available")

    fixed = await copilot_send(
        _client,
        model=get_model_for_task(TEMPLATE_HEALER.task),
        system_prompt=TEMPLATE_HEALER.system_prompt,
        prompt=prompt,
        timeout=90,
        agent_name="TEMPLATE_HEALER",
    )
    if fixed.startswith("```"):
        lines = fixed.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        fixed = "\n".join(lines).strip()

    fixed = guard_locations(fixed)
    fixed = ensure_parameter_defaults(fixed)
    fixed = sanitize_placeholder_guids(fixed)
    fixed = sanitize_dns_zone_names(fixed)
    return fixed


async def copilot_fix_two_phase(
    content: str,
    error: str,
    standards_ctx: str = "",
    planning_context: str = "",
    previous_attempts: list[dict] | None = None,
) -> tuple[str, str]:
    """Two-phase reasoning + fixing for ARM templates.

    Phase 1 (PLANNING model): Root-cause analysis + strategy.
    Phase 2 (CODE_FIXING model): Apply the strategy to produce a fix.

    Returns ``(fixed_template, strategy_text)``.
    """
    from src.agents import DEEP_TEMPLATE_HEALER
    from src.copilot_helpers import copilot_send
    from src.model_router import Task, get_model_for_task

    attempt_num = len(previous_attempts) + 1 if previous_attempts else 1
    fix_model = get_model_for_task(Task.CODE_FIXING)
    plan_model = get_model_for_task(Task.PLANNING)

    # ── Phase 1: Root Cause Analysis + Strategy ──
    analysis_prompt = (
        f"You are debugging an ARM template deployment failure (attempt {attempt_num}).\n\n"
        f"--- ERROR ---\n{error}\n--- END ERROR ---\n\n"
        f"--- CURRENT TEMPLATE (abbreviated) ---\n{content[:8000]}\n--- END TEMPLATE ---\n\n"
    )

    if planning_context:
        analysis_prompt += (
            f"--- ARCHITECTURE INTENT ---\n{planning_context[:3000]}\n--- END INTENT ---\n\n"
        )

    if previous_attempts:
        analysis_prompt += "--- PREVIOUS FAILED ATTEMPTS ---\n"
        for pa in previous_attempts:
            analysis_prompt += (
                f"Attempt {pa.get('step', '?')} (phase: {pa.get('phase', '?')}):\n"
                f"  Error: {pa['error'][:400]}\n"
                f"  Strategy tried: {pa.get('strategy', pa.get('fix_summary', 'unknown'))}\n"
                f"  Structural changes: {pa.get('fix_summary', 'unknown')}\n"
                f"  Result: STILL FAILED\n\n"
            )
        analysis_prompt += "--- END PREVIOUS ATTEMPTS ---\n\n"

    analysis_prompt += (
        "Produce a ROOT CAUSE ANALYSIS followed by a STRATEGY.\n\n"
        "Format your response EXACTLY as:\n\n"
        "ROOT CAUSE:\n<1-3 sentences>\n\n"
        "WHAT WAS TRIED AND WHY IT FAILED:\n<For each previous attempt>\n\n"
        "STRATEGY FOR THIS ATTEMPT:\n<Specific, concrete, DIFFERENT approach>\n\n"
        "Be specific. Don't say 'try a different API version' — say which "
        "version and why.\n"
    )

    # Add resource-type-specific knowledge
    try:
        _tpl = json.loads(content)
        _res_types = {r.get("type", "").lower() for r in _tpl.get("resources", []) if isinstance(r, dict)}
        _type_hints = get_resource_type_hints(_res_types)
        if _type_hints:
            analysis_prompt += f"\n--- RESOURCE-TYPE-SPECIFIC KNOWLEDGE ---\n{_type_hints}\n"
    except Exception:
        pass

    from src.web import ensure_copilot_client
    _client = await ensure_copilot_client()
    if _client is None:
        raise RuntimeError("Copilot SDK not available")

    strategy_text = await copilot_send(
        _client,
        model=plan_model,
        system_prompt=(
            "You are a senior Azure infrastructure engineer debugging ARM template "
            "deployment failures. You think like a developer — you analyze errors deeply, "
            "identify root causes, and propose concrete, specific fixes."
        ),
        prompt=analysis_prompt,
        timeout=60,
        agent_name="DEEP_TEMPLATE_HEALER",
    )

    logger.info(f"[Healer] Phase 1 strategy (attempt {attempt_num}): {strategy_text[:300]}")

    # ── Phase 2: Apply the Strategy ──
    fix_prompt = (
        f"Fix this ARM template following the STRATEGY below.\n\n"
        f"--- STRATEGY (from root cause analysis) ---\n{strategy_text}\n--- END STRATEGY ---\n\n"
        f"--- ERROR ---\n{error}\n--- END ERROR ---\n\n"
        f"--- CURRENT TEMPLATE ---\n{content}\n--- END TEMPLATE ---\n\n"
    )

    try:
        _fix_tpl2 = json.loads(content)
        _fix_params2 = extract_param_values(_fix_tpl2)
        if _fix_params2:
            fix_prompt += (
                "--- PARAMETER VALUES SENT TO ARM ---\n"
                f"{json.dumps(_fix_params2, indent=2, default=str)}\n"
                "--- END PARAMETER VALUES ---\n\n"
            )
    except Exception:
        pass

    if standards_ctx:
        fix_prompt += (
            f"--- ORGANIZATION STANDARDS (MUST be satisfied) ---\n{standards_ctx}\n"
            "--- END STANDARDS ---\n\n"
        )

    fix_prompt += (
        "FOLLOW the strategy above. Apply the SPECIFIC changes it recommends.\n"
        "Return ONLY the corrected raw JSON — no markdown fences, no explanation.\n\n"
        "CRITICAL RULES:\n"
        "1. LOCATIONS — Keep ALL location parameters as \"[resourceGroup().location]\" "
        "or \"[parameters('location')]\" — NEVER hardcode a region.\n"
        "   EXCEPTION: Globally-scoped resources MUST use location \"global\".\n"
        "2. Ensure EVERY parameter has a \"defaultValue\".\n"
        "3. Add tags: environment, owner, costCenter, project on every resource.\n"
        "4. NEVER use placeholder GUIDs like '00000000-0000-0000-0000-000000000000'.\n"
    )

    fixed = await copilot_send(
        _client,
        model=fix_model,
        system_prompt=DEEP_TEMPLATE_HEALER.system_prompt,
        prompt=fix_prompt,
        timeout=90,
        agent_name="DEEP_TEMPLATE_HEALER",
    )
    if fixed.startswith("```"):
        lines = fixed.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        fixed = "\n".join(lines).strip()

    if not fixed:
        logger.warning("Copilot healer returned empty response — keeping original template")
        return content, strategy_text

    if not fixed.startswith("{"):
        _json_start = fixed.find("{")
        _json_end = fixed.rfind("}")
        if _json_start >= 0 and _json_end > _json_start:
            fixed = fixed[_json_start:_json_end + 1]
        else:
            logger.warning("Copilot healer returned non-JSON text — keeping original template")
            return content, strategy_text

    try:
        json.loads(fixed)
    except json.JSONDecodeError:
        logger.warning("Copilot healer returned invalid JSON — keeping original template")
        return content, strategy_text

    fixed = guard_locations(fixed)
    fixed = ensure_parameter_defaults(fixed)
    fixed = sanitize_placeholder_guids(fixed)

    return fixed, strategy_text


# ══════════════════════════════════════════════════════════════
# TRANSIENT ERROR DETECTION
# ══════════════════════════════════════════════════════════════

TRANSIENT_KEYWORDS = (
    "beingdeleted", "being deleted", "deprovisioning",
    "throttled", "toomanyrequests", "retryable",
    "serviceunavailable", "internalservererror",
)


def is_transient_error(error_msg: str) -> bool:
    """Check if an Azure error message indicates a transient infrastructure issue."""
    lower = error_msg.lower()
    return any(kw in lower for kw in TRANSIENT_KEYWORDS)


def build_final_params(tpl: dict, user_params: dict | None = None) -> dict:
    """Build parameter values for ARM deployment from template defaults + user overrides."""
    tpl_params = tpl.get("parameters", {})
    final_params: dict = {}
    for pname, pdef in tpl_params.items():
        if user_params and pname in user_params:
            final_params[pname] = user_params[pname]
        elif isinstance(pdef, dict) and "defaultValue" in pdef:
            dv = pdef["defaultValue"]
            if isinstance(dv, str) and dv.startswith("["):
                continue
            final_params[pname] = dv
        else:
            ptype = pdef.get("type", "string").lower() if isinstance(pdef, dict) else "string"
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
    return final_params
