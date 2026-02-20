"""
InfraForge — Database Layer (Azure SQL Database)

All persistent data lives in Azure SQL Database with Azure AD authentication
via DefaultAzureCredential (managed identity in Azure, Azure CLI locally).

Requires AZURE_SQL_CONNECTION_STRING to be set in the environment.

Tables:
  user_sessions            — Auth sessions (persists across server restarts)
  chat_messages            — Conversation history
  usage_logs               — Work IQ analytics
  approval_requests        — Service approval requests with lifecycle tracking
  projects                 — Infrastructure project proposals and phase tracking
  security_standards       — Machine-readable security rules (HTTPS, TLS, managed identity...)
  compliance_frameworks    — Compliance framework definitions (SOC2, HIPAA, CIS...)
  compliance_controls      — Individual controls within frameworks
  services                 — Approved Azure services catalog (with active_version)
  service_versions         — Versioned ARM templates per service (v1, v2, v3...)
  service_policies         — Per-service policy requirements (legacy)
  service_approved_skus    — Approved SKUs per service
  service_approved_regions — Approved regions per service
  governance_policies      — Organization-wide governance rules (source of truth for validation)
  compliance_assessments   — Results of compliance checks against approval requests
"""

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("infraforge.database")


# ══════════════════════════════════════════════════════════════
# ABSTRACT BACKEND INTERFACE
# ══════════════════════════════════════════════════════════════

