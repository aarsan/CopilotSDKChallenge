-- ══════════════════════════════════════════════════════════════
-- InfraForge — Complete Database Schema (Azure SQL / T-SQL)
-- ══════════════════════════════════════════════════════════════
--
-- This script creates all tables, indexes, and migration columns
-- used by InfraForge. Every statement is idempotent (IF NOT EXISTS).
--
-- Usage:
--   sqlcmd -S <server>.database.windows.net -d <database> -G -i scripts/create_tables.sql
--   Or paste into Azure Portal → Query Editor → Run.
--
-- Generated from: src/database.py AZURE_SQL_SCHEMA_STATEMENTS
--                 src/standards.py _STANDARDS_SCHEMA
-- ══════════════════════════════════════════════════════════════

-- ── Auth & Sessions ─────────────────────────────────────────

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
);
GO

-- ── Chat Messages ───────────────────────────────────────────

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'chat_messages')
CREATE TABLE chat_messages (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    session_token   NVARCHAR(200) NOT NULL,
    role            NVARCHAR(20) NOT NULL,
    content         NVARCHAR(MAX) NOT NULL,
    created_at      FLOAT NOT NULL,
    FOREIGN KEY (session_token) REFERENCES user_sessions(session_token) ON DELETE CASCADE
);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_chat_session')
CREATE INDEX idx_chat_session ON chat_messages(session_token);
GO

-- ── Usage Logs ────────────────────────────────────────────

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
);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_usage_timestamp')
CREATE INDEX idx_usage_timestamp ON usage_logs(timestamp);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_usage_department')
CREATE INDEX idx_usage_department ON usage_logs(department);
GO

-- ── Approval Requests ───────────────────────────────────────

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
);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_approval_status')
CREATE INDEX idx_approval_status ON approval_requests(status);
GO

-- ── Governance: Security Standards ──────────────────────────

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
    enabled         BIT DEFAULT 0,
    created_at      NVARCHAR(50) NOT NULL,
    updated_at      NVARCHAR(50) NOT NULL
);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_security_standards_category')
CREATE INDEX idx_security_standards_category ON security_standards(category);
GO

-- ── Governance: Compliance Frameworks ───────────────────────

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'compliance_frameworks')
CREATE TABLE compliance_frameworks (
    id          NVARCHAR(100) PRIMARY KEY,
    name        NVARCHAR(200) NOT NULL,
    description NVARCHAR(MAX) DEFAULT '',
    version     NVARCHAR(50) DEFAULT '1.0',
    enabled     BIT DEFAULT 0,
    created_at  NVARCHAR(50) NOT NULL
);
GO

-- ── Governance: Compliance Controls ─────────────────────────

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
);
GO

-- ── Governance: Azure Services Catalog ──────────────────────

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
);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_services_category')
CREATE INDEX idx_services_category ON services(category);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_services_status')
CREATE INDEX idx_services_status ON services(status);
GO

-- Migration: active_version column on services
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('services') AND name = 'active_version'
)
ALTER TABLE services ADD active_version INT DEFAULT NULL;
GO

-- Migration: Azure API version tracking columns on services
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('services') AND name = 'latest_api_version'
)
ALTER TABLE services ADD latest_api_version NVARCHAR(20) DEFAULT NULL;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('services') AND name = 'default_api_version'
)
ALTER TABLE services ADD default_api_version NVARCHAR(20) DEFAULT NULL;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('services') AND name = 'template_api_version'
)
ALTER TABLE services ADD template_api_version NVARCHAR(20) DEFAULT NULL;
GO

-- ── Governance: Per-service Policies ────────────────────────

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'service_policies')
CREATE TABLE service_policies (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    service_id      NVARCHAR(200) NOT NULL,
    policy_text     NVARCHAR(MAX) NOT NULL,
    security_standard_id NVARCHAR(100),
    enabled         BIT DEFAULT 1,
    FOREIGN KEY (service_id) REFERENCES services(id),
    FOREIGN KEY (security_standard_id) REFERENCES security_standards(id)
);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_service_policies_service')
CREATE INDEX idx_service_policies_service ON service_policies(service_id);
GO

