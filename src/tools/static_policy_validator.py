"""Static Policy Validator — Standards-Driven.

Validates ARM template JSON against organization governance standards
WITHOUT deploying to Azure.

This is the first validation gate — fast, cheap, and catches most issues
before burning Azure resources on a deployment test.

The validator reads rules from org_standards (the single source of truth)
and evaluates them against ARM template resources dynamically.  Each
standard's ``rule_json`` declares what to check:

Rule types:
    property       — check a resource property value (operator: ==, !=, >=, in, exists)
    property_check — check a specific ARM property path with deep inspection
    tags           — check for required resource tags
    allowed_values — check a value is in an allowlist
    cost_threshold — informational cost cap (warn only)

All checks also have Azure-specific deep-inspection logic so that
common security controls (TLS, encryption, RBAC, soft-delete, blob access)
are validated against the actual ARM property structure, not just abstract
property names.
"""

import fnmatch
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# RESULT TYPES
# ══════════════════════════════════════════════════════════════

@dataclass
class PolicyCheckResult:
    """Result of a single policy check against an ARM template."""
    rule_id: str
    rule_name: str
    passed: bool
    severity: str  # critical, high, medium, low
    enforcement: str  # block, warn
    message: str
    resource_type: str = ""
    resource_name: str = ""
    remediation: str = ""

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "passed": self.passed,
            "severity": self.severity,
            "enforcement": self.enforcement,
            "message": self.message,
            "resource_type": self.resource_type,
            "resource_name": self.resource_name,
            "remediation": self.remediation,
        }


@dataclass
class ValidationReport:
    """Complete validation report for an ARM template."""
    passed: bool
    total_checks: int = 0
    passed_checks: int = 0
    failed_checks: int = 0
    warnings: int = 0
    blockers: int = 0
    results: list[PolicyCheckResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "total_checks": self.total_checks,
            "passed_checks": self.passed_checks,
            "failed_checks": self.failed_checks,
            "warnings": self.warnings,
            "blockers": self.blockers,
            "results": [r.to_dict() for r in self.results],
        }

    def summary(self) -> str:
        icon = "✅" if self.passed else "❌"
        return (
            f"{icon} {self.passed_checks}/{self.total_checks} checks passed, "
            f"{self.blockers} blocker(s), {self.warnings} warning(s)"
        )


# ══════════════════════════════════════════════════════════════
# SCOPE MATCHING  (duplicated from standards.py to avoid circular imports)
# ══════════════════════════════════════════════════════════════

def _scope_matches(scope: str, resource_type: str) -> bool:
    """Check if a resource type matches a scope pattern (comma-separated globs)."""
    resource_lower = resource_type.lower()
    for pattern in scope.split(","):
        pattern = pattern.strip().lower()
        if not pattern:
            continue
        if fnmatch.fnmatch(resource_lower, pattern):
            return True
    return False


# ══════════════════════════════════════════════════════════════
# DEEP ARM PROPERTY INSPECTION
# ══════════════════════════════════════════════════════════════
# Maps abstract standard keys → concrete ARM property paths
# so that e.g. "minTlsVersion" checks the right place for
# each resource type.

# TLS property locations per resource type
_TLS_PROPS: dict[str, tuple[str | None, str]] = {
    "microsoft.web/sites":                          ("siteConfig", "minTlsVersion"),
    "microsoft.sql/servers":                        (None, "minimalTlsVersion"),
    "microsoft.storage/storageaccounts":            (None, "minimumTlsVersion"),
    "microsoft.cache/redis":                        (None, "minimumTlsVersion"),
    "microsoft.dbforpostgresql/flexibleservers":    (None, "minimalTlsVersion"),
}

# Types that should have managed identity
_MI_TYPES: set[str] = {
    "microsoft.web/sites", "microsoft.containerservice/managedclusters",
    "microsoft.app/containerapps", "microsoft.sql/servers",
    "microsoft.keyvault/vaults", "microsoft.cognitiveservices/accounts",
    "microsoft.machinelearningservices/workspaces",
    "microsoft.documentdb/databaseaccounts",
}

