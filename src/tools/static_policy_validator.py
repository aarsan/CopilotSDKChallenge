"""Static Policy Validator.

Validates ARM template JSON against organization-wide governance policies
and security standards WITHOUT deploying to Azure.

This is the first validation gate — fast, cheap, and catches most issues
before burning Azure resources on a deployment test.

Checks include:
- Required resource tags (GOV-001)
- Allowed deployment regions (GOV-002)
- HTTPS enforcement (GOV-003 / SEC-001)
- Managed identity requirement (GOV-004 / SEC-003)
- Private endpoint / public access (GOV-005 / SEC-004)
- TLS version (SEC-002)
- Encryption at rest (SEC-005)
- Soft delete / purge protection (SEC-007)
- RBAC authorization (SEC-008)
- Blob public access (SEC-013)
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

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
# STATIC POLICY VALIDATOR
# ══════════════════════════════════════════════════════════════

def validate_template(
    template: dict,
    governance_policies: dict,
    security_standards: list[dict] | None = None,
) -> ValidationReport:
    """Validate an ARM template against org-wide governance policies.

    Args:
        template: Parsed ARM template JSON dict
        governance_policies: Dict keyed by rule_key from governance_policies table
            e.g. {"require_tags": ["environment", "owner", ...], "allowed_regions": [...]}
        security_standards: Optional list of security standard dicts from DB

    Returns:
        ValidationReport with all check results
    """
    results: list[PolicyCheckResult] = []
    resources = template.get("resources", [])
    parameters = template.get("parameters", {})

    # ── GOV-001: Required Tags ────────────────────────────────
    required_tags = governance_policies.get("require_tags", [])
    if required_tags:
        for res in resources:
            rtype = res.get("type", "unknown")
            rname = res.get("name", "unknown")
            tags = res.get("tags", {})

            if not tags:
                results.append(PolicyCheckResult(
                    rule_id="GOV-001",
                    rule_name="Required Resource Tags",
                    passed=False,
                    severity="high",
                    enforcement="block",
                    message=f"Resource has no tags. Required: {', '.join(required_tags)}",
                    resource_type=rtype,
                    resource_name=rname,
                    remediation=f"Add tags block with: {', '.join(required_tags)}",
                ))
            else:
                # Check which required tags are present (handle ARM expressions)
                missing = []
                for tag in required_tags:
                    if tag not in tags:
                        missing.append(tag)

                if missing:
                    results.append(PolicyCheckResult(
                        rule_id="GOV-001",
                        rule_name="Required Resource Tags",
                        passed=False,
                        severity="high",
                        enforcement="block",
                        message=f"Missing required tags: {', '.join(missing)}",
                        resource_type=rtype,
                        resource_name=rname,
                        remediation=f"Add missing tags: {', '.join(missing)}",
                    ))
                else:
                    results.append(PolicyCheckResult(
                        rule_id="GOV-001",
                        rule_name="Required Resource Tags",
                        passed=True,
                        severity="high",
                        enforcement="block",
                        message=f"All {len(required_tags)} required tags present",
                        resource_type=rtype,
                        resource_name=rname,
                    ))

    # ── GOV-002: Allowed Regions ──────────────────────────────
    allowed_regions = governance_policies.get("allowed_regions", [])
    if allowed_regions:
        for res in resources:
            rtype = res.get("type", "unknown")
            rname = res.get("name", "unknown")
            location = res.get("location", "")

            # ARM expressions like [parameters('location')] and
            # [resourceGroup().location] are always acceptable since they
            # resolve at deployment time to the RG's region (which we control).
            if isinstance(location, str) and location.startswith("["):
                results.append(PolicyCheckResult(
                    rule_id="GOV-002",
                    rule_name="Allowed Deployment Regions",
                    passed=True,
                    severity="critical",
                    enforcement="block",
                    message="Location uses ARM expression (resolved at deploy time)",
                    resource_type=rtype,
                    resource_name=rname,
                ))
            elif isinstance(location, str) and location:
                loc_lower = location.lower().replace(" ", "")
                if loc_lower in [r.lower().replace(" ", "") for r in allowed_regions]:
                    results.append(PolicyCheckResult(
                        rule_id="GOV-002",
                        rule_name="Allowed Deployment Regions",
                        passed=True,
                        severity="critical",
                        enforcement="block",
                        message=f"Location '{location}' is an approved region",
                        resource_type=rtype,
                        resource_name=rname,
                    ))
                else:
                    results.append(PolicyCheckResult(
                        rule_id="GOV-002",
                        rule_name="Allowed Deployment Regions",
                        passed=False,
                        severity="critical",
                        enforcement="block",
                        message=f"Location '{location}' is NOT an approved region. Allowed: {', '.join(allowed_regions)}",
                        resource_type=rtype,
                        resource_name=rname,
                        remediation="Use [parameters('location')] or [resourceGroup().location]",
                    ))
            # Resources without a location (e.g., sub-resources) are OK

    # ── GOV-003 / SEC-001: HTTPS Enforcement ──────────────────
    require_https = governance_policies.get("require_https", False)
    if require_https:
        for res in resources:
            rtype = res.get("type", "").lower()
            rname = res.get("name", "unknown")
            props = res.get("properties", {})

            # App Service
            if "microsoft.web/sites" in rtype:
                https_only = props.get("httpsOnly", False)
                results.append(PolicyCheckResult(
                    rule_id="GOV-003",
                    rule_name="HTTPS Enforcement",
                    passed=bool(https_only),
                    severity="critical",
                    enforcement="block",
                    message="httpsOnly is enabled" if https_only else "httpsOnly is NOT enabled",
                    resource_type=rtype,
                    resource_name=rname,
                    remediation="Set properties.httpsOnly = true" if not https_only else "",
                ))

            # Storage account
            if "microsoft.storage/storageaccounts" in rtype:
                https_only = props.get("supportsHttpsTrafficOnly", False)
                results.append(PolicyCheckResult(
                    rule_id="GOV-003",
                    rule_name="HTTPS Enforcement",
                    passed=bool(https_only),
                    severity="critical",
                    enforcement="block",
                    message="HTTPS-only traffic enabled" if https_only else "HTTPS-only traffic NOT enabled",
                    resource_type=rtype,
                    resource_name=rname,
                    remediation="Set properties.supportsHttpsTrafficOnly = true" if not https_only else "",
                ))

    # ── GOV-004 / SEC-003: Managed Identity ───────────────────
    require_mi = governance_policies.get("require_managed_identity", False)
    if require_mi:
        # Types that should have managed identity
        mi_types = {
            "microsoft.web/sites", "microsoft.containerservice/managedclusters",
            "microsoft.app/containerapps", "microsoft.sql/servers",
            "microsoft.keyvault/vaults", "microsoft.cognitiveservices/accounts",
            "microsoft.machinelearningservices/workspaces",
            "microsoft.documentdb/databaseaccounts",
        }
        for res in resources:
            rtype = res.get("type", "").lower()
            rname = res.get("name", "unknown")

            if rtype in mi_types:
                identity = res.get("identity", {})
                has_mi = identity.get("type") in ("SystemAssigned", "UserAssigned", "SystemAssigned,UserAssigned")

                results.append(PolicyCheckResult(
                    rule_id="GOV-004",
                    rule_name="Managed Identity Enforcement",
                    passed=has_mi,
                    severity="high",
                    enforcement="warn",
                    message="Managed identity configured" if has_mi else "No managed identity configured",
                    resource_type=rtype,
                    resource_name=rname,
                    remediation='Add "identity": {"type": "SystemAssigned"}' if not has_mi else "",
                ))

    # ── GOV-005 / SEC-004: Public Network Access ──────────────
    require_private = governance_policies.get("require_private_endpoints", False)
    if require_private:
        # Types that support publicNetworkAccess
        private_types = {
            "microsoft.sql/servers", "microsoft.keyvault/vaults",
            "microsoft.storage/storageaccounts", "microsoft.cache/redis",
            "microsoft.cognitiveservices/accounts", "microsoft.documentdb/databaseaccounts",
            "microsoft.machinelearningservices/workspaces",
            "microsoft.dbforpostgresql/flexibleservers",
        }
        for res in resources:
            rtype = res.get("type", "").lower()
            rname = res.get("name", "unknown")
            props = res.get("properties", {})

            if rtype in private_types:
                public_access = props.get("publicNetworkAccess", "Enabled")
                is_disabled = str(public_access).lower() in ("disabled", "false")

                results.append(PolicyCheckResult(
                    rule_id="GOV-005",
                    rule_name="Private Endpoints (Production)",
                    passed=is_disabled,
                    severity="high",
                    enforcement="block",
                    message="Public network access disabled" if is_disabled else f"Public network access is '{public_access}'",
                    resource_type=rtype,
                    resource_name=rname,
                    remediation='Set properties.publicNetworkAccess = "Disabled"' if not is_disabled else "",
                ))

    # ── SEC-002: TLS 1.2 Minimum ──────────────────────────────
    _check_tls(resources, results)

    # ── SEC-005: Encryption at Rest ───────────────────────────
    _check_encryption(resources, results)

    # ── SEC-007: Soft Delete / Purge Protection ───────────────
    _check_soft_delete(resources, results)

    # ── SEC-008: RBAC Authorization (Key Vault) ───────────────
    _check_rbac(resources, results)

    # ── SEC-013: Blob Public Access ───────────────────────────
    _check_blob_access(resources, results)

    # ── Build final report ────────────────────────────────────
    passed_count = sum(1 for r in results if r.passed)
    failed_count = sum(1 for r in results if not r.passed)
    blockers = sum(1 for r in results if not r.passed and r.enforcement == "block")
    warnings = sum(1 for r in results if not r.passed and r.enforcement == "warn")

    report = ValidationReport(
        passed=blockers == 0,  # warnings don't block
        total_checks=len(results),
        passed_checks=passed_count,
        failed_checks=failed_count,
        warnings=warnings,
        blockers=blockers,
        results=results,
    )

    logger.info(f"Static policy validation: {report.summary()}")
    return report


# ══════════════════════════════════════════════════════════════
# SECURITY STANDARD CHECKS
# ══════════════════════════════════════════════════════════════

def _check_tls(resources: list, results: list[PolicyCheckResult]):
    """SEC-002: Check TLS 1.2 minimum."""
    tls_props = {
        "microsoft.web/sites": ("siteConfig", "minTlsVersion"),
        "microsoft.sql/servers": (None, "minimalTlsVersion"),
        "microsoft.storage/storageaccounts": (None, "minimumTlsVersion"),
        "microsoft.cache/redis": (None, "minimumTlsVersion"),
        "microsoft.dbforpostgresql/flexibleservers": (None, "minimalTlsVersion"),
    }

    for res in resources:
        rtype = res.get("type", "").lower()
        rname = res.get("name", "unknown")
        props = res.get("properties", {})

        if rtype in tls_props:
            parent_key, tls_key = tls_props[rtype]
            if parent_key:
                tls_val = props.get(parent_key, {}).get(tls_key, "")
            else:
                tls_val = props.get(tls_key, "")

            # Normalize: "1.2", "TLS1_2", "Tls12" → check for 1.2+
            tls_str = str(tls_val).replace("TLS", "").replace("Tls", "").replace("_", ".").replace("1.", "")
            is_ok = tls_str in ("2", "12", "1.2", "3", "13", "1.3") or "1.2" in str(tls_val) or "1_2" in str(tls_val)

            results.append(PolicyCheckResult(
                rule_id="SEC-002",
                rule_name="TLS 1.2 Minimum",
                passed=is_ok,
                severity="critical",
                enforcement="block",
                message=f"TLS version: {tls_val or 'not set'}" + (" (≥1.2 ✓)" if is_ok else " (must be ≥1.2)"),
                resource_type=rtype,
                resource_name=rname,
                remediation=f"Set minTlsVersion/minimumTlsVersion to '1.2'" if not is_ok else "",
            ))


def _check_encryption(resources: list, results: list[PolicyCheckResult]):
    """SEC-005: Check encryption at rest."""
    for res in resources:
        rtype = res.get("type", "").lower()
        rname = res.get("name", "unknown")
        props = res.get("properties", {})

        if "microsoft.storage/storageaccounts" in rtype:
            encryption = props.get("encryption", {})
            has_encryption = bool(encryption.get("services"))

            results.append(PolicyCheckResult(
                rule_id="SEC-005",
                rule_name="Encryption at Rest",
                passed=has_encryption,
                severity="critical",
                enforcement="block",
                message="Storage encryption configured" if has_encryption else "Storage encryption NOT configured",
                resource_type=rtype,
                resource_name=rname,
                remediation="Add encryption.services configuration" if not has_encryption else "",
            ))


def _check_soft_delete(resources: list, results: list[PolicyCheckResult]):
    """SEC-007: Check soft delete / purge protection."""
    for res in resources:
        rtype = res.get("type", "").lower()
        rname = res.get("name", "unknown")
        props = res.get("properties", {})

        if "microsoft.keyvault/vaults" in rtype:
            soft_delete = props.get("enableSoftDelete", False)
            purge_protect = props.get("enablePurgeProtection", False)

            results.append(PolicyCheckResult(
                rule_id="SEC-007",
                rule_name="Soft Delete / Purge Protection",
                passed=bool(soft_delete and purge_protect),
                severity="high",
                enforcement="block",
                message=(
                    f"Soft delete: {'✓' if soft_delete else '✗'}, "
                    f"Purge protection: {'✓' if purge_protect else '✗'}"
                ),
                resource_type=rtype,
                resource_name=rname,
                remediation="Enable both enableSoftDelete and enablePurgeProtection" if not (soft_delete and purge_protect) else "",
            ))


def _check_rbac(resources: list, results: list[PolicyCheckResult]):
    """SEC-008: Check RBAC authorization on Key Vault."""
    for res in resources:
        rtype = res.get("type", "").lower()
        rname = res.get("name", "unknown")
        props = res.get("properties", {})

        if "microsoft.keyvault/vaults" in rtype:
            rbac = props.get("enableRbacAuthorization", False)

            results.append(PolicyCheckResult(
                rule_id="SEC-008",
                rule_name="RBAC Authorization",
                passed=bool(rbac),
                severity="high",
                enforcement="block",
                message="RBAC authorization enabled" if rbac else "Using access policy model (RBAC required)",
                resource_type=rtype,
                resource_name=rname,
                remediation="Set enableRbacAuthorization = true" if not rbac else "",
            ))


def _check_blob_access(resources: list, results: list[PolicyCheckResult]):
    """SEC-013: Check blob public access disabled."""
    for res in resources:
        rtype = res.get("type", "").lower()
        rname = res.get("name", "unknown")
        props = res.get("properties", {})

        if "microsoft.storage/storageaccounts" in rtype:
            blob_public = props.get("allowBlobPublicAccess", True)  # default is True
            is_disabled = not blob_public

            results.append(PolicyCheckResult(
                rule_id="SEC-013",
                rule_name="Blob Public Access Disabled",
                passed=is_disabled,
                severity="critical",
                enforcement="block",
                message="Blob public access disabled" if is_disabled else "Blob public access is ENABLED",
                resource_type=rtype,
                resource_name=rname,
                remediation="Set allowBlobPublicAccess = false" if not is_disabled else "",
            ))


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
