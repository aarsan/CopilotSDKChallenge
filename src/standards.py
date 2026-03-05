"""
InfraForge — Organization Standards Engine

Formal, declarative governance standards stored in Azure SQL Database.
Each standard defines what the organization requires (e.g. "All storage must use TLS 1.2+")
and drives policy generation, ARM template hardening, and compliance checks automatically.

Standards are scoped to Azure resource types via glob patterns
(e.g. "Microsoft.Storage/*" matches all storage resource types).

Version history is tracked — every update creates a new version row so
the platform team can audit who changed what and when.
"""

import fnmatch
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from src.database import get_backend, AZURE_SQL_SCHEMA_STATEMENTS

logger = logging.getLogger("infraforge.standards")


# ══════════════════════════════════════════════════════════════
# SQL SCHEMA — appended to the main schema list at import time
# ══════════════════════════════════════════════════════════════

_STANDARDS_SCHEMA = [
    # ── Organization Standards ──
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'org_standards')
    CREATE TABLE org_standards (
        id              NVARCHAR(100) PRIMARY KEY,
        name            NVARCHAR(300) NOT NULL,
        description     NVARCHAR(MAX) DEFAULT '',
        category        NVARCHAR(100) NOT NULL,
        severity        NVARCHAR(50) NOT NULL DEFAULT 'high',
        scope           NVARCHAR(500) NOT NULL DEFAULT '*',
        rule_json       NVARCHAR(MAX) NOT NULL,
        enabled         BIT DEFAULT 1,
        created_by      NVARCHAR(200) DEFAULT 'platform-team',
        created_at      NVARCHAR(50) NOT NULL,
        updated_at      NVARCHAR(50) NOT NULL
    )
    """,
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_org_standards_category')
    CREATE INDEX idx_org_standards_category ON org_standards(category)""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_org_standards_enabled')
    CREATE INDEX idx_org_standards_enabled ON org_standards(enabled)""",
    # ── Add frameworks column (many-to-many: standards ↔ regulatory frameworks) ──
    """IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('org_standards') AND name = 'frameworks')
    ALTER TABLE org_standards ADD frameworks NVARCHAR(MAX) DEFAULT '[]'""",
    # ── CAF alignment: risk_id, purpose, enforcement_tool ──
    """IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('org_standards') AND name = 'risk_id')
    ALTER TABLE org_standards ADD risk_id NVARCHAR(50) DEFAULT ''""",
    """IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('org_standards') AND name = 'purpose')
    ALTER TABLE org_standards ADD purpose NVARCHAR(MAX) DEFAULT ''""",
    """IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('org_standards') AND name = 'enforcement_tool')
    ALTER TABLE org_standards ADD enforcement_tool NVARCHAR(200) DEFAULT ''""",
    # ── Version history for standards ──
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'org_standards_history')
    CREATE TABLE org_standards_history (
        id              INT IDENTITY(1,1) PRIMARY KEY,
        standard_id     NVARCHAR(100) NOT NULL,
        version         INT NOT NULL,
        name            NVARCHAR(300) NOT NULL,
        description     NVARCHAR(MAX) DEFAULT '',
        category        NVARCHAR(100) NOT NULL,
        severity        NVARCHAR(50) NOT NULL,
        scope           NVARCHAR(500) NOT NULL,
        rule_json       NVARCHAR(MAX) NOT NULL,
        enabled         BIT DEFAULT 1,
        frameworks      NVARCHAR(MAX) DEFAULT '[]',
        changed_by      NVARCHAR(200) DEFAULT 'platform-team',
        changed_at      NVARCHAR(50) NOT NULL,
        change_reason   NVARCHAR(MAX) DEFAULT ''
    )
    """,
    """IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('org_standards_history') AND name = 'frameworks')
    ALTER TABLE org_standards_history ADD frameworks NVARCHAR(MAX) DEFAULT '[]'""",
    # ── CAF alignment: risk_id, purpose, enforcement_tool for history table ──
    """IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('org_standards_history') AND name = 'risk_id')
    ALTER TABLE org_standards_history ADD risk_id NVARCHAR(50) DEFAULT ''""",
    """IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('org_standards_history') AND name = 'purpose')
    ALTER TABLE org_standards_history ADD purpose NVARCHAR(MAX) DEFAULT ''""",
    """IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('org_standards_history') AND name = 'enforcement_tool')
    ALTER TABLE org_standards_history ADD enforcement_tool NVARCHAR(200) DEFAULT ''""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_org_standards_hist_sid')
    CREATE INDEX idx_org_standards_hist_sid ON org_standards_history(standard_id)""",
]

# Register schema so init_db() creates the tables automatically
AZURE_SQL_SCHEMA_STATEMENTS.extend(_STANDARDS_SCHEMA)


# ══════════════════════════════════════════════════════════════
# DEFAULT STANDARDS (seeded on first run)
# ══════════════════════════════════════════════════════════════

DEFAULT_STANDARDS: list[dict] = [
    {
        "id": "STD-ENCRYPT-TLS",
        "name": "Require TLS 1.2 Minimum",
        "description": "All services must enforce TLS 1.2 or higher. Older versions are prohibited.",
        "category": "encryption",
        "severity": "critical",
        "scope": "Microsoft.Storage/*,Microsoft.Web/*,Microsoft.Sql/*,Microsoft.DBforPostgreSQL/*,Microsoft.Cache/*,Microsoft.KeyVault/*,Microsoft.Cdn/*",
        "risk_id": "R02",
        "purpose": "Mitigate data interception and man-in-the-middle attacks by enforcing modern transport encryption",
        "enforcement_tool": "Azure Policy",
        "rule": {
            "type": "property",
            "key": "properties.minimumTlsVersion",
            "operator": ">=",
            "value": "1.2",
            "remediation": "Set properties.minimumTlsVersion to 'TLS1_2' in resource properties.",
        },
    },
    {
        "id": "STD-ENCRYPT-HTTPS",
        "name": "HTTPS Required",
        "description": "All web-facing resources must enforce HTTPS. HTTP must be disabled.",
        "category": "encryption",
        "severity": "critical",
        "scope": "Microsoft.Web/*,Microsoft.Storage/*,Microsoft.Cdn/*",
        "risk_id": "R02",
        "purpose": "Protect data in transit by requiring encrypted connections for all web-facing endpoints",
        "enforcement_tool": "Azure Policy",
        "rule": {
            "type": "property",
            "key": "httpsOnly",
            "operator": "==",
            "value": True,
            "remediation": "Set httpsOnly=true. Disable HTTP listeners.",
        },
    },
    {
        "id": "STD-ENCRYPT-REST",
        "name": "Encryption at Rest Required",
        "description": "All data stores must use encryption at rest (TDE, SSE, or CMK).",
        "category": "encryption",
        "severity": "critical",
        "scope": "Microsoft.Sql/*,Microsoft.Storage/*,Microsoft.DocumentDB/*,Microsoft.DBforPostgreSQL/*",
        "risk_id": "R06",
        "purpose": "Protect sensitive data at rest from unauthorized access per regulatory and security requirements",
        "enforcement_tool": "Azure Policy",
        "rule": {
            "type": "property",
            "key": "encryptionAtRest",
            "operator": "==",
            "value": True,
            "remediation": "Enable Transparent Data Encryption or Storage Service Encryption.",
        },
    },
    {
        "id": "STD-IDENTITY-MI",
        "name": "Managed Identity Required",
        "description": "Resources must use managed identities instead of stored credentials, keys, or passwords.",
        "category": "identity",
        "severity": "high",
        "scope": "Microsoft.Compute/*,Microsoft.Web/*,Microsoft.ContainerService/*,Microsoft.App/*,Microsoft.ContainerRegistry/*",
        "risk_id": "R02",
        "purpose": "Eliminate credential exposure risk by using Azure-managed identity lifecycle",
        "enforcement_tool": "Azure Policy",
        "rule": {
            "type": "property",
            "key": "identity.type",
            "operator": "contains",
            "value": "assigned",
            "remediation": "Add an identity block with type 'SystemAssigned' or 'UserAssigned'.",
        },
    },
    {
        "id": "STD-IDENTITY-AAD",
        "name": "Azure AD Authentication Required",
        "description": "Databases and services supporting Azure AD auth must use it instead of local auth.",
        "category": "identity",
        "severity": "high",
        "scope": "Microsoft.Sql/*,Microsoft.DBforPostgreSQL/*,Microsoft.Cache/*",
        "risk_id": "R02",
        "purpose": "Centralize authentication through Microsoft Entra ID for unified access control and audit",
        "enforcement_tool": "Microsoft Entra ID",
        "rule": {
            "type": "property",
            "key": "aadAuthEnabled",
            "operator": "==",
            "value": True,
            "remediation": "Enable Azure AD authentication. Disable or restrict local SQL authentication.",
        },
    },
    {
        "id": "STD-NETWORK-PUBLIC",
        "name": "No Public Access by Default",
        "description": "Resources must deny public network access unless explicitly approved.",
        "category": "network",
        "severity": "high",
        "scope": "Microsoft.Storage/*,Microsoft.Sql/*,Microsoft.KeyVault/*,Microsoft.DocumentDB/*,Microsoft.Web/*,Microsoft.Cache/*,Microsoft.CognitiveServices/*",
        "risk_id": "R02",
        "purpose": "Reduce attack surface by blocking public internet access to cloud resources",
        "enforcement_tool": "Azure Policy",
        "rule": {
            "type": "property",
            "key": "properties.publicNetworkAccess",
            "operator": "==",
            "value": "Disabled",
            "remediation": "Set properties.publicNetworkAccess to 'Disabled'. Configure private endpoints.",
        },
    },
    {
        "id": "STD-NETWORK-PE",
        "name": "Private Endpoints Required (Production)",
        "description": "Production resources must use private endpoints instead of public access.",
        "category": "network",
        "severity": "high",
        "scope": "Microsoft.Sql/*,Microsoft.Storage/*,Microsoft.KeyVault/*,Microsoft.DocumentDB/*",
        "risk_id": "R02",
        "purpose": "Ensure production data flows over private Azure backbone, not public internet",
        "enforcement_tool": "Azure Policy",
        "rule": {
            "type": "property",
            "key": "privateEndpoints",
            "operator": "==",
            "value": True,
            "remediation": "Create a private endpoint in the appropriate VNet/subnet.",
        },
    },
    {
        "id": "STD-MONITOR-DIAG",
        "name": "Diagnostic Logging Required",
        "description": "Diagnostic settings resources must target a Log Analytics workspace.",
        "category": "monitoring",
        "severity": "high",
        "scope": "Microsoft.Insights/diagnosticSettings",
        "risk_id": "R05",
        "purpose": "Ensure operational visibility and incident response capability across all workloads",
        "enforcement_tool": "Azure Policy",
        "rule": {
            "type": "property",
            "key": "properties.workspaceId",
            "operator": "!=",
            "value": "",
            "remediation": "Set properties.workspaceId to a Log Analytics workspace resource ID.",
        },
    },
    {
        "id": "STD-TAG-REQUIRED",
        "name": "Required Resource Tags",
        "description": "All resources must include environment, owner, costCenter, and project tags.",
        "category": "tagging",
        "severity": "high",
        "scope": "*",
        "risk_id": "R07",
        "purpose": "Facilitate resource tracking, cost attribution, and ownership accountability",
        "enforcement_tool": "Azure Policy",
        "rule": {
            "type": "tags",
            "required_tags": ["environment", "owner", "costCenter", "project"],
            "remediation": "Include all required tags on every resource.",
        },
    },
    {
        "id": "STD-REGION-ALLOWED",
        "name": "Allowed Deployment Regions",
        "description": "Resources may only be deployed to approved Azure regions.",
        "category": "geography",
        "severity": "critical",
        "scope": "*",
        "risk_id": "R01",
        "purpose": "Ensure regulatory compliance with data residency requirements and reduce latency",
        "enforcement_tool": "Azure Policy",
        "rule": {
            "type": "allowed_values",
            "key": "location",
            "values": ["eastus2", "westus2", "westeurope"],
            "remediation": "Deploy resources to approved regions only: eastus2, westus2, westeurope.",
        },
    },
    {
        "id": "STD-COST-THRESHOLD",
        "name": "Cost Approval Threshold",
        "description": "Requests exceeding $5,000/month must receive manager approval before provisioning.",
        "category": "cost",
        "severity": "medium",
        "scope": "*",
        "risk_id": "R04",
        "purpose": "Prevent overspending and ensure cost accountability through budget gate controls",
        "enforcement_tool": "Microsoft Cost Management",
        "rule": {
            "type": "cost_threshold",
            "max_monthly_usd": 5000,
            "remediation": "Submit a cost exception request or reduce resource SKU/count.",
        },
    },
]


# ══════════════════════════════════════════════════════════════
# DATABASE OPERATIONS
# ══════════════════════════════════════════════════════════════


# Scope corrections for standards that had overly broad wildcard scopes.
# Maps standard ID → corrected {scope, rule_json} values.
_SCOPE_FIXES: dict[str, dict] = {
    "STD-ENCRYPT-TLS": {
        "scope": "Microsoft.Storage/*,Microsoft.Web/*,Microsoft.Sql/*,Microsoft.DBforPostgreSQL/*,Microsoft.Cache/*,Microsoft.KeyVault/*,Microsoft.Cdn/*",
        "rule_json": json.dumps({
            "type": "property",
            "key": "properties.minimumTlsVersion",
            "operator": ">=",
            "value": "1.2",
            "remediation": "Set properties.minimumTlsVersion to 'TLS1_2' in resource properties.",
        }),
    },
    "STD-IDENTITY-MI": {
        "scope": "Microsoft.Compute/*,Microsoft.Web/*,Microsoft.ContainerService/*,Microsoft.App/*,Microsoft.ContainerRegistry/*",
        "rule_json": json.dumps({
            "type": "property",
            "key": "identity.type",
            "operator": "contains",
            "value": "assigned",
            "remediation": "Add an identity block with type 'SystemAssigned' or 'UserAssigned'.",
        }),
    },
    "STD-NETWORK-PUBLIC": {
        "scope": "Microsoft.Storage/*,Microsoft.Sql/*,Microsoft.KeyVault/*,Microsoft.DocumentDB/*,Microsoft.Web/*,Microsoft.Cache/*,Microsoft.CognitiveServices/*",
        "rule_json": json.dumps({
            "type": "property",
            "key": "properties.publicNetworkAccess",
            "operator": "==",
            "value": "Disabled",
            "remediation": "Set properties.publicNetworkAccess to 'Disabled'. Configure private endpoints.",
        }),
    },
    "STD-MONITOR-DIAG": {
        "scope": "Microsoft.Insights/diagnosticSettings",
        "rule_json": json.dumps({
            "type": "property",
            "key": "properties.workspaceId",
            "operator": "!=",
            "value": "",
            "remediation": "Set properties.workspaceId to a Log Analytics workspace resource ID.",
        }),
    },
    "STD-NAMING-CHARSET": {
        "scope": "*",
        "rule_json": json.dumps({
            "type": "naming_convention",
            "pattern": "^[a-z0-9-]+$",
            "remediation": "Rename the resource to use only lowercase letters, numbers, and hyphens.",
        }),
    },
}


async def init_standards() -> None:
    """Ensure schema migrations are applied. Does NOT auto-seed defaults."""
    backend = await get_backend()
    rows = await backend.execute(
        "SELECT COUNT(*) as cnt FROM org_standards", ()
    )
    if rows and rows[0]["cnt"] > 0:
        logger.info("Organization standards present (%d) — applying migrations", rows[0]["cnt"])
        await _apply_scope_fixes(backend)
        await _apply_caf_fields(backend)
        return

    logger.info("Organization standards table is empty — ready for generation")
    return

    logger.info("Seeding default organization standards...")
    now = datetime.now(timezone.utc).isoformat()

    for std in DEFAULT_STANDARDS:
        await backend.execute_write(
            """INSERT INTO org_standards
               (id, name, description, category, severity, scope,
                rule_json, enabled, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'platform-team', ?, ?)""",
            (
                std["id"],
                std["name"],
                std.get("description", ""),
                std["category"],
                std.get("severity", "high"),
                std.get("scope", "*"),
                json.dumps(std["rule"]),
                now,
                now,
            ),
        )
        # Also write initial version history
        await backend.execute_write(
            """INSERT INTO org_standards_history
               (standard_id, version, name, description, category,
                severity, scope, rule_json, enabled,
                changed_by, changed_at, change_reason)
               VALUES (?, 1, ?, ?, ?, ?, ?, ?, 1, 'platform-team', ?, 'Initial seed')""",
            (
                std["id"],
                std["name"],
                std.get("description", ""),
                std["category"],
                std.get("severity", "high"),
                std.get("scope", "*"),
                json.dumps(std["rule"]),
                now,
            ),
        )

    logger.info(f"Seeded {len(DEFAULT_STANDARDS)} organization standards")


async def _apply_scope_fixes(backend) -> None:
    """Fix overly broad wildcard scopes on existing standards.

    Standards like STD-ENCRYPT-TLS had scope='*' which checked minTlsVersion
    on VNets and NICs (nonsensical). This migration narrows scopes to only
    the resource types each standard actually applies to.
    """
    now = datetime.now(timezone.utc).isoformat()
    fixed = 0

    for std_id, fix in _SCOPE_FIXES.items():
        # Only update if the scope or rule has changed
        rows = await backend.execute(
            "SELECT scope, rule_json FROM org_standards WHERE id = ?", (std_id,)
        )
        if not rows:
            continue

        current_scope = rows[0]["scope"]
        current_rule = rows[0]["rule_json"]
        new_scope = fix["scope"]
        new_rule = fix["rule_json"]

        # Skip if already fixed (both scope and rule match)
        if current_scope == new_scope and current_rule == new_rule:
            continue

        await backend.execute_write(
            """UPDATE org_standards
               SET scope = ?, rule_json = ?, updated_at = ?
               WHERE id = ?""",
            (new_scope, new_rule, now, std_id),
        )
        fixed += 1

    if fixed:
        logger.info(f"Fixed scopes on {fixed} organization standard(s)")
    else:
        logger.info("All standard scopes already correct")


# CAF field defaults for existing default standards (keyed by standard ID).
_CAF_DEFAULTS: dict[str, dict] = {
    "STD-ENCRYPT-TLS":  {"risk_id": "R02", "purpose": "Mitigate data interception and man-in-the-middle attacks by enforcing modern transport encryption", "enforcement_tool": "Azure Policy"},
    "STD-ENCRYPT-HTTPS": {"risk_id": "R02", "purpose": "Protect data in transit by requiring encrypted connections for all web-facing endpoints", "enforcement_tool": "Azure Policy"},
    "STD-ENCRYPT-REST": {"risk_id": "R06", "purpose": "Protect sensitive data at rest from unauthorized access per regulatory and security requirements", "enforcement_tool": "Azure Policy"},
    "STD-IDENTITY-MI":  {"risk_id": "R02", "purpose": "Eliminate credential exposure risk by using Azure-managed identity lifecycle", "enforcement_tool": "Azure Policy"},
    "STD-IDENTITY-AAD": {"risk_id": "R02", "purpose": "Centralize authentication through Microsoft Entra ID for unified access control and audit", "enforcement_tool": "Microsoft Entra ID"},
    "STD-NETWORK-PUBLIC": {"risk_id": "R02", "purpose": "Reduce attack surface by blocking public internet access to cloud resources", "enforcement_tool": "Azure Policy"},
    "STD-NETWORK-PE":   {"risk_id": "R02", "purpose": "Ensure production data flows over private Azure backbone, not public internet", "enforcement_tool": "Azure Policy"},
    "STD-MONITOR-DIAG": {"risk_id": "R05", "purpose": "Ensure operational visibility and incident response capability across all workloads", "enforcement_tool": "Azure Policy"},
    "STD-TAG-REQUIRED": {"risk_id": "R07", "purpose": "Facilitate resource tracking, cost attribution, and ownership accountability", "enforcement_tool": "Azure Policy"},
    "STD-REGION-ALLOWED": {"risk_id": "R01", "purpose": "Ensure regulatory compliance with data residency requirements and reduce latency", "enforcement_tool": "Azure Policy"},
    "STD-COST-THRESHOLD": {"risk_id": "R04", "purpose": "Prevent overspending and ensure cost accountability through budget gate controls", "enforcement_tool": "Microsoft Cost Management"},
}

# Category → default CAF risk mapping for standards not in _CAF_DEFAULTS.
_CAF_CATEGORY_RISK: dict[str, dict] = {
    "encryption":       {"risk_id": "R02", "enforcement_tool": "Azure Policy"},
    "security":         {"risk_id": "R02", "enforcement_tool": "Azure Policy"},
    "identity":         {"risk_id": "R02", "enforcement_tool": "Microsoft Entra ID"},
    "network":          {"risk_id": "R02", "enforcement_tool": "Azure Policy"},
    "tagging":          {"risk_id": "R07", "enforcement_tool": "Azure Policy"},
    "naming":           {"risk_id": "R07", "enforcement_tool": "Azure Policy"},
    "geography":        {"risk_id": "R01", "enforcement_tool": "Azure Policy"},
    "region":           {"risk_id": "R01", "enforcement_tool": "Azure Policy"},
    "cost":             {"risk_id": "R04", "enforcement_tool": "Microsoft Cost Management"},
    "monitoring":       {"risk_id": "R05", "enforcement_tool": "Azure Policy"},
    "operations":       {"risk_id": "R05", "enforcement_tool": "Azure Policy"},
    "data_protection":  {"risk_id": "R06", "enforcement_tool": "Azure Policy"},
    "compliance":       {"risk_id": "R01", "enforcement_tool": "Azure Policy"},
    "compute":          {"risk_id": "R05", "enforcement_tool": "Azure Policy"},
    "general":          {"risk_id": "R07", "enforcement_tool": "Azure Policy"},
}


async def _apply_caf_fields(backend) -> None:
    """Populate Cloud Adoption Framework fields on existing standards.

    Phase 1: Apply exact-match CAF defaults to known standard IDs.
    Phase 2: For any remaining standards missing risk_id, infer from
    their category using _CAF_CATEGORY_RISK so every standard gets
    linked to the CAF risk register.
    """
    now = datetime.now(timezone.utc).isoformat()
    updated = 0

    # Phase 1 — exact-match defaults (risk_id + purpose + enforcement_tool)
    for std_id, caf in _CAF_DEFAULTS.items():
        rows = await backend.execute(
            "SELECT risk_id, purpose, enforcement_tool FROM org_standards WHERE id = ?",
            (std_id,),
        )
        if not rows:
            continue

        current = rows[0]
        if (current.get("risk_id") or "") and (current.get("purpose") or "") and (current.get("enforcement_tool") or ""):
            continue

        await backend.execute_write(
            """UPDATE org_standards
               SET risk_id = ?, purpose = ?, enforcement_tool = ?, updated_at = ?
               WHERE id = ?""",
            (caf["risk_id"], caf["purpose"], caf["enforcement_tool"], now, std_id),
        )
        updated += 1

    # Phase 2 — category-based fallback for any standards still missing risk_id
    orphans = await backend.execute(
        "SELECT id, category FROM org_standards WHERE risk_id IS NULL OR risk_id = ''",
        (),
    )
    for row in orphans:
        cat = (row.get("category") or "general").lower()
        mapping = _CAF_CATEGORY_RISK.get(cat, _CAF_CATEGORY_RISK["general"])
        await backend.execute_write(
            """UPDATE org_standards
               SET risk_id = ?, enforcement_tool = ?, updated_at = ?
               WHERE id = ?""",
            (mapping["risk_id"], mapping["enforcement_tool"], now, row["id"]),
        )
        updated += 1

    if updated:
        logger.info(f"Populated CAF fields on {updated} organization standard(s)")
    else:
        logger.info("All standards already have CAF fields")


async def get_all_standards(
    category: Optional[str] = None,
    enabled_only: bool = False,
) -> list[dict]:
    """Get all organization standards, optionally filtered."""
    backend = await get_backend()
    where_clauses: list[str] = []
    params: list = []

    if enabled_only:
        where_clauses.append("enabled = 1")
    if category:
        where_clauses.append("category = ?")
        params.append(category.lower())

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    rows = await backend.execute(
        f"SELECT * FROM org_standards {where_sql} ORDER BY category, id",
        tuple(params),
    )

    result = []
    for row in rows:
        d = dict(row)
        d["rule"] = json.loads(d.pop("rule_json", "{}"))
        d["enabled"] = bool(d.get("enabled"))
        d["frameworks"] = json.loads(d.get("frameworks") or "[]")
        result.append(d)
    return result


async def get_standard(standard_id: str) -> Optional[dict]:
    """Get a single standard by ID."""
    backend = await get_backend()
    rows = await backend.execute(
        "SELECT * FROM org_standards WHERE id = ?", (standard_id,)
    )
    if not rows:
        return None
    d = dict(rows[0])
    d["rule"] = json.loads(d.pop("rule_json", "{}"))
    d["enabled"] = bool(d.get("enabled"))
    d["frameworks"] = json.loads(d.get("frameworks") or "[]")
    return d


async def create_standard(std: dict, created_by: str = "platform-team") -> dict:
    """Create a new organization standard. Returns the created record."""
    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()

    std_id = std.get("id") or f"STD-{_short_hash(std['name'])}"

    frameworks_json = json.dumps(std.get("frameworks", []))

    await backend.execute_write(
        """INSERT INTO org_standards
           (id, name, description, category, severity, scope,
            rule_json, enabled, frameworks, risk_id, purpose,
            enforcement_tool, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            std_id,
            std["name"],
            std.get("description", ""),
            std["category"],
            std.get("severity", "high"),
            std.get("scope", "*"),
            json.dumps(std.get("rule", {})),
            int(std.get("enabled", True)),
            frameworks_json,
            std.get("risk_id", ""),
            std.get("purpose", ""),
            std.get("enforcement_tool", ""),
            created_by,
            now,
            now,
        ),
    )

    # Write initial version history
    await backend.execute_write(
        """INSERT INTO org_standards_history
           (standard_id, version, name, description, category,
            severity, scope, rule_json, enabled, frameworks,
            risk_id, purpose, enforcement_tool,
            changed_by, changed_at, change_reason)
           VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Created')""",
        (
            std_id,
            std["name"],
            std.get("description", ""),
            std["category"],
            std.get("severity", "high"),
            std.get("scope", "*"),
            json.dumps(std.get("rule", {})),
            int(std.get("enabled", True)),
            frameworks_json,
            std.get("risk_id", ""),
            std.get("purpose", ""),
            std.get("enforcement_tool", ""),
            created_by,
            now,
        ),
    )

    return await get_standard(std_id)