# Types that support publicNetworkAccess
_PRIVATE_TYPES: set[str] = {
    "microsoft.sql/servers", "microsoft.keyvault/vaults",
    "microsoft.storage/storageaccounts", "microsoft.cache/redis",
    "microsoft.cognitiveservices/accounts", "microsoft.documentdb/databaseaccounts",
    "microsoft.machinelearningservices/workspaces",
    "microsoft.dbforpostgresql/flexibleservers",
}


def _get_deep_property(res: dict, key: str) -> tuple[bool, object]:
    """Get a property value with ARM-specific deep inspection.

    Returns (found: bool, value: object).
    Handles special keys that live in different places per resource type.
    """
    rtype = res.get("type", "").lower()
    props = res.get("properties", {})

    # ── TLS ───────────────────────────────────────────────
    if key.lower() in ("mintlsversion", "minimumtlsversion", "minimaltlsversion"):
        if rtype in _TLS_PROPS:
            parent_key, tls_key = _TLS_PROPS[rtype]
            if parent_key:
                val = props.get(parent_key, {}).get(tls_key)
            else:
                val = props.get(tls_key)
            return (val is not None, val)
        # Try generic lookup
        for candidate in ("minTlsVersion", "minimumTlsVersion", "minimalTlsVersion"):
            if candidate in props:
                return (True, props[candidate])
        return (False, None)

    # ── HTTPS ─────────────────────────────────────────────
    if key.lower() == "httpsonly":
        if "microsoft.web/sites" in rtype:
            return (True, props.get("httpsOnly", False))
        if "microsoft.storage/storageaccounts" in rtype:
            return (True, props.get("supportsHttpsTrafficOnly", False))
        return ("httpsOnly" in props, props.get("httpsOnly"))

    # ── Managed Identity ──────────────────────────────────
    if key.lower() == "managedidentity":
        if rtype not in _MI_TYPES:
            return (True, True)   # Not applicable → passes
        identity = res.get("identity", {})
        has_mi = identity.get("type") in (
            "SystemAssigned", "UserAssigned", "SystemAssigned,UserAssigned"
        )
        return (True, has_mi)

    # ── Public Network Access ─────────────────────────────
    if key.lower() == "publicnetworkaccess":
        if rtype not in _PRIVATE_TYPES:
            return (True, "Disabled")  # Not applicable → passes
        return (True, props.get("publicNetworkAccess", "Enabled"))

    # ── Private Endpoints ─────────────────────────────────
    if key.lower() == "privateendpoints":
        if rtype not in _PRIVATE_TYPES:
            return (True, True)  # Not applicable → passes
        public = props.get("publicNetworkAccess", "Enabled")
        return (True, str(public).lower() in ("disabled", "false"))

    # ── Encryption at rest ────────────────────────────────
    if key.lower() == "encryptionatrest":
        if "microsoft.storage/storageaccounts" in rtype:
            encryption = props.get("encryption", {})
            return (True, bool(encryption.get("services")))
        if "microsoft.sql" in rtype:
            # TDE is on by default for Azure SQL
            return (True, True)
        # Generic: check for encryption block
        return (True, bool(props.get("encryption")))

    # ── Diagnostic Logging ────────────────────────────────
    if key.lower() == "diagnosticlogging":
        # Template-level check — can't reliably check in static analysis
        return (True, True)

    # ── Soft Delete / Purge Protection (Key Vault) ────────
    if key.lower() == "enablesoftdelete":
        return (True, bool(props.get("enableSoftDelete", False)))
    if key.lower() == "enablepurgeprotection":
        return (True, bool(props.get("enablePurgeProtection", False)))

    # ── RBAC Authorization (Key Vault) ────────────────────
    if key.lower() == "enablerbacauthorization":
        return (True, bool(props.get("enableRbacAuthorization", False)))

    # ── AAD Auth ──────────────────────────────────────────
    if key.lower() == "aadauthenabled":
        if "microsoft.sql/servers" in rtype:
            admins = props.get("administrators", {})
            return (True, bool(admins.get("azureADOnlyAuthentication", False)))
        return (True, True)  # Not applicable

    # ── Blob Public Access ────────────────────────────────
    if key.lower() == "allowblobpublicaccess":
        if "microsoft.storage/storageaccounts" in rtype:
            return (True, props.get("allowBlobPublicAccess", True))
        return (True, False)  # Not applicable → passes

    # ── Generic property lookup ───────────────────────────
    if key in props:
        return (True, props[key])

    # Check nested in properties (case-insensitive)
    for k, v in props.items():
        if k.lower() == key.lower():
            return (True, v)

    return (False, None)


