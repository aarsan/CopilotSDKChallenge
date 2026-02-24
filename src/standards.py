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
        changed_by      NVARCHAR(200) DEFAULT 'platform-team',
        changed_at      NVARCHAR(50) NOT NULL,
        change_reason   NVARCHAR(MAX) DEFAULT ''
    )
    """,
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
        "description": "Requests exceeding $5,000/month require manager approval.",
        "category": "cost",
        "severity": "medium",
        "scope": "*",
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
    """Seed default standards if the table is empty, then apply scope fixes."""
    backend = await get_backend()
    rows = await backend.execute(
        "SELECT COUNT(*) as cnt FROM org_standards", ()
    )
    if rows and rows[0]["cnt"] > 0:
        logger.info("Organization standards already seeded — applying scope fixes")
        await _apply_scope_fixes(backend)
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
    return d


async def create_standard(std: dict, created_by: str = "platform-team") -> dict:
    """Create a new organization standard. Returns the created record."""
    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()

    std_id = std.get("id") or f"STD-{_short_hash(std['name'])}"

    await backend.execute_write(
        """INSERT INTO org_standards
           (id, name, description, category, severity, scope,
            rule_json, enabled, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            std_id,
            std["name"],
            std.get("description", ""),
            std["category"],
            std.get("severity", "high"),
            std.get("scope", "*"),
            json.dumps(std.get("rule", {})),
            int(std.get("enabled", True)),
            created_by,
            now,
            now,
        ),
    )

    # Write initial version history
    await backend.execute_write(
        """INSERT INTO org_standards_history
           (standard_id, version, name, description, category,
            severity, scope, rule_json, enabled,
            changed_by, changed_at, change_reason)
           VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Created')""",
        (
            std_id,
            std["name"],
            std.get("description", ""),
            std["category"],
            std.get("severity", "high"),
            std.get("scope", "*"),
            json.dumps(std.get("rule", {})),
            int(std.get("enabled", True)),
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

    await backend.execute_write(
        """UPDATE org_standards
           SET name = ?, description = ?, category = ?, severity = ?,
               scope = ?, rule_json = ?, enabled = ?, updated_at = ?
           WHERE id = ?""",
        (
            name, description, category, severity,
            scope, json.dumps(rule), int(enabled), now,
            standard_id,
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
            severity, scope, rule_json, enabled,
            changed_by, changed_at, change_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            standard_id, next_ver, name, description, category,
            severity, scope, json.dumps(rule), int(enabled),
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
        else:
            lines.append(f"  - {s['name']}: {s['description']}")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════


def _short_hash(text: str) -> str:
    """Generate a short uppercase hash for use in IDs."""
    return hashlib.sha256(text.encode()).hexdigest()[:8].upper()