-- ── Governance: Approved SKUs ───────────────────────────────

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'service_approved_skus')
CREATE TABLE service_approved_skus (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    service_id  NVARCHAR(200) NOT NULL,
    sku         NVARCHAR(100) NOT NULL,
    FOREIGN KEY (service_id) REFERENCES services(id)
);
GO

-- ── Governance: Approved Regions ────────────────────────────

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'service_approved_regions')
CREATE TABLE service_approved_regions (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    service_id  NVARCHAR(200) NOT NULL,
    region      NVARCHAR(100) NOT NULL,
    FOREIGN KEY (service_id) REFERENCES services(id)
);
GO

-- ── Governance: Organization-wide Policies ──────────────────

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
    enabled     BIT DEFAULT 0,
    created_at  NVARCHAR(50) NOT NULL,
    updated_at  NVARCHAR(50) NOT NULL
);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_governance_policies_category')
CREATE INDEX idx_governance_policies_category ON governance_policies(category);
GO

-- ── CAF alignment: add risk_id, policy_statement, purpose, scope, remediation, enforcement_tool ──
IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('governance_policies') AND name = 'risk_id')
ALTER TABLE governance_policies ADD risk_id NVARCHAR(50) DEFAULT '';
GO
IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('governance_policies') AND name = 'policy_statement')
ALTER TABLE governance_policies ADD policy_statement NVARCHAR(MAX) DEFAULT '';
GO
IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('governance_policies') AND name = 'purpose')
ALTER TABLE governance_policies ADD purpose NVARCHAR(MAX) DEFAULT '';
GO
IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('governance_policies') AND name = 'scope')
ALTER TABLE governance_policies ADD scope NVARCHAR(500) DEFAULT 'All cloud resources';
GO
IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('governance_policies') AND name = 'remediation')
ALTER TABLE governance_policies ADD remediation NVARCHAR(MAX) DEFAULT '';
GO
IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('governance_policies') AND name = 'enforcement_tool')
ALTER TABLE governance_policies ADD enforcement_tool NVARCHAR(200) DEFAULT '';
GO

-- ── Governance: Compliance Assessments ──────────────────────

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
);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_compliance_assessments_request')
CREATE INDEX idx_compliance_assessments_request ON compliance_assessments(approval_request_id);
GO

-- ── Projects ────────────────────────────────────────────────

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
);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_projects_owner')
CREATE INDEX idx_projects_owner ON projects(owner_email);
GO

-- ── Deployments ─────────────────────────────────────────────

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
);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_deployments_status')
CREATE INDEX idx_deployments_status ON deployments(status);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_deployments_rg')
CREATE INDEX idx_deployments_rg ON deployments(resource_group);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_deployments_initiated_by')
CREATE INDEX idx_deployments_initiated_by ON deployments(initiated_by);
GO

-- Migration: template tracking columns on deployments
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('deployments') AND name = 'template_id'
)
ALTER TABLE deployments ADD template_id NVARCHAR(200) DEFAULT '';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('deployments') AND name = 'template_name'
)
ALTER TABLE deployments ADD template_name NVARCHAR(200) DEFAULT '';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('deployments') AND name = 'subscription_id'
)
ALTER TABLE deployments ADD subscription_id NVARCHAR(100) DEFAULT '';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('deployments') AND name = 'torn_down_at'
)
ALTER TABLE deployments ADD torn_down_at NVARCHAR(50);
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('deployments') AND name = 'template_version'
)
ALTER TABLE deployments ADD template_version INT DEFAULT 0;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('deployments') AND name = 'template_semver'
)
ALTER TABLE deployments ADD template_semver NVARCHAR(20) DEFAULT '';
GO