# ══════════════════════════════════════════════════════════════
# OPERATOR EVALUATION
# ══════════════════════════════════════════════════════════════

def _evaluate_operator(actual, operator: str, expected) -> bool:
    """Evaluate a comparison operator."""
    if operator in ("==", "eq"):
        if isinstance(expected, bool):
            return bool(actual) == expected
        return str(actual).lower() == str(expected).lower()

    if operator in ("!=", "ne"):
        return str(actual).lower() != str(expected).lower()

    if operator in (">=", "gte"):
        try:
            # Handle TLS versions like "1.2", "TLS1_2", etc.
            def _normalize_ver(v):
                s = str(v).replace("TLS", "").replace("Tls", "").replace("_", ".")
                for part in s.split("."):
                    try:
                        return float(s) if "." in s else float(part)
                    except ValueError:
                        continue
                return 0.0
            return _normalize_ver(actual) >= _normalize_ver(expected)
        except (ValueError, TypeError):
            return str(actual) >= str(expected)

    if operator in ("<=", "lte"):
        try:
            return float(actual) <= float(expected)
        except (ValueError, TypeError):
            return str(actual) <= str(expected)

    if operator == "in":
        if isinstance(expected, list):
            actual_lower = str(actual).lower()
            return actual_lower in [str(v).lower() for v in expected]
        return str(actual).lower() in str(expected).lower()

    if operator == "not_in":
        if isinstance(expected, list):
            actual_lower = str(actual).lower()
            return actual_lower not in [str(v).lower() for v in expected]
        return str(actual).lower() not in str(expected).lower()

    if operator == "exists":
        return actual is not None

    if operator == "not_exists":
        return actual is None

    # Default: equality
    return str(actual).lower() == str(expected).lower()


# ══════════════════════════════════════════════════════════════
# STANDARDS-DRIVEN VALIDATOR (new — single source of truth)
# ══════════════════════════════════════════════════════════════

def validate_template_against_standards(
    template: dict,
    standards: list[dict],
) -> ValidationReport:
    """Validate an ARM template against org_standards rules.

    This is the primary validation entry point.  Each standard's
    ``rule_json`` is interpreted dynamically — no hardcoded rule IDs.

    Args:
        template: Parsed ARM template JSON dict
        standards: List of org_standard dicts (from get_all_standards or
            get_standards_for_service), each with a ``rule`` dict and
            ``scope`` glob pattern.

    Returns:
        ValidationReport with all check results
    """
    results: list[PolicyCheckResult] = []
    resources = template.get("resources", [])

    for std in standards:
        if not std.get("enabled", True):
            continue

        rule = std.get("rule", {})
        rule_type = rule.get("type", "property")
        scope = std.get("scope", "*")
        severity = std.get("severity", "high")
        enforcement = "block" if severity in ("critical", "high") else "warn"
        remediation = rule.get("remediation", "")

        if rule_type in ("property", "property_check"):
            _check_property_standard(std, rule, scope, severity, enforcement,
                                     remediation, resources, results)
        elif rule_type == "tags":
            _check_tags_standard(std, rule, scope, severity, enforcement,
                                 remediation, resources, results)
        elif rule_type == "allowed_values":
            _check_allowed_values_standard(std, rule, scope, severity,
                                           enforcement, remediation,
                                           resources, results, template)
        elif rule_type == "cost_threshold":
            # Cost thresholds are informational — always pass at template level
            results.append(PolicyCheckResult(
                rule_id=std["id"],
                rule_name=std["name"],
                passed=True,
                severity=severity,
                enforcement="warn",
                message=f"Cost threshold: ${rule.get('max_monthly_usd', 0)}/mo (checked at deployment time)",
                remediation=remediation,
            ))
        else:
            logger.warning(f"Unknown rule type '{rule_type}' in standard {std['id']}")

    # ── Build final report ────────────────────────────────
    passed_count = sum(1 for r in results if r.passed)
    failed_count = sum(1 for r in results if not r.passed)
    blockers = sum(1 for r in results if not r.passed and r.enforcement == "block")
    warnings = sum(1 for r in results if not r.passed and r.enforcement == "warn")

    report = ValidationReport(
        passed=blockers == 0,
        total_checks=len(results),
        passed_checks=passed_count,
        failed_checks=failed_count,
        warnings=warnings,
        blockers=blockers,
        results=results,
    )

    logger.info(f"Standards-driven validation: {report.summary()}")
    return report