class DatabaseBackend(ABC):
    """Abstract database backend."""

    @abstractmethod
    async def init(self) -> None:
        """Initialize the database (create tables if needed)."""
        ...

    @abstractmethod
    async def execute(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a query and return rows as dicts."""
        ...

    @abstractmethod
    async def execute_write(self, sql: str, params: tuple = ()) -> int:
        """Execute an INSERT/UPDATE/DELETE. Returns rowcount."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close connections / cleanup."""
        ...


# ══════════════════════════════════════════════════════════════
# AZURE SQL DATABASE BACKEND
# ══════════════════════════════════════════════════════════════

class AzureSQLBackend(DatabaseBackend):
    """Azure SQL Database backend.

    Connects using Azure AD authentication (DefaultAzureCredential),
    which automatically picks up:
    - Managed identity (in Azure)
    - Azure CLI credentials (local dev)
    - Environment variables (CI/CD)
    """

    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self._credential = None
        self._token = None

    def _get_token_struct(self):
        """Get (or refresh) an Azure AD token, encoded for pyodbc."""
        import struct
        import time
        from azure.identity import DefaultAzureCredential

        # Lazily create the credential (reused across calls)
        if self._credential is None:
            # Exclude credential types that don't apply and slow down auth
            self._credential = DefaultAzureCredential(
                exclude_workload_identity_credential=True,
                exclude_managed_identity_credential=True,
                exclude_developer_cli_credential=True,
                exclude_powershell_credential=True,
                exclude_visual_studio_code_credential=True,
                exclude_interactive_browser_credential=True,
            )

        # Refresh token if expired or not yet fetched (5-min buffer)
        if self._token is None or self._token.expires_on < time.time() + 300:
            self._token = self._credential.get_token(
                "https://database.windows.net/.default"
            )

        token_bytes = self._token.token.encode("utf-16-le")
        return struct.pack(
            f"<I{len(token_bytes)}s", len(token_bytes), token_bytes
        )

    async def init(self) -> None:
        import pyodbc

        token_struct = self._get_token_struct()

        conn = pyodbc.connect(
            self.connection_string,
            attrs_before={1256: token_struct},  # SQL_COPT_SS_ACCESS_TOKEN
        )
        try:
            cursor = conn.cursor()
            # Create tables if they don't exist (T-SQL syntax)
            for statement in AZURE_SQL_SCHEMA_STATEMENTS:
                try:
                    cursor.execute(statement)
                except pyodbc.ProgrammingError:
                    pass  # Table already exists
            conn.commit()
            logger.info("Azure SQL Database initialized")
        finally:
            conn.close()

    def _get_connection(self):
        """Get a SQL connection with cached Azure AD token auth."""
        import pyodbc

        token_struct = self._get_token_struct()
        return pyodbc.connect(
            self.connection_string,
            attrs_before={1256: token_struct},
        )

    async def execute(self, sql: str, params: tuple = ()) -> list[dict]:
        import asyncio

        def _run():
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                if cursor.description:
                    columns = [col[0] for col in cursor.description]
                    return [dict(zip(columns, row)) for row in cursor.fetchall()]
                return []
            finally:
                conn.close()

        return await asyncio.get_event_loop().run_in_executor(None, _run)

    async def execute_write(self, sql: str, params: tuple = ()) -> int:
        import asyncio

        def _run():
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()

        return await asyncio.get_event_loop().run_in_executor(None, _run)

    async def close(self) -> None:
        pass


# ══════════════════════════════════════════════════════════════
# SCHEMA DEFINITION (Azure SQL — T-SQL)
# ══════════════════════════════════════════════════════════════

# Azure SQL schema (T-SQL — individual statements)
AZURE_SQL_SCHEMA_STATEMENTS = [
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'user_sessions')
    CREATE TABLE user_sessions (
        session_token   NVARCHAR(200) PRIMARY KEY,
        user_id         NVARCHAR(200) NOT NULL,
        display_name    NVARCHAR(200) NOT NULL,
        email           NVARCHAR(200) NOT NULL,
        job_title       NVARCHAR(200) DEFAULT '',
        department      NVARCHAR(200) DEFAULT '',
        cost_center     NVARCHAR(100) DEFAULT '',
        manager         NVARCHAR(200) DEFAULT '',
        groups_json     NVARCHAR(MAX) DEFAULT '[]',
        roles_json      NVARCHAR(MAX) DEFAULT '[]',
        team            NVARCHAR(200) DEFAULT '',
        is_platform_team BIT DEFAULT 0,
        is_admin        BIT DEFAULT 0,
        access_token    NVARCHAR(MAX) DEFAULT '',
        claims_json     NVARCHAR(MAX) DEFAULT '{}',
        created_at      FLOAT NOT NULL,
        expires_at      FLOAT NOT NULL
    )
    """,
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'chat_messages')
    CREATE TABLE chat_messages (
        id              INT IDENTITY(1,1) PRIMARY KEY,
        session_token   NVARCHAR(200) NOT NULL,
        role            NVARCHAR(20) NOT NULL,
        content         NVARCHAR(MAX) NOT NULL,
        created_at      FLOAT NOT NULL,
        FOREIGN KEY (session_token) REFERENCES user_sessions(session_token) ON DELETE CASCADE
    )
    """,
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'usage_logs')
    CREATE TABLE usage_logs (
        id                  INT IDENTITY(1,1) PRIMARY KEY,
        timestamp           FLOAT NOT NULL,
        user_email          NVARCHAR(200) NOT NULL,
        department          NVARCHAR(200) DEFAULT '',
        cost_center         NVARCHAR(100) DEFAULT '',
        prompt              NVARCHAR(MAX) DEFAULT '',
        resource_types_json NVARCHAR(MAX) DEFAULT '[]',
        estimated_cost      FLOAT DEFAULT 0.0,
        from_catalog        BIT DEFAULT 0
    )
    """,
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'approval_requests')
    CREATE TABLE approval_requests (
        id                      NVARCHAR(100) PRIMARY KEY,
        service_name            NVARCHAR(200) NOT NULL,
        service_resource_type   NVARCHAR(200) DEFAULT 'unknown',
        current_status          NVARCHAR(100) DEFAULT 'not_in_catalog',
        risk_tier               NVARCHAR(50) DEFAULT 'medium',
        business_justification  NVARCHAR(MAX) NOT NULL,
        project_name            NVARCHAR(200) NOT NULL,
        environment             NVARCHAR(50) DEFAULT 'production',
        requestor_name          NVARCHAR(200) DEFAULT '',
        requestor_email         NVARCHAR(200) DEFAULT '',
        status                  NVARCHAR(50) DEFAULT 'submitted',
        submitted_at            NVARCHAR(50) NOT NULL,
        reviewed_at             NVARCHAR(50),
        reviewer                NVARCHAR(200),
        review_notes            NVARCHAR(MAX),
        compliance_assessment_id NVARCHAR(100),
        security_score          FLOAT,
        compliance_results_json NVARCHAR(MAX) DEFAULT '{}'
    )
    """,
    # ── Governance: Security Standards ──
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'security_standards')
    CREATE TABLE security_standards (
        id              NVARCHAR(100) PRIMARY KEY,
        name            NVARCHAR(200) NOT NULL,
        description     NVARCHAR(MAX) DEFAULT '',
        category        NVARCHAR(100) NOT NULL,
        severity        NVARCHAR(50) NOT NULL DEFAULT 'high',
        validation_key  NVARCHAR(200) NOT NULL,
        validation_value NVARCHAR(MAX) NOT NULL DEFAULT 'true',
        remediation     NVARCHAR(MAX) DEFAULT '',
        enabled         BIT DEFAULT 1,
        created_at      NVARCHAR(50) NOT NULL,
        updated_at      NVARCHAR(50) NOT NULL
    )
    """,
    # ── Governance: Compliance Frameworks ──
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'compliance_frameworks')
    CREATE TABLE compliance_frameworks (
        id          NVARCHAR(100) PRIMARY KEY,
        name        NVARCHAR(200) NOT NULL,
        description NVARCHAR(MAX) DEFAULT '',
        version     NVARCHAR(50) DEFAULT '1.0',
        enabled     BIT DEFAULT 1,
        created_at  NVARCHAR(50) NOT NULL
    )
    """,
    # ── Governance: Compliance Controls ──
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'compliance_controls')
    CREATE TABLE compliance_controls (
        id                      NVARCHAR(100) PRIMARY KEY,
        framework_id            NVARCHAR(100) NOT NULL,
        control_id              NVARCHAR(100) NOT NULL,
        name                    NVARCHAR(200) NOT NULL,
        description             NVARCHAR(MAX) DEFAULT '',
        category                NVARCHAR(100) DEFAULT '',
        security_standard_ids_json NVARCHAR(MAX) DEFAULT '[]',
        created_at              NVARCHAR(50) NOT NULL,
        FOREIGN KEY (framework_id) REFERENCES compliance_frameworks(id)
    )
    """,
    # ── Governance: Azure Services Catalog ──
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'services')
    CREATE TABLE services (
        id              NVARCHAR(200) PRIMARY KEY,
        name            NVARCHAR(200) NOT NULL,
        category        NVARCHAR(100) NOT NULL DEFAULT 'other',
        status          NVARCHAR(50) NOT NULL DEFAULT 'not_approved',
        risk_tier       NVARCHAR(50) NOT NULL DEFAULT 'medium',
        conditions_json NVARCHAR(MAX) DEFAULT '[]',
        review_notes    NVARCHAR(MAX) DEFAULT '',
        documentation   NVARCHAR(500) DEFAULT '',
        contact         NVARCHAR(200) DEFAULT '',
        rejection_reason NVARCHAR(MAX) DEFAULT '',
        approved_date   NVARCHAR(50) DEFAULT '',
        reviewed_by     NVARCHAR(200) DEFAULT '',
        created_at      NVARCHAR(50) NOT NULL,
        updated_at      NVARCHAR(50) NOT NULL
    )
    """,
    # ── Governance: Per-service policies ──
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'service_policies')
    CREATE TABLE service_policies (
        id              INT IDENTITY(1,1) PRIMARY KEY,
        service_id      NVARCHAR(200) NOT NULL,
        policy_text     NVARCHAR(MAX) NOT NULL,
        security_standard_id NVARCHAR(100),
        enabled         BIT DEFAULT 1,
        FOREIGN KEY (service_id) REFERENCES services(id),
        FOREIGN KEY (security_standard_id) REFERENCES security_standards(id)
    )
    """,
    # ── Governance: Approved SKUs ──
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'service_approved_skus')
    CREATE TABLE service_approved_skus (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        service_id  NVARCHAR(200) NOT NULL,
        sku         NVARCHAR(100) NOT NULL,
        FOREIGN KEY (service_id) REFERENCES services(id)
    )
    """,
    # ── Governance: Approved Regions ──
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'service_approved_regions')
    CREATE TABLE service_approved_regions (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        service_id  NVARCHAR(200) NOT NULL,
        region      NVARCHAR(100) NOT NULL,
        FOREIGN KEY (service_id) REFERENCES services(id)
    )
    """,
    # ── Governance: Organization-wide policies ──
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'governance_policies')
    CREATE TABLE governance_policies (
        id          NVARCHAR(100) PRIMARY KEY,
        name        NVARCHAR(200) NOT NULL,
        description NVARCHAR(MAX) DEFAULT '',
        category    NVARCHAR(100) NOT NULL,
        rule_key    NVARCHAR(200) NOT NULL,
        rule_value_json NVARCHAR(MAX) NOT NULL,
        severity    NVARCHAR(50) NOT NULL DEFAULT 'high',
        enforcement NVARCHAR(50) NOT NULL DEFAULT 'block',
        enabled     BIT DEFAULT 1,
        created_at  NVARCHAR(50) NOT NULL,
        updated_at  NVARCHAR(50) NOT NULL
    )
    """,
    # ── Governance: Compliance Assessments ──
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'compliance_assessments')
    CREATE TABLE compliance_assessments (
        id                  NVARCHAR(100) PRIMARY KEY,
        approval_request_id NVARCHAR(100),
        assessed_at         NVARCHAR(50) NOT NULL,
        assessed_by         NVARCHAR(200) DEFAULT 'InfraForge',
        overall_result      NVARCHAR(50) NOT NULL DEFAULT 'pending',
        standards_checked_json NVARCHAR(MAX) DEFAULT '[]',
        findings_json       NVARCHAR(MAX) DEFAULT '[]',
        score               FLOAT DEFAULT 0.0,
        FOREIGN KEY (approval_request_id) REFERENCES approval_requests(id)
    )
    """,
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'projects')
    CREATE TABLE projects (
        id              NVARCHAR(100) PRIMARY KEY,
        name            NVARCHAR(200) NOT NULL,
        description     NVARCHAR(MAX) DEFAULT '',
        owner_email     NVARCHAR(200) NOT NULL,
        department      NVARCHAR(200) DEFAULT '',
        cost_center     NVARCHAR(100) DEFAULT '',
        status          NVARCHAR(50) DEFAULT 'draft',
        phase           NVARCHAR(50) DEFAULT 'requirements',
        created_at      NVARCHAR(50) NOT NULL,
        updated_at      NVARCHAR(50) NOT NULL,
        metadata_json   NVARCHAR(MAX) DEFAULT '{}'
    )
    """,
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_chat_session')
    CREATE INDEX idx_chat_session ON chat_messages(session_token)""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_usage_timestamp')
    CREATE INDEX idx_usage_timestamp ON usage_logs(timestamp)""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_usage_department')
    CREATE INDEX idx_usage_department ON usage_logs(department)""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_approval_status')
    CREATE INDEX idx_approval_status ON approval_requests(status)""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_projects_owner')
    CREATE INDEX idx_projects_owner ON projects(owner_email)""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_services_category')
    CREATE INDEX idx_services_category ON services(category)""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_services_status')
    CREATE INDEX idx_services_status ON services(status)""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_security_standards_category')
    CREATE INDEX idx_security_standards_category ON security_standards(category)""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_governance_policies_category')
    CREATE INDEX idx_governance_policies_category ON governance_policies(category)""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_service_policies_service')
    CREATE INDEX idx_service_policies_service ON service_policies(service_id)""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_compliance_assessments_request')
    CREATE INDEX idx_compliance_assessments_request ON compliance_assessments(approval_request_id)""",
    # ── Deployments ──
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'deployments')
    CREATE TABLE deployments (
        deployment_id       NVARCHAR(100) PRIMARY KEY,
        deployment_name     NVARCHAR(200) NOT NULL,
        resource_group      NVARCHAR(200) NOT NULL,
        region              NVARCHAR(100) NOT NULL,
        status              NVARCHAR(50) NOT NULL DEFAULT 'pending',
        phase               NVARCHAR(50) DEFAULT 'init',
        progress            FLOAT DEFAULT 0.0,
        detail              NVARCHAR(MAX) DEFAULT '',
        template_hash       NVARCHAR(100) DEFAULT '',
        initiated_by        NVARCHAR(200) DEFAULT 'agent',
        started_at          NVARCHAR(50) NOT NULL,
        completed_at        NVARCHAR(50),
        error               NVARCHAR(MAX),
        resources_json      NVARCHAR(MAX) DEFAULT '[]',
        what_if_json        NVARCHAR(MAX),
        outputs_json        NVARCHAR(MAX) DEFAULT '{}'
    )
    """,
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_deployments_status')
    CREATE INDEX idx_deployments_status ON deployments(status)""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_deployments_rg')
    CREATE INDEX idx_deployments_rg ON deployments(resource_group)""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_deployments_initiated_by')
    CREATE INDEX idx_deployments_initiated_by ON deployments(initiated_by)""",
    # ── Service Artifacts (3-gate approval) ──
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'service_artifacts')
    CREATE TABLE service_artifacts (
        id              NVARCHAR(300) PRIMARY KEY,
        service_id      NVARCHAR(200) NOT NULL,
        artifact_type   NVARCHAR(50) NOT NULL,
        status          NVARCHAR(50) DEFAULT 'not_started',
        content         NVARCHAR(MAX),
        notes           NVARCHAR(MAX) DEFAULT '',
        approved_by     NVARCHAR(200),
        approved_at     NVARCHAR(50),
        created_at      NVARCHAR(50) NOT NULL,
        updated_at      NVARCHAR(50) NOT NULL
    )
    """,
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_artifacts_service')
    CREATE INDEX idx_artifacts_service ON service_artifacts(service_id)""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_artifacts_type')
    CREATE INDEX idx_artifacts_type ON service_artifacts(artifact_type)""",
    # ── Service Versions (versioned ARM templates) ──
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'service_versions')
    CREATE TABLE service_versions (
        id                      INT IDENTITY(1,1) PRIMARY KEY,
        service_id              NVARCHAR(200) NOT NULL,
        version                 INT NOT NULL DEFAULT 1,
        arm_template            NVARCHAR(MAX) NOT NULL,
        status                  NVARCHAR(50) DEFAULT 'draft',
        validation_result_json  NVARCHAR(MAX) DEFAULT '{}',
        policy_check_json       NVARCHAR(MAX) DEFAULT '{}',
        changelog               NVARCHAR(MAX) DEFAULT '',
        created_by              NVARCHAR(200) DEFAULT 'auto-generated',
        created_at              NVARCHAR(50) NOT NULL,
        validated_at            NVARCHAR(50),
        UNIQUE (service_id, version)
    )
    """,
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_svc_versions_service')
    CREATE INDEX idx_svc_versions_service ON service_versions(service_id)""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_svc_versions_status')
    CREATE INDEX idx_svc_versions_status ON service_versions(status)""",
    # Add active_version column to services if it doesn't exist
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('services') AND name = 'active_version'
    )
    ALTER TABLE services ADD active_version INT DEFAULT NULL
    """,
    # ── Template Catalog ──
    """
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'catalog_templates')
    CREATE TABLE catalog_templates (
        id              NVARCHAR(200) PRIMARY KEY,
        name            NVARCHAR(200) NOT NULL,
        description     NVARCHAR(MAX) DEFAULT '',
        format          NVARCHAR(50) NOT NULL DEFAULT 'bicep',
        category        NVARCHAR(100) NOT NULL DEFAULT 'compute',
        source_path     NVARCHAR(500) DEFAULT '',
        content         NVARCHAR(MAX) DEFAULT '',
        tags_json       NVARCHAR(MAX) DEFAULT '[]',
        resources_json  NVARCHAR(MAX) DEFAULT '[]',
        parameters_json NVARCHAR(MAX) DEFAULT '[]',
        outputs_json    NVARCHAR(MAX) DEFAULT '[]',
        service_ids_json NVARCHAR(MAX) DEFAULT '[]',
        is_blueprint    BIT DEFAULT 0,
        registered_by   NVARCHAR(200) DEFAULT 'platform-team',
        status          NVARCHAR(50) DEFAULT 'approved',
        created_at      NVARCHAR(50) NOT NULL,
        updated_at      NVARCHAR(50) NOT NULL
    )
    """,
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_templates_category')
    CREATE INDEX idx_templates_category ON catalog_templates(category)""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_templates_format')
    CREATE INDEX idx_templates_format ON catalog_templates(format)""",
    """IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_templates_status')
    CREATE INDEX idx_templates_status ON catalog_templates(status)""",
]


# ══════════════════════════════════════════════════════════════
# BACKEND FACTORY
# ══════════════════════════════════════════════════════════════

_backend: Optional[DatabaseBackend] = None


async def get_backend() -> DatabaseBackend:
    """Get or create the Azure SQL Database backend singleton.

    Requires AZURE_SQL_CONNECTION_STRING to be set in the environment.
    """
    global _backend
    if _backend is not None:
        return _backend

    connection_string = os.getenv("AZURE_SQL_CONNECTION_STRING", "")
    if not connection_string:
        raise RuntimeError(
            "AZURE_SQL_CONNECTION_STRING environment variable is required. "
            "Set it to your Azure SQL Database connection string."
        )

    _backend = AzureSQLBackend(connection_string)
    logger.info("Using Azure SQL Database backend")
    return _backend


async def init_db() -> None:
    """Initialize the database and seed governance data on first run."""
    backend = await get_backend()
    await backend.init()
    # Seed governance tables on first run (no-op if already populated)
    await seed_governance_data()


# ══════════════════════════════════════════════════════════════
# USER SESSIONS
# ══════════════════════════════════════════════════════════════

async def save_session(
    session_token: str,
    user_data: dict,
    access_token: str = "",
    claims: dict | None = None,
    ttl_hours: float = 8.0,
) -> None:
    """Persist a user session."""
    now = time.time()
    backend = await get_backend()

    # DELETE + INSERT for upsert behavior
    await backend.execute_write(
        "DELETE FROM user_sessions WHERE session_token = ?",
        (session_token,),
    )
    await backend.execute_write(
        """INSERT INTO user_sessions
           (session_token, user_id, display_name, email, job_title,
            department, cost_center, manager, groups_json, roles_json,
            team, is_platform_team, is_admin, access_token, claims_json,
            created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_token,
            user_data.get("user_id", ""),
            user_data.get("display_name", ""),
            user_data.get("email", ""),
            user_data.get("job_title", ""),
            user_data.get("department", ""),
            user_data.get("cost_center", ""),
            user_data.get("manager", ""),
            json.dumps(user_data.get("groups", [])),
            json.dumps(user_data.get("roles", [])),
            user_data.get("team", ""),
            int(user_data.get("is_platform_team", False)),
            int(user_data.get("is_admin", False)),
            access_token,
            json.dumps(claims or {}),
            now,
            now + (ttl_hours * 3600),
        ),
    )