async def update_standard(
    standard_id: str,
    updates: dict,
    changed_by: str = "platform-team",
    change_reason: str = "",
) -> Optional[dict]:
    """Update an existing standard and record version history."""
    backend = await get_backend()
    existing = await get_standard(standard_id)
    if not existing:
        return None

    now = datetime.now(timezone.utc).isoformat()

    # Merge updates
    name = updates.get("name", existing["name"])
    description = updates.get("description", existing["description"])
    category = updates.get("category", existing["category"])
    severity = updates.get("severity", existing["severity"])
    scope = updates.get("scope", existing["scope"])
    rule = updates.get("rule", existing["rule"])
    enabled = updates.get("enabled", existing["enabled"])
    frameworks = updates.get("frameworks", existing.get("frameworks", []))
    frameworks_json = json.dumps(frameworks)
    risk_id = updates.get("risk_id", existing.get("risk_id", ""))
    purpose = updates.get("purpose", existing.get("purpose", ""))
    enforcement_tool = updates.get("enforcement_tool", existing.get("enforcement_tool", ""))

    await backend.execute_write(
        """UPDATE org_standards
           SET name = ?, description = ?, category = ?, severity = ?,
               scope = ?, rule_json = ?, enabled = ?, frameworks = ?,
               risk_id = ?, purpose = ?, enforcement_tool = ?,
               updated_at = ?
           WHERE id = ?""",
        (
            name, description, category, severity,
            scope, json.dumps(rule), int(enabled), frameworks_json,
            risk_id, purpose, enforcement_tool,
            now, standard_id,
        ),
    )

    # Get next version number
    rows = await backend.execute(
        "SELECT MAX(version) as max_ver FROM org_standards_history WHERE standard_id = ?",
        (standard_id,),
    )
    next_ver = (rows[0]["max_ver"] or 0) + 1 if rows else 1

    await backend.execute_write(
        """INSERT INTO org_standards_history
           (standard_id, version, name, description, category,
            severity, scope, rule_json, enabled, frameworks,
            risk_id, purpose, enforcement_tool,
            changed_by, changed_at, change_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            standard_id, next_ver, name, description, category,
            severity, scope, json.dumps(rule), int(enabled),
            frameworks_json, risk_id, purpose, enforcement_tool,
            changed_by, now, change_reason,
        ),
    )

    return await get_standard(standard_id)


async def delete_standard(standard_id: str) -> bool:
    """Delete a standard and its version history."""
    backend = await get_backend()
    await backend.execute_write(
        "DELETE FROM org_standards_history WHERE standard_id = ?",
        (standard_id,),
    )
    count = await backend.execute_write(
        "DELETE FROM org_standards WHERE id = ?",
        (standard_id,),
    )
    return count > 0


async def delete_all_standards() -> int:
    """Delete ALL standards and their version history. Returns count deleted."""
    backend = await get_backend()
    await backend.execute_write("DELETE FROM org_standards_history", ())
    count = await backend.execute_write("DELETE FROM org_standards", ())
    return count


async def delete_standards_bulk(standard_ids: list[str]) -> int:
    """Delete multiple standards by ID. Returns count deleted."""
    if not standard_ids:
        return 0
    backend = await get_backend()
    deleted = 0
    for sid in standard_ids:
        await backend.execute_write(
            "DELETE FROM org_standards_history WHERE standard_id = ?",
            (sid,),
        )
        c = await backend.execute_write(
            "DELETE FROM org_standards WHERE id = ?",
            (sid,),
        )
        deleted += c
    return deleted


async def get_standard_history(standard_id: str) -> list[dict]:
    """Get the version history for a standard."""
    backend = await get_backend()
    rows = await backend.execute(
        """SELECT * FROM org_standards_history
           WHERE standard_id = ? ORDER BY version DESC""",
        (standard_id,),
    )
    result = []
    for row in rows:
        d = dict(row)
        d["rule"] = json.loads(d.pop("rule_json", "{}"))
        d["enabled"] = bool(d.get("enabled"))
        d["frameworks"] = json.loads(d.get("frameworks") or "[]")
        result.append(d)
    return result


async def get_standards_categories() -> list[str]:
    """Get distinct categories from standards."""
    backend = await get_backend()
    rows = await backend.execute(
        "SELECT DISTINCT category FROM org_standards ORDER BY category", ()
    )
    return [r["category"] for r in rows]


# ══════════════════════════════════════════════════════════════
# SCOPE MATCHING
# ══════════════════════════════════════════════════════════════


def _scope_matches(scope: str, resource_type: str) -> bool:
    """Check if a resource type matches the standard's scope pattern.

    Scope is a comma-separated list of glob patterns, e.g.:
      "*"                             — matches everything
      "Microsoft.Storage/*"           — matches all storage types
      "Microsoft.Sql/*,Microsoft.DBforPostgreSQL/*" — matches SQL + PG
    """
    resource_lower = resource_type.lower()
    for pattern in scope.split(","):
        pattern = pattern.strip().lower()
        if not pattern:
            continue
        if fnmatch.fnmatch(resource_lower, pattern):
            return True
    return False


async def get_standards_for_service(service_id: str) -> list[dict]:
    """Get all enabled standards that apply to a given service resource type."""
    all_stds = await get_all_standards(enabled_only=True)
    return [s for s in all_stds if _scope_matches(s.get("scope", "*"), service_id)]


# ══════════════════════════════════════════════════════════════
# PROMPT BUILDERS — feed standards into AI generation
# ══════════════════════════════════════════════════════════════


async def build_policy_generation_context(service_id: str) -> str:
    """Build a text block for the Copilot SDK prompt when generating policies.

    Returns a formatted string listing all applicable standards so the AI
    can generate per-service policies that comply with org governance.
    """
    standards = await get_standards_for_service(service_id)
    if not standards:
        return "No organization standards apply to this service type."

    lines = [
        f"Organization Standards for {service_id}:",
        f"({len(standards)} standards apply)",
        "",
    ]
    for s in standards:
        rule = s.get("rule", {})
        lines.append(f"  [{s['severity'].upper()}] {s['name']}")
        lines.append(f"    {s['description']}")
        risk_id = s.get("risk_id", "")
        purpose = s.get("purpose", "")
        enforcement_tool = s.get("enforcement_tool", "")
        if risk_id:
            lines.append(f"    Risk: {risk_id}")
        if purpose:
            lines.append(f"    Purpose: {purpose}")
        if enforcement_tool:
            lines.append(f"    Enforcement: {enforcement_tool}")
        if rule.get("remediation"):
            lines.append(f"    Remediation: {rule['remediation']}")
        lines.append("")

    return "\n".join(lines)


async def build_arm_generation_context(service_id: str) -> str:
    """Build a text block for the Copilot SDK prompt when generating ARM templates.

    Includes specific property requirements that the ARM template must satisfy.
    """
    standards = await get_standards_for_service(service_id)
    if not standards:
        return ""

    lines = [
        "MANDATORY REQUIREMENTS from organization standards — the generated ARM template MUST satisfy ALL of these:",
        "",
    ]
    for s in standards:
        rule = s.get("rule", {})
        rule_type = rule.get("type", "property")

        if rule_type == "property":
            lines.append(
                f"  - {s['name']}: Set {rule.get('key', '?')} "
                f"{rule.get('operator', '==')} {json.dumps(rule.get('value', True))}"
            )
        elif rule_type == "tags":
            tags = rule.get("required_tags", [])
            lines.append(f"  - {s['name']}: Include tags: {', '.join(tags)}")
        elif rule_type == "allowed_values":
            vals = rule.get("values", [])
            lines.append(
                f"  - {s['name']}: {rule.get('key', '?')} must be one of: {', '.join(str(v) for v in vals)}"
            )
        elif rule_type == "cost_threshold":
            lines.append(
                f"  - {s['name']}: Monthly cost must not exceed ${rule.get('max_monthly_usd', 0)}"
            )
        elif rule_type == "naming_convention":
            pattern = rule.get("pattern", "")
            examples = rule.get("examples", [])
            lines.append(f"  - {s['name']}: Resource names MUST match pattern: {pattern}")
            if examples:
                lines.append(f"    Examples: {', '.join(examples)}")
            if rule.get("remediation"):
                lines.append(f"    Remediation: {rule['remediation']}")
        else:
            lines.append(f"  - {s['name']}: {s['description']}")

    return "\n".join(lines)


async def build_governance_generation_context() -> str:
    """Build a security & governance requirements block for ARM template generation.

    Fetches security standards and governance policies from the database and
    formats them as explicit, actionable requirements that the ARM generator
    MUST follow. This ensures the generator is aware of CISO-level security
    expectations BEFORE building the template — not after.
    """
    from src.database import get_security_standards, get_governance_policies

    standards = await get_security_standards(enabled_only=True)
    policies = await get_governance_policies(enabled_only=True)

    if not standards and not policies:
        return ""

    lines = [
        "MANDATORY SECURITY & GOVERNANCE REQUIREMENTS — the generated ARM template MUST comply with ALL of these.",
        "These are enforced by the CISO reviewer and violations WILL block deployment.",
        "",
    ]

    if standards:
        lines.append("## Security Standards")
        for std in standards:
            sev = std.get("severity", "medium").upper()
            lines.append(f"  - [{sev}] {std.get('name', std['id'])}: {std.get('description', '')}")
            remediation = std.get("remediation", "")
            if remediation:
                lines.append(f"    Remediation: {remediation}")
        lines.append("")

    if policies:
        lines.append("## Governance Policies")
        for pol in policies:
            enf = pol.get("enforcement", "warn").upper()
            lines.append(f"  - [{enf}] {pol.get('name', pol['id'])}: {pol.get('description', '')}")
        lines.append("")

    # Add explicit CISO review criteria so the generator knows what will be checked
    lines.extend([
        "## CISO Review Criteria (the template WILL be reviewed against all of these)",
        "  - NEVER include hardcoded passwords, secrets, API keys, or connection strings in parameters or variables",
        "  - Password parameters MUST use secureString type with NO defaultValue",
        "  - Use SSH public keys instead of password authentication for Linux VMs",
        "  - Enable disk encryption (Azure Disk Encryption or EncryptionAtHost) on all VMs and disks",
        "  - Associate Network Security Groups (NSGs) with all subnets and NICs",
        "  - Use managed identities (SystemAssigned or UserAssigned) instead of stored credentials",
        "  - Enable encryption at rest for all storage and database resources",
        "  - Enforce TLS 1.2+ and HTTPS-only on all applicable resources",
        "  - Disable public network access unless explicitly required",
        "  - Enable diagnostic settings and monitoring where supported",
        "  - Include proper resource tagging (environment, owner, costCenter, project)",
        "  - Use private endpoints for PaaS services where applicable",
        "",
    ])

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════


def _short_hash(text: str) -> str:
    """Generate a short uppercase hash for use in IDs."""
    return hashlib.sha256(text.encode()).hexdigest()[:8].upper()