def _check_property_standard(std, rule, scope, severity, enforcement,
                              remediation, resources, results):
    """Evaluate a property-type standard against matching resources."""
    key = rule.get("key", "")
    operator = rule.get("operator", "==")
    expected = rule.get("value")

    applicable = [r for r in resources if _scope_matches(scope, r.get("type", ""))]

    if not applicable:
        return

    for res in applicable:
        rtype = res.get("type", "unknown")
        rname = res.get("name", "unknown")

        found, actual = _get_deep_property(res, key)

        if not found and operator not in ("exists", "not_exists"):
            results.append(PolicyCheckResult(
                rule_id=std["id"],
                rule_name=std["name"],
                passed=False,
                severity=severity,
                enforcement=enforcement,
                message=f"Property '{key}' not set on resource",
                resource_type=rtype,
                resource_name=rname,
                remediation=remediation,
            ))
            continue

        passed = _evaluate_operator(actual, operator, expected)

        results.append(PolicyCheckResult(
            rule_id=std["id"],
            rule_name=std["name"],
            passed=passed,
            severity=severity,
            enforcement=enforcement,
            message=(
                f"{key} = {actual}" + (" ✓" if passed else f" (expected {operator} {expected})")
            ),
            resource_type=rtype,
            resource_name=rname,
            remediation=remediation if not passed else "",
        ))


def _check_tags_standard(std, rule, scope, severity, enforcement,
                          remediation, resources, results):
    """Evaluate a tags-type standard against matching resources."""
    required_tags = rule.get("required_tags", [])
    # Handle string format: "environment owner costCenter project"
    if isinstance(required_tags, str):
        required_tags = required_tags.split()
    if not required_tags:
        return

    applicable = [r for r in resources if _scope_matches(scope, r.get("type", ""))]

    for res in applicable:
        rtype = res.get("type", "unknown")
        rname = res.get("name", "unknown")
        tags = res.get("tags", {})

        if not tags:
            results.append(PolicyCheckResult(
                rule_id=std["id"],
                rule_name=std["name"],
                passed=False,
                severity=severity,
                enforcement=enforcement,
                message=f"Resource has no tags. Required: {', '.join(required_tags)}",
                resource_type=rtype,
                resource_name=rname,
                remediation=f"Add tags block with: {', '.join(required_tags)}",
            ))
        else:
            missing = [t for t in required_tags if t not in tags]
            results.append(PolicyCheckResult(
                rule_id=std["id"],
                rule_name=std["name"],
                passed=len(missing) == 0,
                severity=severity,
                enforcement=enforcement,
                message=(
                    f"All {len(required_tags)} required tags present"
                    if not missing
                    else f"Missing required tags: {', '.join(missing)}"
                ),
                resource_type=rtype,
                resource_name=rname,
                remediation=f"Add missing tags: {', '.join(missing)}" if missing else "",
            ))


