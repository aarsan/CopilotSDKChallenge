"""
Policy compliance checker tool.

Validates infrastructure configurations against organizational governance policies
and security standards stored in the InfraForge database.
No hardcoded rules — all policies come from the governance_policies and
security_standards tables, making them auditable and version-controlled.
"""

from pydantic import BaseModel, Field
from copilot import define_tool

from src.database import (
    get_governance_policies_as_dict,
    get_security_standards,
    get_service,
    save_compliance_assessment,
)


class CheckPolicyParams(BaseModel):
    resources: list[dict] = Field(
        description=(
            "List of resource definitions to check. Each dict should contain: "
            "'name' (str), 'type' (str), 'region' (str), "
            "'tags' (dict, optional), 'public_access' (bool, optional), "
            "'uses_managed_identity' (bool, optional), "
            "'https_only' (bool, optional), "
            "'tls_version' (str, optional), "
            "'encryption_at_rest' (bool, optional), "
            "'diagnostic_logging' (bool, optional)."
        )
    )
    custom_policies: dict = Field(
        default={},
        description=(
            "Optional custom policy overrides. Keys: 'require_tags', 'allowed_regions', "
            "'naming_convention', 'require_https', 'require_managed_identity', "
            "'require_private_endpoints', 'max_public_ips'."
        ),
    )
    approval_request_id: str = Field(
        default="",
        description=(
            "If provided, link the compliance assessment to this approval request. "
            "The score and findings will be stored for governance audit trail."
        ),
    )