-- ── Service Artifacts (Approval Gates) ──────────────────────

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
);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_artifacts_service')
CREATE INDEX idx_artifacts_service ON service_artifacts(service_id);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_artifacts_type')
CREATE INDEX idx_artifacts_type ON service_artifacts(artifact_type);
GO

-- ── Service Versions (Versioned ARM Templates) ──────────────

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
);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_svc_versions_service')
CREATE INDEX idx_svc_versions_service ON service_versions(service_id);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_svc_versions_status')
CREATE INDEX idx_svc_versions_status ON service_versions(status);
GO

-- Migration: deployment tracking columns on service_versions
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('service_versions') AND name = 'run_id'
)
ALTER TABLE service_versions ADD run_id NVARCHAR(50) DEFAULT NULL;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('service_versions') AND name = 'resource_group'
)
ALTER TABLE service_versions ADD resource_group NVARCHAR(200) DEFAULT NULL;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('service_versions') AND name = 'deployment_name'
)
ALTER TABLE service_versions ADD deployment_name NVARCHAR(200) DEFAULT NULL;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('service_versions') AND name = 'subscription_id'
)
ALTER TABLE service_versions ADD subscription_id NVARCHAR(100) DEFAULT NULL;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('service_versions') AND name = 'semver'
)
ALTER TABLE service_versions ADD semver NVARCHAR(20) DEFAULT NULL;
GO

-- ── Template Catalog ────────────────────────────────────────

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
);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_templates_category')
CREATE INDEX idx_templates_category ON catalog_templates(category);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_templates_format')
CREATE INDEX idx_templates_format ON catalog_templates(format);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_templates_status')
CREATE INDEX idx_templates_status ON catalog_templates(status);
GO

-- Migration: template dependency columns
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('catalog_templates') AND name = 'template_type'
)
ALTER TABLE catalog_templates ADD template_type NVARCHAR(30) DEFAULT 'workload';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('catalog_templates') AND name = 'provides_json'
)
ALTER TABLE catalog_templates ADD provides_json NVARCHAR(MAX) DEFAULT '[]';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('catalog_templates') AND name = 'requires_json'
)
ALTER TABLE catalog_templates ADD requires_json NVARCHAR(MAX) DEFAULT '[]';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('catalog_templates') AND name = 'optional_refs_json'
)
ALTER TABLE catalog_templates ADD optional_refs_json NVARCHAR(MAX) DEFAULT '[]';
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_templates_type')
CREATE INDEX idx_templates_type ON catalog_templates(template_type);
GO

-- Migration: active_version on catalog_templates
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('catalog_templates') AND name = 'active_version'
)
ALTER TABLE catalog_templates ADD active_version INT DEFAULT NULL;
GO

-- Migration: compliance_profile on catalog_templates
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('catalog_templates') AND name = 'compliance_profile_json'
)
ALTER TABLE catalog_templates ADD compliance_profile_json NVARCHAR(MAX) DEFAULT NULL;
GO

-- ── Template Versions ───────────────────────────────────────

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'template_versions')
CREATE TABLE template_versions (
    id                      INT IDENTITY(1,1) PRIMARY KEY,
    template_id             NVARCHAR(200) NOT NULL,
    version                 INT NOT NULL DEFAULT 1,
    arm_template            NVARCHAR(MAX) NOT NULL,
    status                  NVARCHAR(50) DEFAULT 'draft',
    test_results_json       NVARCHAR(MAX) DEFAULT '{}',
    changelog               NVARCHAR(MAX) DEFAULT '',
    semver                  NVARCHAR(20) DEFAULT NULL,
    created_by              NVARCHAR(200) DEFAULT 'template-composer',
    created_at              NVARCHAR(50) NOT NULL,
    tested_at               NVARCHAR(50),
    UNIQUE (template_id, version)
);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_tmpl_versions_template')
CREATE INDEX idx_tmpl_versions_template ON template_versions(template_id);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_tmpl_versions_status')
CREATE INDEX idx_tmpl_versions_status ON template_versions(status);
GO