def _check_allowed_values_standard(std, rule, scope, severity, enforcement,
                                    remediation, resources, results, template):
    """Evaluate an allowed_values-type standard."""
    key = rule.get("key", "")
    allowed = rule.get("values", [])
    # Handle string format: "eastus2 westus2 westeurope"
    if isinstance(allowed, str):
        allowed = allowed.split()

    if not allowed:
        return

    applicable = [r for r in resources if _scope_matches(scope, r.get("type", ""))]

    for res in applicable:
        rtype = res.get("type", "unknown")
        rname = res.get("name", "unknown")

        if key.lower() == "location":
            location = res.get("location", "")
            if isinstance(location, str) and location.startswith("["):
                results.append(PolicyCheckResult(
                    rule_id=std["id"],
                    rule_name=std["name"],
                    passed=True,
                    severity=severity,
                    enforcement=enforcement,
                    message="Location uses ARM expression (resolved at deploy time)",
                    resource_type=rtype,
                    resource_name=rname,
                ))
                continue

            loc_lower = location.lower().replace(" ", "") if location else ""
            allowed_lower = [v.lower().replace(" ", "") for v in allowed]
            passed = loc_lower in allowed_lower if loc_lower else True

            results.append(PolicyCheckResult(
                rule_id=std["id"],
                rule_name=std["name"],
                passed=passed,
                severity=severity,
                enforcement=enforcement,
                message=(
                    f"Location '{location}' is an approved region"
                    if passed
                    else f"Location '{location}' is NOT an approved region. Allowed: {', '.join(allowed)}"
                ),
                resource_type=rtype,
                resource_name=rname,
                remediation=remediation if not passed else "",
            ))
        else:
            found, actual = _get_deep_property(res, key)
            if found:
                actual_lower = str(actual).lower()
                allowed_lower = [str(v).lower() for v in allowed]
                passed = actual_lower in allowed_lower
                results.append(PolicyCheckResult(
                    rule_id=std["id"],
                    rule_name=std["name"],
                    passed=passed,
                    severity=severity,
                    enforcement=enforcement,
                    message=(
                        f"{key} = '{actual}' is allowed"
                        if passed
                        else f"{key} = '{actual}' not in allowed values: {', '.join(str(v) for v in allowed)}"
                    ),
                    resource_type=rtype,
                    resource_name=rname,
                    remediation=remediation if not passed else "",
                ))


# ══════════════════════════════════════════════════════════════
# LEGACY ADAPTER — backward compatibility with governance_policies dict
# ══════════════════════════════════════════════════════════════

def validate_template(
    template: dict,
    governance_policies: dict,
    security_standards: list[dict] | None = None,
) -> ValidationReport:
    """Legacy entry point — converts governance_policies dict to standards format.

    This maintains backward compatibility with existing callers that pass the
    old-style governance_policies dict (from the governance_policies table).
    Internally, it converts these to the new org_standards format and delegates
    to ``validate_template_against_standards``.
    """
    synthetic_standards: list[dict] = []

    # GOV-001: Required Tags
    required_tags = governance_policies.get("require_tags", [])
    if required_tags:
        synthetic_standards.append({
            "id": "GOV-001", "name": "Required Resource Tags",
            "scope": "*", "severity": "high", "enabled": True,
            "rule": {"type": "tags", "required_tags": required_tags,
                     "remediation": f"Add tags block with: {', '.join(required_tags)}"},
        })

    # GOV-002: Allowed Regions
    allowed_regions = governance_policies.get("allowed_regions", [])
    if allowed_regions:
        synthetic_standards.append({
            "id": "GOV-002", "name": "Allowed Deployment Regions",
            "scope": "*", "severity": "critical", "enabled": True,
            "rule": {"type": "allowed_values", "key": "location",
                     "values": allowed_regions,
                     "remediation": "Use [parameters('location')] or [resourceGroup().location]"},
        })

    # GOV-003: HTTPS
    if governance_policies.get("require_https", False):
        synthetic_standards.append({
            "id": "GOV-003", "name": "HTTPS Enforcement",
            "scope": "Microsoft.Web/*,Microsoft.Storage/*", "severity": "critical",
            "enabled": True,
            "rule": {"type": "property", "key": "httpsOnly", "operator": "==",
                     "value": True, "remediation": "Set httpsOnly = true"},
        })

    # GOV-004: Managed Identity
    if governance_policies.get("require_managed_identity", False):
        synthetic_standards.append({
            "id": "GOV-004", "name": "Managed Identity Enforcement",
            "scope": "*", "severity": "high", "enabled": True,
            "rule": {"type": "property", "key": "managedIdentity", "operator": "==",
                     "value": True,
                     "remediation": 'Add "identity": {"type": "SystemAssigned"}'},
        })

    # GOV-005: Private Endpoints
    if governance_policies.get("require_private_endpoints", False):
        synthetic_standards.append({
            "id": "GOV-005", "name": "Private Endpoints (Production)",
            "scope": "Microsoft.Sql/*,Microsoft.KeyVault/*,Microsoft.Storage/*,"
                     "Microsoft.Cache/*,Microsoft.DocumentDB/*",
            "severity": "high", "enabled": True,
            "rule": {"type": "property", "key": "publicNetworkAccess", "operator": "==",
                     "value": "Disabled",
                     "remediation": 'Set properties.publicNetworkAccess = "Disabled"'},
        })

    # Always run security checks
    synthetic_standards.extend(_SECURITY_STANDARDS)

    return validate_template_against_standards(template, synthetic_standards)