async def get_session(session_token: str) -> Optional[dict]:
    """Retrieve a session if it exists and hasn't expired."""
    backend = await get_backend()
    rows = await backend.execute(
        "SELECT * FROM user_sessions WHERE session_token = ? AND expires_at > ?",
        (session_token, time.time()),
    )
    if not rows:
        return None

    row = rows[0]
    return {
        "session_token": row["session_token"],
        "user_id": row["user_id"],
        "display_name": row["display_name"],
        "email": row["email"],
        "job_title": row["job_title"],
        "department": row["department"],
        "cost_center": row["cost_center"],
        "manager": row["manager"],
        "groups": json.loads(row["groups_json"]),
        "roles": json.loads(row["roles_json"]),
        "team": row["team"],
        "is_platform_team": bool(row["is_platform_team"]),
        "is_admin": bool(row["is_admin"]),
        "access_token": row["access_token"],
        "claims": json.loads(row["claims_json"]),
        "created_at": row["created_at"],
    }


async def delete_session(session_token: str) -> None:
    """Remove a session (logout)."""
    backend = await get_backend()
    await backend.execute_write(
        "DELETE FROM user_sessions WHERE session_token = ?",
        (session_token,),
    )


async def cleanup_expired_sessions() -> int:
    """Remove expired sessions. Returns count removed."""
    backend = await get_backend()
    return await backend.execute_write(
        "DELETE FROM user_sessions WHERE expires_at <= ?",
        (time.time(),),
    )


# ══════════════════════════════════════════════════════════════
# CHAT MESSAGES
# ══════════════════════════════════════════════════════════════

async def save_chat_message(
    session_token: str, role: str, content: str
) -> None:
    """Save a chat message to the conversation history."""
    backend = await get_backend()
    await backend.execute_write(
        """INSERT INTO chat_messages (session_token, role, content, created_at)
           VALUES (?, ?, ?, ?)""",
        (session_token, role, content, time.time()),
    )


async def get_chat_history(
    session_token: str, limit: int = 100
) -> list[dict]:
    """Retrieve chat history for a session."""
    backend = await get_backend()
    rows = await backend.execute(
        """SELECT role, content, created_at FROM chat_messages
           WHERE session_token = ?
           ORDER BY created_at ASC""",
        (session_token,),
    )
    return rows[:limit]


async def get_user_chat_history(email: str, limit: int = 50) -> list[dict]:
    """Retrieve chat history across all sessions for a user."""
    backend = await get_backend()
    rows = await backend.execute(
        """SELECT cm.role, cm.content, cm.created_at
           FROM chat_messages cm
           JOIN user_sessions us ON cm.session_token = us.session_token
           WHERE us.email = ?
           ORDER BY cm.created_at DESC""",
        (email,),
    )
    return rows[:limit]


# ══════════════════════════════════════════════════════════════
# USAGE LOGS (Work IQ Analytics)
# ══════════════════════════════════════════════════════════════

async def log_usage(record: dict) -> None:
    """Log a usage record for Work IQ analytics.

    When backed by Azure SQL, this data can be surfaced in:
    - Power BI dashboards for org-wide spend visibility
    - M365 Copilot for conversational analytics
    """
    backend = await get_backend()
    await backend.execute_write(
        """INSERT INTO usage_logs
           (timestamp, user_email, department, cost_center, prompt,
            resource_types_json, estimated_cost, from_catalog)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            record.get("timestamp", time.time()),
            record.get("user", ""),
            record.get("department", ""),
            record.get("cost_center", ""),
            record.get("prompt", ""),
            json.dumps(record.get("resource_types", [])),
            record.get("estimated_cost", 0.0),
            int(record.get("from_catalog", False)),
        ),
    )


async def get_usage_stats(
    department: Optional[str] = None,
    since_timestamp: Optional[float] = None,
) -> dict:
    """Aggregate usage statistics for the Work IQ analytics dashboard."""
    backend = await get_backend()

    where_clauses: list[str] = []
    params: list = []

    if department:
        where_clauses.append("department = ?")
        params.append(department)
    if since_timestamp:
        where_clauses.append("timestamp >= ?")
        params.append(since_timestamp)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    # Total requests
    rows = await backend.execute(
        f"SELECT COUNT(*) as total FROM usage_logs {where_sql}", tuple(params)
    )
    total = rows[0]["total"] if rows else 0

    # Catalog reuse
    catalog_where = f"{where_sql} {'AND' if where_clauses else 'WHERE'} from_catalog = 1"
    rows = await backend.execute(
        f"SELECT COUNT(*) as hits FROM usage_logs {catalog_where}",
        tuple(params),
    )
    catalog_hits = rows[0]["hits"] if rows else 0

    # Total estimated cost
    rows = await backend.execute(
        f"SELECT COALESCE(SUM(estimated_cost), 0) as total_cost FROM usage_logs {where_sql}",
        tuple(params),
    )
    total_cost = rows[0]["total_cost"] if rows else 0

    # By department
    rows = await backend.execute(
        f"""SELECT department, COUNT(*) as count
            FROM usage_logs {where_sql}
            GROUP BY department ORDER BY count DESC""",
        tuple(params),
    )
    by_department = {row["department"]: row["count"] for row in rows}

    # By user
    rows = await backend.execute(
        f"""SELECT user_email, COUNT(*) as count
            FROM usage_logs {where_sql}
            GROUP BY user_email ORDER BY count DESC""",
        tuple(params),
    )
    by_user = {row["user_email"]: row["count"] for row in rows}

    return {
        "totalRequests": total,
        "catalogReuseRate": round(catalog_hits / max(total, 1) * 100, 1),
        "totalEstimatedMonthlyCost": round(total_cost, 2),
        "byDepartment": by_department,
        "byUser": by_user,
    }


# ══════════════════════════════════════════════════════════════
# APPROVAL REQUESTS
# ══════════════════════════════════════════════════════════════

async def save_approval_request(request: dict) -> str:
    """Save a service approval request. Returns the request ID."""
    request_id = request.get("id", f"SAR-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
    submitted_at = request.get("submitted_at", datetime.now(timezone.utc).isoformat())

    backend = await get_backend()
    await backend.execute_write(
        """INSERT INTO approval_requests
           (id, service_name, service_resource_type, current_status,
            risk_tier, business_justification, project_name, environment,
            requestor_name, requestor_email, status, submitted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            request_id,
            request.get("service_name", ""),
            request.get("service_resource_type", "unknown"),
            request.get("current_status", "not_in_catalog"),
            request.get("risk_tier", "medium"),
            request.get("business_justification", ""),
            request.get("project_name", ""),
            request.get("environment", "production"),
            request.get("requestor", {}).get("name", ""),
            request.get("requestor", {}).get("email", ""),
            request.get("status", "submitted"),
            submitted_at,
        ),
    )
    return request_id


async def get_approval_requests(
    status: Optional[str] = None,
    requestor_email: Optional[str] = None,
) -> list[dict]:
    """List approval requests with optional filtering."""
    backend = await get_backend()

    where_clauses: list[str] = []
    params: list = []

    if status:
        where_clauses.append("status = ?")
        params.append(status)
    if requestor_email:
        where_clauses.append("requestor_email = ?")
        params.append(requestor_email)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    return await backend.execute(
        f"SELECT * FROM approval_requests {where_sql} ORDER BY submitted_at DESC",
        tuple(params),
    )


async def update_approval_request(
    request_id: str,
    status: str,
    reviewer: str = "",
    review_notes: str = "",
) -> bool:
    """Update the status of an approval request (platform team action)."""
    backend = await get_backend()
    count = await backend.execute_write(
        """UPDATE approval_requests
           SET status = ?, reviewer = ?, review_notes = ?, reviewed_at = ?
           WHERE id = ?""",
        (
            status,
            reviewer,
            review_notes,
            datetime.now(timezone.utc).isoformat(),
            request_id,
        ),
    )
    return count > 0


# ══════════════════════════════════════════════════════════════
# PROJECTS
# ══════════════════════════════════════════════════════════════