-- Migration: validation columns on template_versions
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('template_versions') AND name = 'validation_results_json'
)
ALTER TABLE template_versions ADD validation_results_json NVARCHAR(MAX) DEFAULT NULL;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('template_versions') AND name = 'validated_at'
)
ALTER TABLE template_versions ADD validated_at NVARCHAR(50) DEFAULT NULL;
GO

-- ── Orchestration Processes ─────────────────────────────────

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'orchestration_processes')
CREATE TABLE orchestration_processes (
    id              NVARCHAR(100) PRIMARY KEY,
    name            NVARCHAR(200) NOT NULL,
    description     NVARCHAR(MAX) DEFAULT '',
    trigger_event   NVARCHAR(200) NOT NULL,
    enabled         BIT DEFAULT 1,
    created_at      NVARCHAR(50) NOT NULL,
    updated_at      NVARCHAR(50) NOT NULL
);
GO

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'process_steps')
CREATE TABLE process_steps (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    process_id      NVARCHAR(100) NOT NULL,
    step_order      INT NOT NULL,
    name            NVARCHAR(200) NOT NULL,
    description     NVARCHAR(MAX) NOT NULL,
    action          NVARCHAR(200) NOT NULL,
    condition_json  NVARCHAR(MAX) DEFAULT '{}',
    on_success      NVARCHAR(200) DEFAULT 'next',
    on_failure      NVARCHAR(200) DEFAULT 'abort',
    config_json     NVARCHAR(MAX) DEFAULT '{}',
    FOREIGN KEY (process_id) REFERENCES orchestration_processes(id)
);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_process_steps_process')
CREATE INDEX idx_process_steps_process ON process_steps(process_id, step_order);
GO

-- ── Organization Standards (from standards.py) ──────────────

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
);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_org_standards_category')
CREATE INDEX idx_org_standards_category ON org_standards(category);
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_org_standards_enabled')
CREATE INDEX idx_org_standards_enabled ON org_standards(enabled);
GO

-- Migration: frameworks column on org_standards
IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('org_standards') AND name = 'frameworks')
ALTER TABLE org_standards ADD frameworks NVARCHAR(MAX) DEFAULT '[]';
GO

-- ── CAF alignment: risk_id, purpose, enforcement_tool on org_standards ──
IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('org_standards') AND name = 'risk_id')
ALTER TABLE org_standards ADD risk_id NVARCHAR(50) DEFAULT '';
GO
IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('org_standards') AND name = 'purpose')
ALTER TABLE org_standards ADD purpose NVARCHAR(MAX) DEFAULT '';
GO
IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('org_standards') AND name = 'enforcement_tool')
ALTER TABLE org_standards ADD enforcement_tool NVARCHAR(200) DEFAULT '';
GO

-- ── Organization Standards History ──────────────────────────

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
);
GO

IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('org_standards_history') AND name = 'frameworks')
ALTER TABLE org_standards_history ADD frameworks NVARCHAR(MAX) DEFAULT '[]';
GO

-- ── CAF alignment: risk_id, purpose, enforcement_tool on org_standards_history ──
IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('org_standards_history') AND name = 'risk_id')
ALTER TABLE org_standards_history ADD risk_id NVARCHAR(50) DEFAULT '';
GO
IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('org_standards_history') AND name = 'purpose')
ALTER TABLE org_standards_history ADD purpose NVARCHAR(MAX) DEFAULT '';
GO
IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('org_standards_history') AND name = 'enforcement_tool')
ALTER TABLE org_standards_history ADD enforcement_tool NVARCHAR(200) DEFAULT '';
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_org_standards_hist_sid')
CREATE INDEX idx_org_standards_hist_sid ON org_standards_history(standard_id);
GO

-- ══════════════════════════════════════════════════════════════
-- Done. All 22 tables + migration columns + indexes created.
-- ══════════════════════════════════════════════════════════════

PRINT 'InfraForge schema creation complete.';
GO