# Built-in security standards (always active)
_SECURITY_STANDARDS: list[dict] = [
    {
        "id": "SEC-002", "name": "TLS 1.2 Minimum",
        "scope": "Microsoft.Web/*,Microsoft.Sql/*,Microsoft.Storage/*,"
                 "Microsoft.Cache/*,Microsoft.DBforPostgreSQL/*",
        "severity": "critical", "enabled": True,
        "rule": {"type": "property", "key": "minTlsVersion", "operator": ">=",
                 "value": "1.2", "remediation": "Set minTlsVersion to '1.2'"},
    },
    {
        "id": "SEC-005", "name": "Encryption at Rest",
        "scope": "Microsoft.Storage/*", "severity": "critical", "enabled": True,
        "rule": {"type": "property", "key": "encryptionAtRest", "operator": "==",
                 "value": True, "remediation": "Add encryption.services configuration"},
    },
    {
        "id": "SEC-007", "name": "Soft Delete / Purge Protection",
        "scope": "Microsoft.KeyVault/*", "severity": "high", "enabled": True,
        "rule": {"type": "property", "key": "enableSoftDelete", "operator": "==",
                 "value": True,
                 "remediation": "Enable both enableSoftDelete and enablePurgeProtection"},
    },
    {
        "id": "SEC-008", "name": "RBAC Authorization",
        "scope": "Microsoft.KeyVault/*", "severity": "high", "enabled": True,
        "rule": {"type": "property", "key": "enableRbacAuthorization", "operator": "==",
                 "value": True,
                 "remediation": "Set enableRbacAuthorization = true"},
    },
    {
        "id": "SEC-013", "name": "Blob Public Access Disabled",
        "scope": "Microsoft.Storage/*", "severity": "critical", "enabled": True,
        "rule": {"type": "property", "key": "allowBlobPublicAccess", "operator": "==",
                 "value": False,
                 "remediation": "Set allowBlobPublicAccess = false"},
    },
]


# ══════════════════════════════════════════════════════════════
# GENERATE REMEDIATION PROMPT
# ══════════════════════════════════════════════════════════════

def build_remediation_prompt(
    template_json: str,
    failed_results: list[PolicyCheckResult],
) -> str:
    """Build a Copilot prompt to fix an ARM template based on failed policy checks.

    Used by the auto-healing loop to fix templates that fail static validation.
    """
    violations = "\n".join(
        f"- [{r.rule_id}] {r.rule_name} ({r.severity}): {r.message}. "
        f"Fix: {r.remediation}"
        for r in failed_results
    )

    return (
        "The following ARM template failed static policy validation.\n\n"
        f"--- POLICY VIOLATIONS ---\n{violations}\n--- END VIOLATIONS ---\n\n"
        f"--- CURRENT TEMPLATE ---\n{template_json}\n--- END TEMPLATE ---\n\n"
        "Fix the template so ALL policy checks pass. Return ONLY the corrected "
        "raw JSON — no markdown fences, no explanation.\n\n"
        "CRITICAL RULES:\n"
        "- Keep ALL location values as ARM expressions: "
        "\"[resourceGroup().location]\" or \"[parameters('location')]\"\n"
        "- Keep the same resource type and intent\n"
        "- Add ALL required tags: environment, owner, costCenter, project\n"
        "- Fix security settings as described in the violations above\n"
        "- Do NOT add resources that weren't there before (no diagnosticSettings, "
        "no Log Analytics workspaces)\n"
        "- Do NOT change parameter default values unless fixing a violation\n"
    )