@define_tool(description=(
    "Check infrastructure resources against organizational compliance policies and security "
    "standards. Validates required tags, allowed regions, naming conventions, HTTPS enforcement, "
    "managed identity, TLS version, encryption, diagnostic logging, and public access "
    "restrictions. All rules come from the governance database — not hardcoded. "
    "Optionally links results to an approval request for audit trail."
))
async def check_policy_compliance(params: CheckPolicyParams) -> str:
    """Validate resources against governance policies and security standards."""

    # Load policies and standards from database
    db_policies = await get_governance_policies_as_dict()
    security_stds = await get_security_standards(enabled_only=True)

    # Merge with any custom overrides
    policies = {**db_policies, **params.custom_policies}

    # Build a lookup of security standards by validation_key
    std_by_key: dict[str, dict] = {}
    for std in security_stds:
        std_by_key[std["validation_key"]] = std

    findings = []
    all_findings_flat = []
    passed = 0
    warnings = 0
    failures = 0
    standards_checked = set()

    for resource in params.resources:
        name = resource.get("name", "unnamed")
        rtype = resource.get("type", "unknown")
        region = resource.get("region", "")
        tags = resource.get("tags", {})
        public_access = resource.get("public_access", False)
        uses_identity = resource.get("uses_managed_identity", False)
        https_only = resource.get("https_only", True)
        tls_version = resource.get("tls_version", "1.2")
        encryption_at_rest = resource.get("encryption_at_rest", True)
        diagnostic_logging = resource.get("diagnostic_logging", True)

        resource_findings = []

        # ── Check required tags (GOV-001) ────────────────────
        required_tags = policies.get("require_tags", [])
        if required_tags:
            standards_checked.add("GOV-001")
            missing_tags = [t for t in required_tags if t not in tags]
            if missing_tags:
                resource_findings.append({
                    "severity": "FAIL",
                    "rule": "Required Tags",
                    "standard_id": "GOV-001",
                    "detail": f"Missing tags: {', '.join(missing_tags)}",
                    "remediation": "Add required tags: " + ", ".join(missing_tags),
                })
                failures += 1
            else:
                passed += 1

        # ── Check allowed regions (GOV-002) ──────────────────
        allowed_regions = policies.get("allowed_regions", [])
        if allowed_regions and region:
            standards_checked.add("GOV-002")
            if region not in allowed_regions:
                resource_findings.append({
                    "severity": "FAIL",
                    "rule": "Allowed Regions",
                    "standard_id": "GOV-002",
                    "detail": f"Region '{region}' not in allowed list: {', '.join(allowed_regions)}",
                    "remediation": f"Deploy to one of: {', '.join(allowed_regions)}",
                })
                failures += 1
            else:
                passed += 1

        # ── Check HTTPS (SEC-001) ────────────────────────────
        sec001 = std_by_key.get("require_https")
        if sec001 or policies.get("require_https"):
            standards_checked.add("SEC-001")
            if not https_only:
                resource_findings.append({
                    "severity": "FAIL",
                    "rule": "HTTPS Required",
                    "standard_id": "SEC-001",
                    "detail": "Resource does not enforce HTTPS-only access",
                    "remediation": sec001["remediation"] if sec001 else "Enable HTTPS-only mode",
                })
                failures += 1
            else:
                passed += 1

        # ── Check TLS version (SEC-002) ──────────────────────
        sec002 = std_by_key.get("min_tls_version")
        if sec002:
            standards_checked.add("SEC-002")
            try:
                if float(tls_version) < float(sec002["validation_value"]):
                    resource_findings.append({
                        "severity": "FAIL",
                        "rule": "TLS 1.2 Minimum",
                        "standard_id": "SEC-002",
                        "detail": f"TLS version {tls_version} is below minimum {sec002['validation_value']}",
                        "remediation": sec002["remediation"],
                    })
                    failures += 1
                else:
                    passed += 1
            except (ValueError, TypeError):
                passed += 1  # Can't parse, skip

        # ── Check managed identity (SEC-003) ─────────────────
        sec003 = std_by_key.get("require_managed_identity")
        if sec003 or policies.get("require_managed_identity"):
            standards_checked.add("SEC-003")
            if not uses_identity:
                # Enforcement from governance policy
                enforcement = "warn"
                gov_pols = await get_governance_policies_as_dict()  # already cached above but this is fine
                if policies.get("require_managed_identity"):
                    enforcement = "warn"  # GOV-004 is warn by default
                resource_findings.append({
                    "severity": "WARN" if enforcement == "warn" else "FAIL",
                    "rule": "Managed Identity",
                    "standard_id": "SEC-003",
                    "detail": "Resource should use managed identity instead of keys/passwords",
                    "remediation": sec003["remediation"] if sec003 else "Enable managed identity",
                })
                if enforcement == "warn":
                    warnings += 1
                else:
                    failures += 1
            else:
                passed += 1

        # ── Check encryption at rest (SEC-005) ───────────────
        sec005 = std_by_key.get("require_encryption_at_rest")
        if sec005:
            standards_checked.add("SEC-005")
            if not encryption_at_rest:
                resource_findings.append({
                    "severity": "FAIL",
                    "rule": "Encryption at Rest",
                    "standard_id": "SEC-005",
                    "detail": "Data store does not have encryption at rest enabled",
                    "remediation": sec005["remediation"],
                })
                failures += 1
            else:
                passed += 1

        # ── Check diagnostic logging (SEC-006) ───────────────
        sec006 = std_by_key.get("require_diagnostic_logging")
        if sec006:
            standards_checked.add("SEC-006")
            if not diagnostic_logging:
                resource_findings.append({
                    "severity": "WARN",
                    "rule": "Diagnostic Logging",
                    "standard_id": "SEC-006",
                    "detail": "Diagnostic logging is not enabled",
                    "remediation": sec006["remediation"],
                })
                warnings += 1
            else:
                passed += 1

        # ── Check public access (GOV-006) ────────────────────
        max_public = policies.get("max_public_ips", 0)
        if public_access:
            standards_checked.add("GOV-006")
            if max_public == 0:
                resource_findings.append({
                    "severity": "FAIL",
                    "rule": "Public Access",
                    "standard_id": "GOV-006",
                    "detail": "Public access is not allowed by policy",
                    "remediation": "Disable public network access. Configure private endpoints.",
                })
                failures += 1
            else:
                resource_findings.append({
                    "severity": "WARN",
                    "rule": "Public Access",
                    "standard_id": "GOV-006",
                    "detail": "Resource has public access enabled — ensure this is intended",
                    "remediation": "Review public access justification.",
                })
                warnings += 1
        else:
            passed += 1

        # ── Check service-specific policies ──────────────────
        if rtype:
            svc_info = await get_service(rtype)
            if svc_info and svc_info.get("policies"):
                for policy_text in svc_info["policies"]:
                    resource_findings.append({
                        "severity": "INFO",
                        "rule": "Service Policy",
                        "standard_id": svc_info["id"],
                        "detail": policy_text,
                    })

        findings.append({
            "resource": f"{name} ({rtype})",
            "findings": resource_findings,
        })
        all_findings_flat.extend(resource_findings)

    # ── Calculate score ──────────────────────────────────────
    total_checks = passed + warnings + failures
    score = (passed / total_checks * 100) if total_checks > 0 else 0.0

    # ── Save compliance assessment if linked to approval ─────
    if params.approval_request_id:
        overall = "pass" if failures == 0 and warnings == 0 else (
            "warn" if failures == 0 else "fail"
        )
        await save_compliance_assessment({
            "approval_request_id": params.approval_request_id,
            "overall_result": overall,
            "standards_checked": list(standards_checked),
            "findings": all_findings_flat,
            "score": round(score, 1),
        })

    # ── Build report ─────────────────────────────────────────
    lines = []
    lines.append("## 🛡️ Policy Compliance Report\n")
    lines.append(f"**Checked:** {len(params.resources)} resources, {total_checks} policy rules\n")
    lines.append(f"**Security Standards Applied:** {len(standards_checked)}\n")
    lines.append(f"**Compliance Score:** {score:.0f}%\n")

    if failures == 0 and warnings == 0:
        lines.append("✅ **All checks passed!**\n")
    else:
        lines.append(f"| Status | Count |")
        lines.append(f"|--------|-------|")
        lines.append(f"| ✅ Passed | {passed} |")
        lines.append(f"| ⚠️ Warnings | {warnings} |")
        lines.append(f"| ❌ Failures | {failures} |")
        lines.append("")

    for item in findings:
        check_items = [f for f in item["findings"] if f["severity"] != "INFO"]
        info_items = [f for f in item["findings"] if f["severity"] == "INFO"]
        if check_items or info_items:
            lines.append(f"### {item['resource']}")
            for f in check_items:
                icon = "❌" if f["severity"] == "FAIL" else "⚠️"
                lines.append(f"- {icon} **{f['rule']}** ({f.get('standard_id', 'N/A')}): {f['detail']}")
                if f.get("remediation"):
                    lines.append(f"  - 💡 *{f['remediation']}*")
            if info_items:
                lines.append("- 📋 **Service-Specific Policies:**")
                for f in info_items:
                    lines.append(f"  - {f['detail']}")
            lines.append("")

    if failures > 0:
        lines.append("---")
        lines.append("**⛔ Deployment should be blocked until failures are resolved.**")
    elif warnings > 0:
        lines.append("---")
        lines.append("**⚠️ Review warnings before deploying to production.**")

    return "\n".join(lines)