async def create_project(project: dict) -> str:
    """Create a new infrastructure project."""
    now = datetime.now(timezone.utc).isoformat()
    project_id = project.get("id", f"PRJ-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")

    backend = await get_backend()
    await backend.execute_write(
        """INSERT INTO projects
           (id, name, description, owner_email, department, cost_center,
            status, phase, created_at, updated_at, metadata_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            project.get("name", ""),
            project.get("description", ""),
            project.get("owner_email", ""),
            project.get("department", ""),
            project.get("cost_center", ""),
            project.get("status", "draft"),
            project.get("phase", "requirements"),
            now,
            now,
            json.dumps(project.get("metadata", {})),
        ),
    )
    return project_id


async def get_project(project_id: str) -> Optional[dict]:
    """Retrieve a project by ID."""
    backend = await get_backend()
    rows = await backend.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    )
    if not rows:
        return None
    result = rows[0]
    result["metadata"] = json.loads(result.pop("metadata_json", "{}"))
    return result


async def list_projects(
    owner_email: Optional[str] = None,
    status: Optional[str] = None,
    department: Optional[str] = None,
) -> list[dict]:
    """List projects with optional filtering."""
    backend = await get_backend()

    where_clauses: list[str] = []
    params: list = []

    if owner_email:
        where_clauses.append("owner_email = ?")
        params.append(owner_email)
    if status:
        where_clauses.append("status = ?")
        params.append(status)
    if department:
        where_clauses.append("department = ?")
        params.append(department)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    rows = await backend.execute(
        f"SELECT * FROM projects {where_sql} ORDER BY updated_at DESC",
        tuple(params),
    )
    for row in rows:
        row["metadata"] = json.loads(row.pop("metadata_json", "{}"))
    return rows


async def update_project(project_id: str, updates: dict) -> bool:
    """Update a project's fields."""
    allowed_fields = {
        "name", "description", "status", "phase",
        "department", "cost_center",
    }
    set_clauses: list[str] = []
    params: list = []

    for field_name, value in updates.items():
        if field_name in allowed_fields:
            set_clauses.append(f"{field_name} = ?")
            params.append(value)

    if "metadata" in updates:
        set_clauses.append("metadata_json = ?")
        params.append(json.dumps(updates["metadata"]))

    if not set_clauses:
        return False

    set_clauses.append("updated_at = ?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(project_id)

    backend = await get_backend()
    count = await backend.execute_write(
        f"UPDATE projects SET {', '.join(set_clauses)} WHERE id = ?",
        tuple(params),
    )
    return count > 0


# ══════════════════════════════════════════════════════════════
# GOVERNANCE: SERVICES CATALOG
# ══════════════════════════════════════════════════════════════


async def bulk_insert_services(services: list[dict]) -> int:
    """Insert many new services in a single DB connection/transaction.

    This is used by the Azure sync to avoid thousands of individual round-trips.
    Only inserts — does NOT delete/update existing services.
    Each service dict should have: id, name, category, and optionally
    status, risk_tier, review_notes, contact, approved_regions.

    Returns the count of services inserted.
    """
    if not services:
        return 0

    import asyncio

    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()

    def _run():
        conn = backend._get_connection()
        try:
            cursor = conn.cursor()
            count = 0
            for svc in services:
                try:
                    cursor.execute(
                        """IF NOT EXISTS (SELECT 1 FROM services WHERE id = ?)
                           INSERT INTO services
                           (id, name, category, status, risk_tier, conditions_json,
                            review_notes, documentation, contact, rejection_reason,
                            approved_date, reviewed_by, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            svc["id"],
                            svc["id"],
                            svc.get("name", ""),
                            svc.get("category", "other"),
                            svc.get("status", "not_approved"),
                            svc.get("risk_tier", "medium"),
                            "[]",
                            svc.get("review_notes", ""),
                            "",
                            svc.get("contact", ""),
                            "",
                            "",
                            "",
                            now,
                            now,
                        ),
                    )
                    if cursor.rowcount > 0:
                        # Insert regions if provided
                        for region in svc.get("approved_regions", []):
                            cursor.execute(
                                "INSERT INTO service_approved_regions (service_id, region) VALUES (?, ?)",
                                (svc["id"], region),
                            )
                        count += 1
                except Exception:
                    # Skip duplicates silently
                    pass
            conn.commit()
            return count
        finally:
            conn.close()

    return await asyncio.get_event_loop().run_in_executor(None, _run)

async def upsert_service(svc: dict) -> None:
    """Insert or replace a service in the catalog."""
    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()
    await backend.execute_write(
        "DELETE FROM service_approved_skus WHERE service_id = ?", (svc["id"],))
    await backend.execute_write(
        "DELETE FROM service_approved_regions WHERE service_id = ?", (svc["id"],))
    await backend.execute_write(
        "DELETE FROM service_policies WHERE service_id = ?", (svc["id"],))
    await backend.execute_write(
        "DELETE FROM services WHERE id = ?", (svc["id"],))
    await backend.execute_write(
        """INSERT INTO services
           (id, name, category, status, risk_tier, conditions_json,
            review_notes, documentation, contact, rejection_reason,
            approved_date, reviewed_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            svc["id"],
            svc.get("name", ""),
            svc.get("category", "other"),
            svc.get("status", "not_approved"),
            svc.get("risk_tier", "medium"),
            json.dumps(svc.get("conditions", [])),
            svc.get("review_notes", ""),
            svc.get("documentation", ""),
            svc.get("contact", ""),
            svc.get("rejection_reason", ""),
            svc.get("approved_date", ""),
            svc.get("reviewed_by", ""),
            now,
            now,
        ),
    )
    for sku in svc.get("approved_skus", []):
        await backend.execute_write(
            "INSERT INTO service_approved_skus (service_id, sku) VALUES (?, ?)",
            (svc["id"], sku),
        )
    for region in svc.get("approved_regions", []):
        await backend.execute_write(
            "INSERT INTO service_approved_regions (service_id, region) VALUES (?, ?)",
            (svc["id"], region),
        )
    for policy_text in svc.get("policies", []):
        await backend.execute_write(
            "INSERT INTO service_policies (service_id, policy_text) VALUES (?, ?)",
            (svc["id"], policy_text),
        )


async def get_all_services(
    category: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """Get all services from the catalog, hydrated with SKUs, regions, policies.

    Uses batch queries (4 total) instead of per-service queries to avoid
    N+1 performance issues — critical when thousands of services exist.
    """
    backend = await get_backend()

    where_clauses: list[str] = []
    params: list = []
    if category:
        where_clauses.append("s.category = ?")
        params.append(category.lower())
    if status:
        where_clauses.append("s.status = ?")
        params.append(status.lower())

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    # 1. Fetch all services (single query)
    rows = await backend.execute(
        f"SELECT * FROM services s {where_sql} ORDER BY s.category, s.name",
        tuple(params),
    )

    if not rows:
        return []

    # 2. Batch-fetch ALL related data in 4 queries (not N+1)
    all_skus = await backend.execute(
        "SELECT service_id, sku FROM service_approved_skus", ())
    all_regions = await backend.execute(
        "SELECT service_id, region FROM service_approved_regions", ())
    all_policies = await backend.execute(
        "SELECT service_id, policy_text, security_standard_id "
        "FROM service_policies WHERE enabled = 1", ())
    all_artifacts = await backend.execute(
        "SELECT service_id, artifact_type, status FROM service_artifacts", ())

    # Group by service_id for O(1) lookup
    from collections import defaultdict
    skus_map: dict[str, list[str]] = defaultdict(list)
    for r in all_skus:
        skus_map[r["service_id"]].append(r["sku"])

    regions_map: dict[str, list[str]] = defaultdict(list)
    for r in all_regions:
        regions_map[r["service_id"]].append(r["region"])

    policies_map: dict[str, list[dict]] = defaultdict(list)
    for p in all_policies:
        policies_map[p["service_id"]].append(p)

    artifacts_map: dict[str, dict[str, str]] = defaultdict(dict)
    for a in all_artifacts:
        artifacts_map[a["service_id"]][a["artifact_type"]] = a["status"]

    # 3. Assemble hydrated results
    result = []
    for row in rows:
        svc = dict(row)
        svc_id = svc["id"]
        svc["approved_skus"] = skus_map.get(svc_id, [])
        svc["approved_regions"] = regions_map.get(svc_id, [])
        svc_policies = policies_map.get(svc_id, [])
        svc["policies"] = [p["policy_text"] for p in svc_policies]
        svc["policy_standard_links"] = [
            {"text": p["policy_text"], "standard_id": p["security_standard_id"]}
            for p in svc_policies if p.get("security_standard_id")
        ]
        svc["conditions"] = json.loads(svc.pop("conditions_json", "[]"))

        # Approval gate summary
        svc_arts = artifacts_map.get(svc_id, {})
        svc["gates"] = {
            "policy": svc_arts.get("policy", "not_started"),
            "template": svc_arts.get("template", "not_started"),
        }
        svc["gates_approved"] = sum(
            1 for s in svc["gates"].values() if s == "approved"
        )

        result.append(svc)

    return result


async def get_service(service_id: str) -> Optional[dict]:
    """Get a single service by ID, fully hydrated."""
    services = await get_all_services()
    for svc in services:
        if svc["id"] == service_id:
            return svc
    return None


# ══════════════════════════════════════════════════════════════
# TEMPLATE CATALOG CRUD
# ══════════════════════════════════════════════════════════════

async def upsert_template(tmpl: dict) -> None:
    """Insert or replace a catalog template."""
    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()
    await backend.execute_write(
        "DELETE FROM catalog_templates WHERE id = ?", (tmpl["id"],)
    )
    await backend.execute_write(
        """
        INSERT INTO catalog_templates
            (id, name, description, format, category, source_path, content,
             tags_json, resources_json, parameters_json, outputs_json,
             service_ids_json, is_blueprint, registered_by, status,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tmpl["id"],
            tmpl.get("name", ""),
            tmpl.get("description", ""),
            tmpl.get("format", "bicep"),
            tmpl.get("category", "compute"),
            tmpl.get("source_path", ""),
            tmpl.get("content", ""),
            json.dumps(tmpl.get("tags", [])),
            json.dumps(tmpl.get("resources", [])),
            json.dumps(tmpl.get("parameters", [])),
            json.dumps(tmpl.get("outputs", [])),
            json.dumps(tmpl.get("service_ids", tmpl.get("composedOf", []))),
            1 if tmpl.get("is_blueprint", tmpl.get("category") == "blueprint") else 0,
            tmpl.get("registered_by", "platform-team"),
            tmpl.get("status", "approved"),
            now,
            now,
        ),
    )


async def get_all_templates(
    category: Optional[str] = None,
    fmt: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
) -> list[dict]:
    """Get all catalog templates with optional filters."""
    backend = await get_backend()

    where_clauses: list[str] = []
    params: list = []
    if category:
        where_clauses.append("category = ?")
        params.append(category.lower())
    if fmt:
        where_clauses.append("format = ?")
        params.append(fmt.lower())
    if status:
        where_clauses.append("status = ?")
        params.append(status.lower())
    if search:
        where_clauses.append(
            "(LOWER(name) LIKE ? OR LOWER(description) LIKE ? OR tags_json LIKE ?)"
        )
        like = f"%{search.lower()}%"
        params.extend([like, like, like])

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    rows = await backend.execute(
        f"SELECT * FROM catalog_templates {where_sql} ORDER BY category, name",
        tuple(params),
    )

    result = []
    for row in rows:
        t = dict(row)
        t["tags"] = json.loads(t.pop("tags_json", "[]"))
        t["resources"] = json.loads(t.pop("resources_json", "[]"))
        t["parameters"] = json.loads(t.pop("parameters_json", "[]"))
        t["outputs"] = json.loads(t.pop("outputs_json", "[]"))
        t["service_ids"] = json.loads(t.pop("service_ids_json", "[]"))
        t["is_blueprint"] = bool(t.get("is_blueprint"))
        # Rename source_path back to 'source' for compatibility
        t["source"] = t.pop("source_path", "")
        result.append(t)
    return result


async def get_template_by_id(template_id: str) -> Optional[dict]:
    """Get a single template by ID."""
    backend = await get_backend()
    rows = await backend.execute(
        "SELECT * FROM catalog_templates WHERE id = ?", (template_id,)
    )
    if not rows:
        return None
    t = dict(rows[0])
    t["tags"] = json.loads(t.pop("tags_json", "[]"))
    t["resources"] = json.loads(t.pop("resources_json", "[]"))
    t["parameters"] = json.loads(t.pop("parameters_json", "[]"))
    t["outputs"] = json.loads(t.pop("outputs_json", "[]"))
    t["service_ids"] = json.loads(t.pop("service_ids_json", "[]"))
    t["is_blueprint"] = bool(t.get("is_blueprint"))
    t["source"] = t.pop("source_path", "")
    return t


async def delete_template(template_id: str) -> bool:
    """Delete a template by ID. Returns True if deleted."""
    backend = await get_backend()
    rows = await backend.execute(
        "SELECT id FROM catalog_templates WHERE id = ?", (template_id,)
    )
    if not rows:
        return False
    await backend.execute_write(
        "DELETE FROM catalog_templates WHERE id = ?", (template_id,)
    )
    return True


# ══════════════════════════════════════════════════════════════
# DEPLOYMENTS
# ══════════════════════════════════════════════════════════════

async def save_deployment(deployment: dict) -> None:
    """Insert or update a deployment record."""
    backend = await get_backend()
    # Upsert: delete then insert
    await backend.execute_write(
        "DELETE FROM deployments WHERE deployment_id = ?",
        (deployment["deployment_id"],),
    )
    await backend.execute_write(
        """INSERT INTO deployments
           (deployment_id, deployment_name, resource_group, region,
            status, phase, progress, detail, template_hash,
            initiated_by, started_at, completed_at, error,
            resources_json, what_if_json, outputs_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            deployment["deployment_id"],
            deployment["deployment_name"],
            deployment["resource_group"],
            deployment["region"],
            deployment.get("status", "pending"),
            deployment.get("phase", "init"),
            deployment.get("progress", 0.0),
            deployment.get("detail", ""),
            deployment.get("template_hash", ""),
            deployment.get("initiated_by", "agent"),
            deployment["started_at"],
            deployment.get("completed_at"),
            deployment.get("error"),
            json.dumps(deployment.get("provisioned_resources", [])),
            json.dumps(deployment.get("what_if_results")),
            json.dumps(deployment.get("outputs", {})),
        ),
    )


async def get_deployments(
    status: Optional[str] = None,
    resource_group: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """List deployment records from the database."""
    backend = await get_backend()
    where_clauses: list[str] = []
    params: list = []

    if status:
        where_clauses.append("status = ?")
        params.append(status)
    if resource_group:
        where_clauses.append("resource_group = ?")
        params.append(resource_group)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    rows = await backend.execute(
        f"SELECT * FROM deployments {where_sql} ORDER BY started_at DESC",
        tuple(params),
    )

    result = []
    for row in rows[:limit]:
        d = dict(row)
        d["provisioned_resources"] = json.loads(d.pop("resources_json", "[]"))
        d["what_if_results"] = json.loads(d.pop("what_if_json", "null"))
        d["outputs"] = json.loads(d.pop("outputs_json", "{}"))
        result.append(d)
    return result


async def get_deployment(deployment_id: str) -> Optional[dict]:
    """Get a single deployment by ID."""
    backend = await get_backend()
    rows = await backend.execute(
        "SELECT * FROM deployments WHERE deployment_id = ?",
        (deployment_id,),
    )
    if not rows:
        return None
    d = dict(rows[0])
    d["provisioned_resources"] = json.loads(d.pop("resources_json", "[]"))
    d["what_if_results"] = json.loads(d.pop("what_if_json", "null"))
    d["outputs"] = json.loads(d.pop("outputs_json", "{}"))
    return d


# ══════════════════════════════════════════════════════════════
# SERVICE APPROVAL ARTIFACTS (2-Gate Workflow)
# ══════════════════════════════════════════════════════════════

ARTIFACT_TYPES = ("policy", "template")


async def save_service_artifact(
    service_id: str,
    artifact_type: str,
    content: str = "",
    status: str = "draft",
    notes: str = "",
    approved_by: Optional[str] = None,
) -> dict:
    """Save or update a service approval artifact (policy or template).

    When status is set to 'approved', the approved_at timestamp is recorded.
    After every save, auto-promotion is checked — if all 2 gates are approved
    the service itself is promoted to 'approved'.
    """
    if artifact_type not in ARTIFACT_TYPES:
        raise ValueError(f"artifact_type must be one of {ARTIFACT_TYPES}")

    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()
    artifact_id = f"{service_id}:{artifact_type}"

    existing = await backend.execute(
        "SELECT id FROM service_artifacts WHERE id = ?", (artifact_id,)
    )

    if existing:
        await backend.execute_write(
            """UPDATE service_artifacts
               SET content = ?, status = ?, notes = ?, approved_by = ?,
                   approved_at = CASE WHEN ? = 'approved' THEN ? ELSE approved_at END,
                   updated_at = ?
               WHERE id = ?""",
            (content, status, notes, approved_by,
             status, now, now, artifact_id),
        )
    else:
        await backend.execute_write(
            """INSERT INTO service_artifacts
               (id, service_id, artifact_type, status, content, notes,
                approved_by, approved_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (artifact_id, service_id, artifact_type, status, content, notes,
             approved_by, now if status == "approved" else None, now, now),
        )

    # Check if all 2 gates are approved → auto-promote service
    await _check_and_promote_service(service_id)

    return await get_service_artifact(service_id, artifact_type)


async def get_service_artifact(
    service_id: str,
    artifact_type: str,
) -> Optional[dict]:
    """Get a specific artifact for a service."""
    backend = await get_backend()
    artifact_id = f"{service_id}:{artifact_type}"
    rows = await backend.execute(
        "SELECT * FROM service_artifacts WHERE id = ?", (artifact_id,)
    )
    return dict(rows[0]) if rows else None


async def get_service_artifacts(service_id: str) -> dict:
    """Get all artifact gates for a service.

    Returns a dict with keys: policy, template, _summary.
    Each artifact value is either the DB record or a 'not_started' placeholder.
    """
    backend = await get_backend()
    rows = await backend.execute(
        "SELECT * FROM service_artifacts WHERE service_id = ? ORDER BY artifact_type",
        (service_id,),
    )

    artifacts_by_type = {row["artifact_type"]: dict(row) for row in rows}

    result = {}
    for atype in ARTIFACT_TYPES:
        if atype in artifacts_by_type:
            result[atype] = artifacts_by_type[atype]
        else:
            result[atype] = {
                "id": f"{service_id}:{atype}",
                "service_id": service_id,
                "artifact_type": atype,
                "status": "not_started",
                "content": "",
                "notes": "",
                "approved_by": None,
                "approved_at": None,
            }

    approved_count = sum(1 for a in result.values() if a.get("status") == "approved")
    result["_summary"] = {
        "approved_count": approved_count,
        "total_gates": 2,
        "all_approved": approved_count == 2,
    }
    return result


async def approve_service_artifact(
    service_id: str,
    artifact_type: str,
    approved_by: str = "IT Staff",
) -> dict:
    """Mark an artifact as approved. Artifact must have content (draft status)."""
    artifact = await get_service_artifact(service_id, artifact_type)
    if not artifact or artifact["status"] == "not_started":
        raise ValueError("Cannot approve artifact without content. Save a draft first.")

    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()
    artifact_id = f"{service_id}:{artifact_type}"

    await backend.execute_write(
        """UPDATE service_artifacts
           SET status = 'approved', approved_by = ?, approved_at = ?, updated_at = ?
           WHERE id = ?""",
        (approved_by, now, now, artifact_id),
    )

    await _check_and_promote_service(service_id)
    return await get_service_artifact(service_id, artifact_type)


async def unapprove_service_artifact(
    service_id: str,
    artifact_type: str,
) -> dict:
    """Revert an artifact back to draft status (e.g. for edits)."""
    backend = await get_backend()
    artifact_id = f"{service_id}:{artifact_type}"
    now = datetime.now(timezone.utc).isoformat()

    await backend.execute_write(
        """UPDATE service_artifacts
           SET status = 'draft', approved_by = NULL, approved_at = NULL, updated_at = ?
           WHERE id = ?""",
        (now, artifact_id),
    )

    await _check_and_promote_service(service_id)
    return await get_service_artifact(service_id, artifact_type)


async def _check_and_promote_service(service_id: str) -> str:
    """Check gate status and update service lifecycle state.

    Lifecycle: not_approved → validating (2/2 gates) → approved (deploy test passes)

    When both gates are approved, sets the service to 'validating' — the caller
    is responsible for triggering the deployment test.  Only
    promote_service_after_validation() sets status to 'approved'.

    Returns the resulting status: 'not_approved', 'validating', or 'approved'.
    """
    backend = await get_backend()
    rows = await backend.execute(
        "SELECT COUNT(*) as cnt FROM service_artifacts "
        "WHERE service_id = ? AND status = 'approved'",
        (service_id,),
    )

    approved_count = rows[0]["cnt"] if rows else 0

    if approved_count >= 2:
        # Both gates approved — move to 'validating' (not directly to 'approved')
        now = datetime.now(timezone.utc).isoformat()
        await backend.execute_write(
            """UPDATE services
               SET status = 'validating', approved_date = NULL,
                   reviewed_by = NULL
               WHERE id = ? AND status NOT IN ('validating', 'approved')""",
            (service_id,),
        )
        logger.info(
            f"Service {service_id} moved to 'validating' (both gates passed, awaiting deployment test)"
        )
        return "validating"
    else:
        # Demote if a gate was reverted
        await backend.execute_write(
            """UPDATE services SET status = 'not_approved'
               WHERE id = ? AND status IN ('approved', 'validating')
               AND reviewed_by IN ('Deployment Validated', 'Two-Gate Approval', 'Three-Gate Approval', NULL, '')""",
            (service_id,),
        )
        return "not_approved"


async def promote_service_after_validation(
    service_id: str,
    validation_result: dict,
) -> bool:
    """Promote a service to 'approved' after successful deployment validation.

    Called only when the deployment test (What-If + validation) passes.
    """
    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()

    # Save validation result as a note on the service
    notes = json.dumps({
        "validation_passed": True,
        "validated_at": now,
        "what_if_summary": validation_result.get("change_counts", {}),
        "total_changes": validation_result.get("total_changes", 0),
    })

    await backend.execute_write(
        """UPDATE services
           SET status = 'approved', approved_date = ?,
               reviewed_by = 'Deployment Validated',
               review_notes = ?
           WHERE id = ?""",
        (now, notes, service_id),
    )
    logger.info(f"Service {service_id} promoted to 'approved' after deployment validation")
    return True


async def fail_service_validation(
    service_id: str,
    error: str,
) -> bool:
    """Mark a service as failed validation — back to not_approved with error."""
    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()

    notes = json.dumps({
        "validation_passed": False,
        "validated_at": now,
        "error": error,
    })

    await backend.execute_write(
        """UPDATE services
           SET status = 'validation_failed',
               review_notes = ?
           WHERE id = ?""",
        (notes, service_id),
    )
    logger.info(f"Service {service_id} failed deployment validation: {error}")
    return True


# ══════════════════════════════════════════════════════════════
# SERVICE VERSIONS (Versioned ARM Templates)
# ══════════════════════════════════════════════════════════════


async def create_service_version(
    service_id: str,
    arm_template: str,
    version: int | None = None,
    status: str = "draft",
    changelog: str = "",
    created_by: str = "auto-generated",
) -> dict:
    """Create a new version of a service's ARM template.

    If version is None, automatically increments from the latest version.
    Returns the created version record.
    """
    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()

    if version is None:
        rows = await backend.execute(
            "SELECT MAX(version) as max_ver FROM service_versions WHERE service_id = ?",
            (service_id,),
        )
        current_max = rows[0]["max_ver"] if rows and rows[0]["max_ver"] else 0
        version = current_max + 1

    await backend.execute_write(
        """INSERT INTO service_versions
           (service_id, version, arm_template, status, changelog,
            created_by, created_at, validation_result_json, policy_check_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, '{}', '{}')""",
        (service_id, version, arm_template, status, changelog, created_by, now),
    )

    logger.info(f"Created service version {service_id} v{version} ({status})")
    return await get_service_version(service_id, version)


async def get_service_version(service_id: str, version: int) -> dict | None:
    """Get a specific version of a service's ARM template."""
    backend = await get_backend()
    rows = await backend.execute(
        "SELECT * FROM service_versions WHERE service_id = ? AND version = ?",
        (service_id, version),
    )
    if not rows:
        return None
    row = dict(rows[0])
    row["validation_result"] = json.loads(row.pop("validation_result_json", "{}"))
    row["policy_check"] = json.loads(row.pop("policy_check_json", "{}"))
    return row


async def get_service_versions(
    service_id: str,
    status: str | None = None,
) -> list[dict]:
    """Get all versions of a service, ordered by version descending."""
    backend = await get_backend()
    if status:
        rows = await backend.execute(
            "SELECT * FROM service_versions WHERE service_id = ? AND status = ? ORDER BY version DESC",
            (service_id, status),
        )
    else:
        rows = await backend.execute(
            "SELECT * FROM service_versions WHERE service_id = ? ORDER BY version DESC",
            (service_id,),
        )
    result = []
    for row in rows:
        d = dict(row)
        d["validation_result"] = json.loads(d.pop("validation_result_json", "{}"))
        d["policy_check"] = json.loads(d.pop("policy_check_json", "{}"))
        result.append(d)
    return result


async def get_latest_service_version(service_id: str) -> dict | None:
    """Get the latest version (by version number) for a service."""
    backend = await get_backend()
    rows = await backend.execute(
        "SELECT TOP 1 * FROM service_versions WHERE service_id = ? ORDER BY version DESC",
        (service_id,),
    )
    if not rows:
        return None
    row = dict(rows[0])
    row["validation_result"] = json.loads(row.pop("validation_result_json", "{}"))
    row["policy_check"] = json.loads(row.pop("policy_check_json", "{}"))
    return row


async def update_service_version_status(
    service_id: str,
    version: int,
    status: str,
    validation_result: dict | None = None,
    policy_check: dict | None = None,
) -> bool:
    """Update the status and validation results of a service version."""
    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()

    set_clauses = ["status = ?"]
    params: list = [status]

    if validation_result is not None:
        set_clauses.append("validation_result_json = ?")
        params.append(json.dumps(validation_result))

    if policy_check is not None:
        set_clauses.append("policy_check_json = ?")
        params.append(json.dumps(policy_check))

    if status in ("approved", "failed"):
        set_clauses.append("validated_at = ?")
        params.append(now)

    params.extend([service_id, version])

    count = await backend.execute_write(
        f"UPDATE service_versions SET {', '.join(set_clauses)} "
        f"WHERE service_id = ? AND version = ?",
        tuple(params),
    )
    return count > 0


async def update_service_version_template(
    service_id: str,
    version: int,
    arm_template: str,
    created_by: str = "copilot-healed",
) -> bool:
    """Update the ARM template content for a version (used by auto-healing)."""
    backend = await get_backend()
    count = await backend.execute_write(
        "UPDATE service_versions SET arm_template = ?, created_by = ? "
        "WHERE service_id = ? AND version = ?",
        (arm_template, created_by, service_id, version),
    )
    return count > 0


async def set_active_service_version(service_id: str, version: int) -> bool:
    """Set the active (deployed) version for a service.

    Also promotes the service to 'approved' status.
    """
    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()

    count = await backend.execute_write(
        """UPDATE services
           SET active_version = ?, status = 'approved',
               approved_date = ?, reviewed_by = 'Deployment Validated'
           WHERE id = ?""",
        (version, now, service_id),
    )
    logger.info(f"Service {service_id} active_version set to v{version}")
    return count > 0


async def get_active_service_version(service_id: str) -> dict | None:
    """Get the currently active version for a service."""
    backend = await get_backend()
    rows = await backend.execute(
        "SELECT active_version FROM services WHERE id = ?",
        (service_id,),
    )
    if not rows or not rows[0].get("active_version"):
        return None
    return await get_service_version(service_id, rows[0]["active_version"])


# ══════════════════════════════════════════════════════════════
# GOVERNANCE: SECURITY STANDARDS
# ══════════════════════════════════════════════════════════════

async def upsert_security_standard(std: dict) -> None:
    """Insert or replace a security standard."""
    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()
    await backend.execute_write(
        "DELETE FROM security_standards WHERE id = ?", (std["id"],))
    await backend.execute_write(
        """INSERT INTO security_standards
           (id, name, description, category, severity,
            validation_key, validation_value, remediation, enabled,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            std["id"],
            std["name"],
            std.get("description", ""),
            std["category"],
            std.get("severity", "high"),
            std["validation_key"],
            str(std.get("validation_value", "true")),
            std.get("remediation", ""),
            int(std.get("enabled", True)),
            now,
            now,
        ),
    )


async def get_security_standards(
    category: Optional[str] = None,
    enabled_only: bool = True,
) -> list[dict]:
    """Get security standards, optionally filtered."""
    backend = await get_backend()
    where_clauses: list[str] = []
    params: list = []
    if enabled_only:
        where_clauses.append("enabled = 1")
    if category:
        where_clauses.append("category = ?")
        params.append(category)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    return await backend.execute(
        f"SELECT * FROM security_standards {where_sql} ORDER BY category, id",
        tuple(params),
    )


# ══════════════════════════════════════════════════════════════
# GOVERNANCE: COMPLIANCE FRAMEWORKS & CONTROLS
# ══════════════════════════════════════════════════════════════

async def upsert_compliance_framework(fw: dict) -> None:
    """Insert or replace a compliance framework."""
    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()
    # Delete child controls first to satisfy FK constraint
    await backend.execute_write(
        "DELETE FROM compliance_controls WHERE framework_id = ?", (fw["id"],))
    await backend.execute_write(
        "DELETE FROM compliance_frameworks WHERE id = ?", (fw["id"],))
    await backend.execute_write(
        """INSERT INTO compliance_frameworks
           (id, name, description, version, enabled, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            fw["id"],
            fw["name"],
            fw.get("description", ""),
            fw.get("version", "1.0"),
            int(fw.get("enabled", True)),
            now,
        ),
    )


async def upsert_compliance_control(ctrl: dict) -> None:
    """Insert or replace a compliance control."""
    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()
    await backend.execute_write(
        "DELETE FROM compliance_controls WHERE id = ?", (ctrl["id"],))
    await backend.execute_write(
        """INSERT INTO compliance_controls
           (id, framework_id, control_id, name, description,
            category, security_standard_ids_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ctrl["id"],
            ctrl["framework_id"],
            ctrl["control_id"],
            ctrl["name"],
            ctrl.get("description", ""),
            ctrl.get("category", ""),
            json.dumps(ctrl.get("security_standard_ids", [])),
            now,
        ),
    )


async def get_compliance_frameworks(enabled_only: bool = True) -> list[dict]:
    """Get compliance frameworks with their control counts."""
    backend = await get_backend()
    where = "WHERE enabled = 1" if enabled_only else ""
    frameworks = await backend.execute(
        f"SELECT * FROM compliance_frameworks {where} ORDER BY name", ())
    for fw in frameworks:
        controls = await backend.execute(
            "SELECT * FROM compliance_controls WHERE framework_id = ? ORDER BY control_id",
            (fw["id"],),
        )
        for c in controls:
            c["security_standard_ids"] = json.loads(
                c.pop("security_standard_ids_json", "[]"))
        fw["controls"] = controls
    return frameworks


# ══════════════════════════════════════════════════════════════
# GOVERNANCE: ORGANIZATION-WIDE POLICIES
# ══════════════════════════════════════════════════════════════

async def upsert_governance_policy(pol: dict) -> None:
    """Insert or replace a governance policy."""
    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()
    await backend.execute_write(
        "DELETE FROM governance_policies WHERE id = ?", (pol["id"],))
    await backend.execute_write(
        """INSERT INTO governance_policies
           (id, name, description, category, rule_key,
            rule_value_json, severity, enforcement, enabled,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            pol["id"],
            pol["name"],
            pol.get("description", ""),
            pol["category"],
            pol["rule_key"],
            json.dumps(pol["rule_value"]),
            pol.get("severity", "high"),
            pol.get("enforcement", "block"),
            int(pol.get("enabled", True)),
            now,
            now,
        ),
    )


async def get_governance_policies(
    category: Optional[str] = None,
    enabled_only: bool = True,
) -> list[dict]:
    """Get governance policies, optionally filtered."""
    backend = await get_backend()
    where_clauses: list[str] = []
    params: list = []
    if enabled_only:
        where_clauses.append("enabled = 1")
    if category:
        where_clauses.append("category = ?")
        params.append(category)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    rows = await backend.execute(
        f"SELECT * FROM governance_policies {where_sql} ORDER BY category, id",
        tuple(params),
    )
    for r in rows:
        r["rule_value"] = json.loads(r.pop("rule_value_json", "null"))
    return rows


async def get_governance_policies_as_dict() -> dict:
    """Get active governance policies as a flat dict keyed by rule_key.

    Returns something like:
    {
        "require_tags": ["environment", "owner", "costCenter", "project"],
        "allowed_regions": ["eastus2", "westus2", "westeurope"],
        "require_https": True,
        ...
    }
    """
    policies = await get_governance_policies(enabled_only=True)
    result = {}
    for p in policies:
        result[p["rule_key"]] = p["rule_value"]
    return result


# ══════════════════════════════════════════════════════════════
# GOVERNANCE: COMPLIANCE ASSESSMENTS
# ══════════════════════════════════════════════════════════════

async def save_compliance_assessment(assessment: dict) -> str:
    """Save a compliance assessment result."""
    backend = await get_backend()
    assessment_id = assessment.get(
        "id", f"CA-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
    now = datetime.now(timezone.utc).isoformat()
    await backend.execute_write(
        """INSERT INTO compliance_assessments
           (id, approval_request_id, assessed_at, assessed_by,
            overall_result, standards_checked_json, findings_json, score)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            assessment_id,
            assessment.get("approval_request_id"),
            now,
            assessment.get("assessed_by", "InfraForge"),
            assessment.get("overall_result", "pending"),
            json.dumps(assessment.get("standards_checked", [])),
            json.dumps(assessment.get("findings", [])),
            assessment.get("score", 0.0),
        ),
    )
    # Link back to approval request if provided
    if assessment.get("approval_request_id"):
        await backend.execute_write(
            """UPDATE approval_requests
               SET compliance_assessment_id = ?, security_score = ?,
                   compliance_results_json = ?
               WHERE id = ?""",
            (
                assessment_id,
                assessment.get("score", 0.0),
                json.dumps(assessment.get("findings", [])),
                assessment["approval_request_id"],
            ),
        )
    return assessment_id


async def get_compliance_assessment(assessment_id: str) -> Optional[dict]:
    """Get a compliance assessment by ID."""
    backend = await get_backend()
    rows = await backend.execute(
        "SELECT * FROM compliance_assessments WHERE id = ?",
        (assessment_id,),
    )
    if not rows:
        return None
    result = rows[0]
    result["standards_checked"] = json.loads(
        result.pop("standards_checked_json", "[]"))
    result["findings"] = json.loads(result.pop("findings_json", "[]"))
    return result


# ══════════════════════════════════════════════════════════════
# SEED: POPULATE GOVERNANCE DATA ON FIRST RUN
# ══════════════════════════════════════════════════════════════

async def seed_governance_data() -> dict:
    """Populate governance tables with initial data if they are empty.

    Seeds:
    - Security standards (machine-readable security rules)
    - Compliance frameworks and controls (SOC2, CIS Azure)
    - Azure services catalog (the 20 services from the original YAML)
    - Organization-wide governance policies (tag, region, encryption rules)

    Returns a summary of what was seeded.
    """
    backend = await get_backend()
    summary = {}

    # ── Check if services already seeded ─────────────────────
    rows = await backend.execute("SELECT COUNT(*) as cnt FROM services", ())
    services_exist = rows and rows[0]["cnt"] > 0

    # ── Check if templates already seeded ────────────────────
    tmpl_rows = await backend.execute("SELECT COUNT(*) as cnt FROM catalog_templates", ())
    templates_exist = tmpl_rows and tmpl_rows[0]["cnt"] > 0

    if services_exist and templates_exist:
        logger.info("Governance data already seeded — skipping.")
        return {"status": "already_seeded"}

    logger.info("Seeding governance data into database...")
    now = datetime.now(timezone.utc).isoformat()

    # ── Seed services + governance (sections 1-4) if not already present ──
    if not services_exist:
        await _seed_governance_and_services(backend, summary, now)

    # ── Seed templates (section 5) if not already present ──
    if not templates_exist:
        await _seed_templates(summary)

    logger.info(f"Governance data seeded: {summary}")
    return summary


async def _seed_governance_and_services(backend, summary: dict, now: str) -> None:
    """Seed security standards, compliance frameworks, governance policies, and services."""
    # ══════════════════════════════════════════════════════════
    # 1. SECURITY STANDARDS
    # ══════════════════════════════════════════════════════════
    security_standards = [
        {
            "id": "SEC-001", "name": "HTTPS Required",
            "description": "All web-facing resources must enforce HTTPS-only access. HTTP must be disabled.",
            "category": "encryption", "severity": "critical",
            "validation_key": "require_https", "validation_value": "true",
            "remediation": "Set httpsOnly=true in resource configuration. Disable HTTP listeners.",
        },
        {
            "id": "SEC-002", "name": "TLS 1.2 Minimum",
            "description": "All resources must use TLS 1.2 or higher. Older TLS/SSL versions are prohibited.",
            "category": "encryption", "severity": "critical",
            "validation_key": "min_tls_version", "validation_value": "1.2",
            "remediation": "Set minTlsVersion to '1.2' in resource properties.",
        },
        {
            "id": "SEC-003", "name": "Managed Identity Required",
            "description": "Resources must use managed identities for authentication instead of stored credentials, keys, or passwords.",
            "category": "identity", "severity": "high",
            "validation_key": "require_managed_identity", "validation_value": "true",
            "remediation": "Enable system-assigned or user-assigned managed identity. Remove stored credentials.",
        },
        {
            "id": "SEC-004", "name": "No Public Access",
            "description": "Resources must not expose public endpoints unless explicitly approved. Use private endpoints or VNet integration.",
            "category": "network", "severity": "high",
            "validation_key": "deny_public_access", "validation_value": "true",
            "remediation": "Disable public network access. Configure private endpoints.",
        },
        {
            "id": "SEC-005", "name": "Encryption at Rest",
            "description": "All data stores must use encryption at rest with platform-managed or customer-managed keys.",
            "category": "encryption", "severity": "critical",
            "validation_key": "require_encryption_at_rest", "validation_value": "true",
            "remediation": "Enable Transparent Data Encryption (TDE) or Storage Service Encryption (SSE).",
        },
        {
            "id": "SEC-006", "name": "Diagnostic Logging",
            "description": "All resources must have diagnostic logging enabled and connected to Log Analytics.",
            "category": "monitoring", "severity": "high",
            "validation_key": "require_diagnostic_logging", "validation_value": "true",
            "remediation": "Enable diagnostic settings and connect to a Log Analytics workspace.",
        },
        {
            "id": "SEC-007", "name": "Soft Delete / Purge Protection",
            "description": "Key Vaults and storage accounts must have soft delete and purge protection enabled.",
            "category": "data_protection", "severity": "high",
            "validation_key": "require_soft_delete", "validation_value": "true",
            "remediation": "Enable soft delete and purge protection on Key Vault / Storage Account.",
        },
        {
            "id": "SEC-008", "name": "RBAC Authorization",
            "description": "Key Vaults must use RBAC authorization model instead of access policies.",
            "category": "identity", "severity": "high",
            "validation_key": "require_rbac_auth", "validation_value": "true",
            "remediation": "Set Key Vault access model to 'Azure role-based access control'.",
        },
        {
            "id": "SEC-009", "name": "Network Security Groups Required",
            "description": "All VNet subnets must have a Network Security Group (NSG) attached.",
            "category": "network", "severity": "high",
            "validation_key": "require_nsg_on_subnets", "validation_value": "true",
            "remediation": "Create and attach an NSG to every subnet in the VNet.",
        },
        {
            "id": "SEC-010", "name": "Remote Debugging Disabled",
            "description": "Remote debugging must be disabled on all production App Service resources.",
            "category": "compute", "severity": "medium",
            "validation_key": "deny_remote_debugging", "validation_value": "true",
            "remediation": "Disable remote debugging in App Service configuration.",
        },
        {
            "id": "SEC-011", "name": "Azure AD Authentication",
            "description": "Databases and services supporting Azure AD auth must use it instead of local SQL auth.",
            "category": "identity", "severity": "high",
            "validation_key": "require_aad_auth", "validation_value": "true",
            "remediation": "Enable Azure AD authentication. Disable or restrict local SQL authentication.",
        },
        {
            "id": "SEC-012", "name": "Azure Defender / Microsoft Defender",
            "description": "Microsoft Defender must be enabled for applicable resource types (SQL, Storage, VMs, Containers).",
            "category": "monitoring", "severity": "high",
            "validation_key": "require_defender", "validation_value": "true",
            "remediation": "Enable Microsoft Defender for the resource type in Defender for Cloud.",
        },
        {
            "id": "SEC-013", "name": "Blob Public Access Disabled",
            "description": "Storage accounts must have blob public access disabled at the account level.",
            "category": "data_protection", "severity": "critical",
            "validation_key": "deny_blob_public_access", "validation_value": "true",
            "remediation": "Set 'Allow Blob public access' to Disabled on the storage account.",
        },
        {
            "id": "SEC-014", "name": "Automated OS Patching",
            "description": "Virtual machines must have automated OS patching enabled.",
            "category": "compute", "severity": "medium",
            "validation_key": "require_auto_patching", "validation_value": "true",
            "remediation": "Enable Azure Update Manager automatic patching.",
        },
        {
            "id": "SEC-015", "name": "Private Endpoint Required (Production)",
            "description": "Production-tier resources must use private endpoints instead of public access.",
            "category": "network", "severity": "high",
            "validation_key": "require_private_endpoints", "validation_value": "true",
            "remediation": "Create a private endpoint for the resource in the appropriate VNet/subnet.",
        },
    ]

    for std in security_standards:
        await upsert_security_standard(std)
    summary["security_standards"] = len(security_standards)

    # ══════════════════════════════════════════════════════════
    # 2. COMPLIANCE FRAMEWORKS & CONTROLS
    # ══════════════════════════════════════════════════════════
    frameworks = [
        {
            "id": "CIS-AZURE-2.0",
            "name": "CIS Microsoft Azure Foundations Benchmark",
            "description": "Center for Internet Security benchmark for Azure — industry-standard security baseline.",
            "version": "2.0",
            "controls": [
                {"control_id": "2.1.1", "name": "Ensure TLS 1.2+ for Storage",
                 "category": "storage", "standard_ids": ["SEC-002"]},
                {"control_id": "2.1.2", "name": "Ensure HTTPS Transfer Required",
                 "category": "storage", "standard_ids": ["SEC-001"]},
                {"control_id": "3.1", "name": "Ensure Diagnostic Logging Enabled",
                 "category": "logging", "standard_ids": ["SEC-006"]},
                {"control_id": "4.1.1", "name": "Ensure Azure SQL AD Auth Enabled",
                 "category": "database", "standard_ids": ["SEC-011"]},
                {"control_id": "4.1.3", "name": "Ensure SQL TDE Enabled",
                 "category": "database", "standard_ids": ["SEC-005"]},
                {"control_id": "4.2.1", "name": "Ensure Defender for SQL Enabled",
                 "category": "database", "standard_ids": ["SEC-012"]},
                {"control_id": "5.1.1", "name": "Ensure NSG on All Subnets",
                 "category": "networking", "standard_ids": ["SEC-009"]},
                {"control_id": "7.1", "name": "Ensure VM Managed Disks Encrypted",
                 "category": "compute", "standard_ids": ["SEC-005"]},
                {"control_id": "8.1", "name": "Ensure Key Vault Soft Delete Enabled",
                 "category": "security", "standard_ids": ["SEC-007"]},
                {"control_id": "8.5", "name": "Ensure Key Vault RBAC Mode",
                 "category": "security", "standard_ids": ["SEC-008"]},
            ],
        },
        {
            "id": "SOC2-TYPE2",
            "name": "SOC 2 Type II",
            "description": "Service Organization Control 2 — Trust Services Criteria for security, availability, and confidentiality.",
            "version": "2024",
            "controls": [
                {"control_id": "CC6.1", "name": "Logical and Physical Access Controls",
                 "category": "access_control", "standard_ids": ["SEC-003", "SEC-008", "SEC-011"]},
                {"control_id": "CC6.3", "name": "Role-Based Access",
                 "category": "access_control", "standard_ids": ["SEC-008"]},
                {"control_id": "CC6.6", "name": "Secure Transmission",
                 "category": "encryption", "standard_ids": ["SEC-001", "SEC-002"]},
                {"control_id": "CC6.7", "name": "Data-at-Rest Encryption",
                 "category": "encryption", "standard_ids": ["SEC-005", "SEC-013"]},
                {"control_id": "CC7.1", "name": "Monitoring and Detection",
                 "category": "monitoring", "standard_ids": ["SEC-006", "SEC-012"]},
                {"control_id": "CC7.2", "name": "Incident Response",
                 "category": "monitoring", "standard_ids": ["SEC-006"]},
                {"control_id": "CC8.1", "name": "Change Management",
                 "category": "operations", "standard_ids": ["SEC-014"]},
            ],
        },
        {
            "id": "HIPAA",
            "name": "HIPAA Security Rule",
            "description": "Health Insurance Portability and Accountability Act — security standards for protecting ePHI.",
            "version": "2024",
            "controls": [
                {"control_id": "164.312(a)(1)", "name": "Access Control",
                 "category": "access_control", "standard_ids": ["SEC-003", "SEC-008", "SEC-011"]},
                {"control_id": "164.312(a)(2)(iv)", "name": "Encryption and Decryption",
                 "category": "encryption", "standard_ids": ["SEC-005"]},
                {"control_id": "164.312(b)", "name": "Audit Controls",
                 "category": "monitoring", "standard_ids": ["SEC-006"]},
                {"control_id": "164.312(c)(1)", "name": "Integrity",
                 "category": "data_protection", "standard_ids": ["SEC-007", "SEC-013"]},
                {"control_id": "164.312(e)(1)", "name": "Transmission Security",
                 "category": "encryption", "standard_ids": ["SEC-001", "SEC-002"]},
            ],
        },
    ]

    for fw in frameworks:
        await upsert_compliance_framework({
            "id": fw["id"],
            "name": fw["name"],
            "description": fw["description"],
            "version": fw["version"],
        })
        for ctrl in fw["controls"]:
            await upsert_compliance_control({
                "id": f"{fw['id']}-{ctrl['control_id']}",
                "framework_id": fw["id"],
                "control_id": ctrl["control_id"],
                "name": ctrl["name"],
                "category": ctrl.get("category", ""),
                "security_standard_ids": ctrl.get("standard_ids", []),
            })
    summary["compliance_frameworks"] = len(frameworks)
    summary["compliance_controls"] = sum(len(fw["controls"]) for fw in frameworks)

    # ══════════════════════════════════════════════════════════
    # 3. GOVERNANCE POLICIES (org-wide rules)
    # ══════════════════════════════════════════════════════════
    governance_policies_data = [
        {
            "id": "GOV-001", "name": "Required Resource Tags",
            "description": "All Azure resources must include these tags for cost attribution, ownership tracking, and operational management.",
            "category": "tagging", "rule_key": "require_tags",
            "rule_value": ["environment", "owner", "costCenter", "project"],
            "severity": "high", "enforcement": "block",
        },
        {
            "id": "GOV-002", "name": "Allowed Deployment Regions",
            "description": "Resources may only be deployed to approved Azure regions. Other regions are blocked.",
            "category": "geography", "rule_key": "allowed_regions",
            "rule_value": ["eastus2", "westus2", "westeurope"],
            "severity": "critical", "enforcement": "block",
        },
        {
            "id": "GOV-003", "name": "HTTPS Enforcement",
            "description": "All web-facing resources must enforce HTTPS. HTTP-only endpoints are blocked.",
            "category": "security", "rule_key": "require_https",
            "rule_value": True,
            "severity": "critical", "enforcement": "block",
        },
        {
            "id": "GOV-004", "name": "Managed Identity Enforcement",
            "description": "Resources must use managed identities for authentication instead of stored credentials.",
            "category": "security", "rule_key": "require_managed_identity",
            "rule_value": True,
            "severity": "high", "enforcement": "warn",
        },
        {
            "id": "GOV-005", "name": "Private Endpoints (Production)",
            "description": "Production resources must use private endpoints. Public endpoints are blocked in production.",
            "category": "network", "rule_key": "require_private_endpoints",
            "rule_value": True,
            "severity": "high", "enforcement": "block",
        },
        {
            "id": "GOV-006", "name": "Public IP Restriction",
            "description": "No public IP addresses unless explicitly approved via exception request.",
            "category": "network", "rule_key": "max_public_ips",
            "rule_value": 0,
            "severity": "high", "enforcement": "block",
        },
        {
            "id": "GOV-007", "name": "Naming Convention",
            "description": "All resources must follow the organizational naming convention.",
            "category": "operations", "rule_key": "naming_convention",
            "rule_value": "{resourceType}-{project}-{environment}-{instance}",
            "severity": "medium", "enforcement": "warn",
        },
        {
            "id": "GOV-008", "name": "Budget Alert Threshold",
            "description": "Infrastructure requests exceeding the monthly cost threshold require manager approval.",
            "category": "cost", "rule_key": "cost_approval_threshold",
            "rule_value": 5000,
            "severity": "medium", "enforcement": "warn",
        },
    ]

    for pol in governance_policies_data:
        await upsert_governance_policy(pol)
    summary["governance_policies"] = len(governance_policies_data)

    # ══════════════════════════════════════════════════════════
    # 4. SERVICES CATALOG — all start as not_approved
    #    Approval requires the 3-gate process (policy + template + pipeline)
    # ══════════════════════════════════════════════════════════
    services_data = [
        # ── Compute ──
        {"id": "Microsoft.Web/serverfarms", "name": "App Service Plan", "category": "compute",
         "status": "not_approved", "risk_tier": "low"},
        {"id": "Microsoft.Web/sites", "name": "App Service", "category": "compute",
         "status": "not_approved", "risk_tier": "low"},
        {"id": "Microsoft.ContainerInstance/containerGroups",
         "name": "Azure Container Instances", "category": "compute",
         "status": "not_approved", "risk_tier": "medium"},
        {"id": "Microsoft.App/containerApps", "name": "Azure Container Apps",
         "category": "compute", "status": "not_approved", "risk_tier": "medium"},
        {"id": "Microsoft.ContainerService/managedClusters",
         "name": "Azure Kubernetes Service (AKS)", "category": "compute",
         "status": "not_approved", "risk_tier": "high"},
        {"id": "Microsoft.Compute/virtualMachines", "name": "Virtual Machines",
         "category": "compute", "status": "not_approved", "risk_tier": "high"},

        # ── Databases ──
        {"id": "Microsoft.Sql/servers", "name": "Azure SQL Server",
         "category": "database", "status": "not_approved", "risk_tier": "medium"},
        {"id": "Microsoft.Sql/servers/databases", "name": "Azure SQL Database",
         "category": "database", "status": "not_approved", "risk_tier": "medium"},
        {"id": "Microsoft.DBforPostgreSQL/flexibleServers",
         "name": "Azure Database for PostgreSQL (Flexible Server)",
         "category": "database", "status": "not_approved", "risk_tier": "medium"},
        {"id": "Microsoft.DocumentDB/databaseAccounts",
         "name": "Azure Cosmos DB", "category": "database",
         "status": "not_approved", "risk_tier": "high"},
        {"id": "Microsoft.Cache/Redis", "name": "Azure Cache for Redis",
         "category": "database", "status": "not_approved", "risk_tier": "low"},

        # ── Security & Identity ──
        {"id": "Microsoft.KeyVault/vaults", "name": "Azure Key Vault",
         "category": "security", "status": "not_approved", "risk_tier": "critical"},
        {"id": "Microsoft.ManagedIdentity/userAssignedIdentities",
         "name": "User-Assigned Managed Identity", "category": "security",
         "status": "not_approved", "risk_tier": "low"},

        # ── Storage ──
        {"id": "Microsoft.Storage/storageAccounts",
         "name": "Azure Storage Account", "category": "storage",
         "status": "not_approved", "risk_tier": "medium"},

        # ── Monitoring ──
        {"id": "Microsoft.OperationalInsights/workspaces",
         "name": "Log Analytics Workspace", "category": "monitoring",
         "status": "not_approved", "risk_tier": "low"},
        {"id": "Microsoft.Insights/components",
         "name": "Application Insights", "category": "monitoring",
         "status": "not_approved", "risk_tier": "low"},

        # ── Networking ──
        {"id": "Microsoft.Network/virtualNetworks",
         "name": "Virtual Network", "category": "networking",
         "status": "not_approved", "risk_tier": "medium"},
        {"id": "Microsoft.Network/applicationGateways",
         "name": "Application Gateway", "category": "networking",
         "status": "not_approved", "risk_tier": "high"},

        # ── AI ──
        {"id": "Microsoft.MachineLearningServices/workspaces",
         "name": "Azure Machine Learning", "category": "ai",
         "status": "not_approved", "risk_tier": "high"},
        {"id": "Microsoft.CognitiveServices/accounts",
         "name": "Azure AI Services (Cognitive Services)", "category": "ai",
         "status": "not_approved", "risk_tier": "high"},

        # ── Other ──
        {"id": "Microsoft.Blockchain/blockchainMembers",
         "name": "Azure Blockchain Service", "category": "other",
         "status": "not_approved", "risk_tier": "high"},
    ]

    for svc in services_data:
        await upsert_service(svc)
    summary["services"] = len(services_data)


async def _seed_templates(summary: dict) -> None:
    """Skip template seeding — templates require approved services first.

    The 3-gate workflow means services must be individually approved
    (policy + template + pipeline) before catalog templates can be built
    from them. Templates will be created by IT Staff after services are approved.
    """
    summary["templates"] = 0
    logger.info("Template seeding skipped — no approved services yet")
