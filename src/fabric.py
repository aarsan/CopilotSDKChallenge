"""
Microsoft Fabric Integration for InfraForge.

Provides:
- OneLake data sync (SQL Server → OneLake DFS as CSV/Parquet)
- Fabric workspace & item management via REST API
- Analytics aggregation queries for the dashboard
- Fabric connection health monitoring

Architecture:
    Azure SQL (transactional) ──ETL──▶ OneLake (analytics/reporting)
    The sync pushes denormalized analytics tables to OneLake so Fabric
    Semantic Models / Power BI can query them without touching the OLTP DB.
"""

import asyncio
import csv
import io
import json
import logging
import struct
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

logger = logging.getLogger("infraforge.fabric")

# ── Constants ─────────────────────────────────────────────────

FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
ONELAKE_DFS_SCOPE = "https://storage.azure.com/.default"
FABRIC_API_SCOPE = "https://api.fabric.microsoft.com/.default"

# Tables synced to OneLake (SQL table → OneLake file name)
SYNC_TABLES = {
    "pipeline_runs": {
        "query": """
            SELECT pr.run_id, pr.service_id, pr.pipeline_type, pr.status,
                   pr.version_num, pr.semver, pr.started_at, pr.completed_at,
                   pr.duration_secs, pr.heal_count, pr.created_by,
                   s.name AS service_name, s.category AS service_category
            FROM pipeline_runs pr
            LEFT JOIN services s ON pr.service_id = s.id
            ORDER BY pr.started_at DESC
        """,
        "description": "Pipeline execution history with service metadata",
    },
    "governance_reviews": {
        "query": """
            SELECT gr.service_id, gr.version, gr.semver, gr.pipeline_type,
                   gr.run_id, gr.agent, gr.verdict, gr.confidence,
                   gr.risk_score, gr.architecture_score, gr.security_posture,
                   gr.cost_assessment, gr.gate_decision, gr.gate_reason,
                   gr.model_used, gr.reviewed_at, gr.created_by,
                   s.name AS service_name, s.category AS service_category
            FROM governance_reviews gr
            LEFT JOIN services s ON gr.service_id = s.id
            ORDER BY gr.reviewed_at DESC
        """,
        "description": "CISO/CTO governance review decisions",
    },
    "service_catalog": {
        "query": """
            SELECT s.id, s.name, s.category, s.status, s.risk_tier,
                   s.active_version, s.approved_date, s.reviewed_by,
                   s.created_at, s.updated_at,
                   (SELECT COUNT(*) FROM service_versions sv
                    WHERE sv.service_id = s.id) AS version_count,
                   (SELECT COUNT(*) FROM service_policies sp
                    WHERE sp.service_id = s.id) AS policy_count
            FROM services s
            ORDER BY s.name
        """,
        "description": "Service catalog with version and policy counts",
    },
    "template_catalog": {
        "query": """
            SELECT ct.id, ct.name, ct.category, ct.format, ct.status,
                   ct.is_blueprint, ct.template_type, ct.registered_by,
                   ct.active_version, ct.created_at, ct.updated_at,
                   (SELECT COUNT(*) FROM template_versions tv
                    WHERE tv.template_id = ct.id) AS version_count
            FROM catalog_templates ct
            ORDER BY ct.name
        """,
        "description": "Template catalog with version counts",
    },
    "deployments": {
        "query": """
            SELECT d.deployment_id, d.deployment_name, d.resource_group,
                   d.region, d.status, d.phase, d.progress,
                   d.template_id, d.template_name, d.template_semver,
                   d.initiated_by, d.started_at, d.completed_at,
                   d.subscription_id, d.torn_down_at
            FROM deployments d
            ORDER BY d.started_at DESC
        """,
        "description": "Infrastructure deployment records",
    },
    "compliance_assessments": {
        "query": """
            SELECT ca.id, ca.approval_request_id, ca.assessed_at,
                   ca.assessed_by, ca.overall_result, ca.score,
                   ar.service_name, ar.project_name, ar.environment,
                   ar.risk_tier, ar.status AS request_status
            FROM compliance_assessments ca
            LEFT JOIN approval_requests ar ON ca.approval_request_id = ar.id
            ORDER BY ca.assessed_at DESC
        """,
        "description": "Compliance assessment results with request context",
    },
}


# ── Token Manager ─────────────────────────────────────────────

class _TokenCache:
    """Manages Azure AD tokens for different resource scopes."""

    def __init__(self):
        self._credential = None
        self._tokens: dict[str, tuple] = {}  # scope → (token_str, expires_on)

    def _get_credential(self):
        if self._credential is None:
            from azure.identity import DefaultAzureCredential
            self._credential = DefaultAzureCredential(
                exclude_workload_identity_credential=True,
                exclude_managed_identity_credential=True,
                exclude_developer_cli_credential=True,
                exclude_powershell_credential=True,
                exclude_visual_studio_code_credential=True,
                exclude_interactive_browser_credential=True,
            )
        return self._credential

    def get_token(self, scope: str) -> str:
        """Get a cached token for the given scope, refreshing if needed."""
        cached = self._tokens.get(scope)
        if cached and cached[1] > time.time() + 300:
            return cached[0]

        cred = self._get_credential()
        token = cred.get_token(scope)
        self._tokens[scope] = (token.token, token.expires_on)
        return token.token


_token_cache = _TokenCache()


# ── Fabric API Client ────────────────────────────────────────

class FabricClient:
    """Client for Microsoft Fabric REST APIs and OneLake DFS."""

    def __init__(self, workspace_id: str, onelake_dfs_endpoint: str):
        self.workspace_id = workspace_id
        self.dfs_endpoint = onelake_dfs_endpoint.rstrip("/")
        self._http: Optional[httpx.AsyncClient] = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=60.0)
        return self._http

    def _fabric_headers(self) -> dict:
        token = _token_cache.get_token(FABRIC_API_SCOPE)
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _onelake_headers(self) -> dict:
        token = _token_cache.get_token(ONELAKE_DFS_SCOPE)
        return {"Authorization": f"Bearer {token}"}

    # ── Workspace / Item Management ──────────────────────────

    async def get_workspace(self) -> dict:
        """Get workspace details."""
        client = await self._client()
        url = f"{FABRIC_API_BASE}/workspaces/{self.workspace_id}"
        r = await client.get(url, headers=self._fabric_headers())
        r.raise_for_status()
        return r.json()

    async def list_items(self, item_type: str = None) -> list[dict]:
        """List items in the workspace, optionally filtered by type."""
        client = await self._client()
        url = f"{FABRIC_API_BASE}/workspaces/{self.workspace_id}/items"
        if item_type:
            url += f"?type={item_type}"
        r = await client.get(url, headers=self._fabric_headers())
        r.raise_for_status()
        return r.json().get("value", [])

    async def create_item(self, display_name: str, item_type: str,
                          definition: dict = None) -> dict:
        """Create a Fabric item in the workspace."""
        client = await self._client()
        url = f"{FABRIC_API_BASE}/workspaces/{self.workspace_id}/items"
        body = {"displayName": display_name, "type": item_type}
        if definition:
            body["definition"] = definition
        r = await client.post(url, headers=self._fabric_headers(), json=body)
        r.raise_for_status()
        return r.json()

    # ── OneLake DFS Operations ───────────────────────────────

    async def write_file(self, item_name: str, path: str,
                         content: bytes, content_type: str = "text/csv") -> bool:
        """Write a file to OneLake via the DFS endpoint.

        Args:
            item_name: The Lakehouse/Warehouse display name
            path: Path within the item (e.g., "Files/analytics/pipeline_runs.csv")
            content: File content as bytes
            content_type: MIME type
        """
        client = await self._client()
        # OneLake path: /{workspace_name}/{item_name}/{path}
        workspace = await self.get_workspace()
        ws_name = workspace.get("displayName", self.workspace_id)

        # Step 1: Create file (PUT with resource=file)
        file_url = f"{self.dfs_endpoint}/{ws_name}/{item_name}/{path}"
        headers = self._onelake_headers()
        headers["Content-Length"] = "0"

        r_create = await client.put(
            f"{file_url}?resource=file",
            headers=headers,
        )
        if r_create.status_code not in (201, 409):  # 409 = already exists
            logger.warning(f"OneLake create file failed: {r_create.status_code} {r_create.text}")

        # Step 2: Append data
        headers = self._onelake_headers()
        headers["Content-Type"] = content_type
        r_append = await client.patch(
            f"{file_url}?action=append&position=0",
            headers=headers,
            content=content,
        )
        if r_append.status_code not in (200, 202):
            logger.error(f"OneLake append failed: {r_append.status_code} {r_append.text}")
            return False

        # Step 3: Flush to commit
        headers = self._onelake_headers()
        r_flush = await client.patch(
            f"{file_url}?action=flush&position={len(content)}",
            headers=headers,
        )
        if r_flush.status_code not in (200, 202):
            logger.error(f"OneLake flush failed: {r_flush.status_code} {r_flush.text}")
            return False

        logger.info(f"Wrote {len(content):,} bytes to OneLake: {item_name}/{path}")
        return True

    async def list_files(self, item_name: str, path: str = "") -> list[dict]:
        """List files in an OneLake directory."""
        client = await self._client()
        workspace = await self.get_workspace()
        ws_name = workspace.get("displayName", self.workspace_id)

        dir_url = f"{self.dfs_endpoint}/{ws_name}/{item_name}/{path}"
        headers = self._onelake_headers()
        r = await client.get(
            f"{dir_url}?resource=filesystem&recursive=false",
            headers=headers,
        )
        if r.status_code != 200:
            return []
        return r.json().get("paths", [])

    async def health_check(self) -> dict:
        """Check Fabric connectivity and return status."""
        result = {
            "workspace": {"status": "unknown"},
            "onelake": {"status": "unknown"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            ws = await self.get_workspace()
            result["workspace"] = {
                "status": "connected",
                "id": ws.get("id"),
                "name": ws.get("displayName"),
                "capacity_id": ws.get("capacityId"),
                "region": ws.get("capacityRegion"),
            }
        except Exception as e:
            result["workspace"] = {"status": "error", "error": str(e)}

        try:
            workspace = await self.get_workspace()
            ws_name = workspace.get("displayName", self.workspace_id)
            client = await self._client()
            r = await client.get(
                f"{self.dfs_endpoint}/{ws_name}?resource=filesystem&recursive=false",
                headers=self._onelake_headers(),
            )
            result["onelake"] = {
                "status": "connected" if r.status_code == 200 else "error",
                "dfs_endpoint": self.dfs_endpoint,
                "status_code": r.status_code,
            }
        except Exception as e:
            result["onelake"] = {"status": "error", "error": str(e)}

        return result

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()


# ── Data Sync Engine ──────────────────────────────────────────

class FabricSyncEngine:
    """Syncs analytics data from SQL Server to OneLake.

    Reads denormalized views from SQL Server via the existing
    DatabaseBackend, converts to CSV, and writes to OneLake DFS.
    """

    def __init__(self, fabric_client: FabricClient, lakehouse_name: str):
        self.fabric = fabric_client
        self.lakehouse_name = lakehouse_name
        self._last_sync: Optional[str] = None
        self._sync_history: list[dict] = []

    async def sync_table(self, table_name: str) -> dict:
        """Sync a single analytics table to OneLake."""
        from src.database import get_backend

        table_def = SYNC_TABLES.get(table_name)
        if not table_def:
            return {"table": table_name, "status": "error",
                    "error": f"Unknown sync table: {table_name}"}

        started = datetime.now(timezone.utc)
        try:
            # Query SQL Server
            backend = await get_backend()
            rows = await backend.execute(table_def["query"])

            if not rows:
                return {"table": table_name, "status": "skipped",
                        "reason": "no data", "row_count": 0}

            # Convert to CSV
            csv_content = self._rows_to_csv(rows)

            # Write to OneLake
            path = f"Files/analytics/{table_name}.csv"
            success = await self.fabric.write_file(
                self.lakehouse_name, path,
                csv_content.encode("utf-8"),
                content_type="text/csv",
            )

            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            result = {
                "table": table_name,
                "status": "synced" if success else "error",
                "row_count": len(rows),
                "size_bytes": len(csv_content.encode("utf-8")),
                "path": f"{self.lakehouse_name}/{path}",
                "duration_secs": round(elapsed, 2),
                "synced_at": datetime.now(timezone.utc).isoformat(),
            }

            if not success:
                result["error"] = "OneLake write failed"

            return result

        except Exception as e:
            logger.error(f"Sync failed for {table_name}: {e}")
            return {"table": table_name, "status": "error", "error": str(e)}

    async def sync_all(self) -> dict:
        """Sync all analytics tables to OneLake."""
        started = datetime.now(timezone.utc)
        results = []

        for table_name in SYNC_TABLES:
            result = await self.sync_table(table_name)
            results.append(result)

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        total_rows = sum(r.get("row_count", 0) for r in results)
        total_bytes = sum(r.get("size_bytes", 0) for r in results)
        synced_count = sum(1 for r in results if r["status"] == "synced")
        error_count = sum(1 for r in results if r["status"] == "error")

        sync_result = {
            "status": "completed" if error_count == 0 else "partial",
            "tables_synced": synced_count,
            "tables_errored": error_count,
            "tables_skipped": len(results) - synced_count - error_count,
            "total_rows": total_rows,
            "total_bytes": total_bytes,
            "duration_secs": round(elapsed, 2),
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "details": results,
        }

        self._last_sync = sync_result["synced_at"]
        self._sync_history.append({
            "timestamp": sync_result["synced_at"],
            "status": sync_result["status"],
            "rows": total_rows,
            "duration": sync_result["duration_secs"],
        })
        # Keep last 50 entries
        self._sync_history = self._sync_history[-50:]

        logger.info(
            f"Fabric sync completed: {synced_count}/{len(results)} tables, "
            f"{total_rows:,} rows, {total_bytes:,} bytes in {elapsed:.1f}s"
        )
        return sync_result

    @property
    def last_sync(self) -> Optional[str]:
        return self._last_sync

    @property
    def sync_history(self) -> list[dict]:
        return list(self._sync_history)

    @staticmethod
    def _rows_to_csv(rows: list[dict]) -> str:
        """Convert a list of dicts to CSV string."""
        if not rows:
            return ""
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue()


# ── Analytics Aggregation (queries SQL Server directly) ───────

class AnalyticsEngine:
    """Computes analytics aggregates from SQL Server for the dashboard.

    These queries power the in-app analytics page. Data is also synced
    to OneLake for Fabric/Power BI consumption.
    """

    @staticmethod
    async def get_pipeline_analytics() -> dict:
        """Pipeline execution trends and success rates."""
        from src.database import get_backend
        backend = await get_backend()

        # Overall stats
        stats = await backend.execute("""
            SELECT
                COUNT(*) AS total_runs,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS succeeded,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running,
                AVG(duration_secs) AS avg_duration_secs,
                SUM(heal_count) AS total_heals
            FROM pipeline_runs
        """)

        # By pipeline type
        by_type = await backend.execute("""
            SELECT pipeline_type,
                   COUNT(*) AS runs,
                   SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS succeeded,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                   AVG(duration_secs) AS avg_duration
            FROM pipeline_runs
            GROUP BY pipeline_type
            ORDER BY runs DESC
        """)

        # Recent trend (last 30 days, grouped by day)
        trend = await backend.execute("""
            SELECT
                LEFT(started_at, 10) AS run_date,
                COUNT(*) AS runs,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS succeeded,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM pipeline_runs
            WHERE started_at >= CONVERT(VARCHAR, DATEADD(DAY, -30, GETUTCDATE()), 126)
            GROUP BY LEFT(started_at, 10)
            ORDER BY run_date
        """)

        overall = stats[0] if stats else {}
        total = overall.get("total_runs", 0)
        succeeded = overall.get("succeeded", 0)

        return {
            "total_runs": total,
            "success_rate": round(succeeded / total * 100, 1) if total else 0,
            "succeeded": succeeded,
            "failed": overall.get("failed", 0),
            "running": overall.get("running", 0),
            "avg_duration_secs": round(overall.get("avg_duration_secs") or 0, 1),
            "total_heals": overall.get("total_heals", 0),
            "by_type": by_type,
            "trend": [
                {
                    "date": str(t.get("run_date", "")),
                    "runs": t.get("runs", 0),
                    "succeeded": t.get("succeeded", 0),
                    "failed": t.get("failed", 0),
                }
                for t in trend
            ],
        }

    @staticmethod
    async def get_governance_analytics() -> dict:
        """Governance review verdict distribution and trends."""
        from src.database import get_backend
        backend = await get_backend()

        # CISO verdicts
        ciso = await backend.execute("""
            SELECT verdict, COUNT(*) AS count,
                   AVG(CAST(risk_score AS FLOAT)) AS avg_risk_score,
                   AVG(confidence) AS avg_confidence
            FROM governance_reviews
            WHERE agent = 'ciso'
            GROUP BY verdict
        """)

        # CTO verdicts
        cto = await backend.execute("""
            SELECT verdict, COUNT(*) AS count,
                   AVG(CAST(architecture_score AS FLOAT)) AS avg_arch_score,
                   AVG(confidence) AS avg_confidence
            FROM governance_reviews
            WHERE agent = 'cto'
            GROUP BY verdict
        """)

        # Gate decisions
        gates = await backend.execute("""
            SELECT gate_decision, COUNT(*) AS count
            FROM governance_reviews
            WHERE gate_decision IS NOT NULL
            GROUP BY gate_decision
        """)

        # Security posture distribution
        postures = await backend.execute("""
            SELECT security_posture, COUNT(*) AS count
            FROM governance_reviews
            WHERE security_posture IS NOT NULL AND agent = 'ciso'
            GROUP BY security_posture
        """)

        # Review trend (last 30 days)
        trend = await backend.execute("""
            SELECT
                LEFT(reviewed_at, 10) AS review_date,
                agent,
                COUNT(*) AS reviews,
                SUM(CASE WHEN verdict = 'approved' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN verdict = 'blocked' THEN 1 ELSE 0 END) AS blocked
            FROM governance_reviews
            WHERE reviewed_at >= CONVERT(VARCHAR, DATEADD(DAY, -30, GETUTCDATE()), 126)
            GROUP BY LEFT(reviewed_at, 10), agent
            ORDER BY review_date
        """)

        return {
            "ciso_verdicts": ciso,
            "cto_verdicts": cto,
            "gate_decisions": gates,
            "security_postures": postures,
            "trend": [
                {
                    "date": str(t.get("review_date", "")),
                    "agent": t.get("agent"),
                    "reviews": t.get("reviews", 0),
                    "approved": t.get("approved", 0),
                    "blocked": t.get("blocked", 0),
                }
                for t in trend
            ],
        }

    @staticmethod
    async def get_service_analytics() -> dict:
        """Service catalog adoption and version metrics."""
        from src.database import get_backend
        backend = await get_backend()

        # Service status distribution
        by_status = await backend.execute("""
            SELECT status, COUNT(*) AS count
            FROM services
            GROUP BY status
            ORDER BY count DESC
        """)

        # By category
        by_category = await backend.execute("""
            SELECT category, COUNT(*) AS count,
                   SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved,
                   SUM(CASE WHEN active_version IS NOT NULL THEN 1 ELSE 0 END) AS with_templates
            FROM services
            GROUP BY category
            ORDER BY count DESC
        """)

        # Services with most versions
        top_versioned = await backend.execute("""
            SELECT TOP 10
                sv.service_id, s.name, COUNT(*) AS version_count,
                MAX(sv.semver) AS latest_semver
            FROM service_versions sv
            JOIN services s ON sv.service_id = s.id
            GROUP BY sv.service_id, s.name
            ORDER BY version_count DESC
        """)

        # Total counts
        totals = await backend.execute("""
            SELECT
                (SELECT COUNT(*) FROM services) AS total_services,
                (SELECT COUNT(*) FROM services WHERE status = 'approved') AS approved_services,
                (SELECT COUNT(*) FROM service_versions) AS total_versions,
                (SELECT COUNT(*) FROM catalog_templates) AS total_templates,
                (SELECT COUNT(*) FROM catalog_templates WHERE is_blueprint = 1) AS blueprints
        """)

        return {
            "totals": totals[0] if totals else {},
            "by_status": by_status,
            "by_category": by_category,
            "top_versioned": top_versioned,
        }

    @staticmethod
    async def get_deployment_analytics() -> dict:
        """Deployment success rates and regional distribution."""
        from src.database import get_backend
        backend = await get_backend()

        # Overall deployment stats
        stats = await backend.execute("""
            SELECT
                COUNT(*) AS total_deployments,
                SUM(CASE WHEN status = 'Succeeded' THEN 1 ELSE 0 END) AS succeeded,
                SUM(CASE WHEN status = 'Failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN torn_down_at IS NOT NULL THEN 1 ELSE 0 END) AS torn_down
            FROM deployments
        """)

        # By region
        by_region = await backend.execute("""
            SELECT region, COUNT(*) AS count,
                   SUM(CASE WHEN status = 'Succeeded' THEN 1 ELSE 0 END) AS succeeded
            FROM deployments
            GROUP BY region
            ORDER BY count DESC
        """)

        # By template
        by_template = await backend.execute("""
            SELECT TOP 10 template_name, COUNT(*) AS deployment_count,
                   SUM(CASE WHEN status = 'Succeeded' THEN 1 ELSE 0 END) AS succeeded
            FROM deployments
            WHERE template_name IS NOT NULL AND template_name != ''
            GROUP BY template_name
            ORDER BY deployment_count DESC
        """)

        return {
            "totals": stats[0] if stats else {},
            "by_region": by_region,
            "by_template": by_template,
        }

    @staticmethod
    async def get_compliance_analytics() -> dict:
        """Compliance assessment score trends."""
        from src.database import get_backend
        backend = await get_backend()

        stats = await backend.execute("""
            SELECT
                COUNT(*) AS total_assessments,
                AVG(score) AS avg_score,
                SUM(CASE WHEN overall_result = 'pass' THEN 1 ELSE 0 END) AS passed,
                SUM(CASE WHEN overall_result = 'fail' THEN 1 ELSE 0 END) AS failed
            FROM compliance_assessments
        """)

        # Score distribution
        distribution = await backend.execute("""
            SELECT
                CASE
                    WHEN score >= 90 THEN 'A (90-100)'
                    WHEN score >= 80 THEN 'B (80-89)'
                    WHEN score >= 70 THEN 'C (70-79)'
                    WHEN score >= 60 THEN 'D (60-69)'
                    ELSE 'F (<60)'
                END AS grade,
                COUNT(*) AS count
            FROM compliance_assessments
            WHERE score IS NOT NULL
            GROUP BY
                CASE
                    WHEN score >= 90 THEN 'A (90-100)'
                    WHEN score >= 80 THEN 'B (80-89)'
                    WHEN score >= 70 THEN 'C (70-79)'
                    WHEN score >= 60 THEN 'D (60-69)'
                    ELSE 'F (<60)'
                END
            ORDER BY grade
        """)

        return {
            "totals": stats[0] if stats else {},
            "score_distribution": distribution,
        }

    @staticmethod
    async def get_full_dashboard() -> dict:
        """Aggregate all analytics for the dashboard."""
        pipeline, governance, services, deployments, compliance = await asyncio.gather(
            AnalyticsEngine.get_pipeline_analytics(),
            AnalyticsEngine.get_governance_analytics(),
            AnalyticsEngine.get_service_analytics(),
            AnalyticsEngine.get_deployment_analytics(),
            AnalyticsEngine.get_compliance_analytics(),
            return_exceptions=True,
        )

        def _safe(result):
            if isinstance(result, Exception):
                logger.error(f"Analytics query failed: {result}")
                return {"error": str(result)}
            return result

        return {
            "pipeline": _safe(pipeline),
            "governance": _safe(governance),
            "services": _safe(services),
            "deployments": _safe(deployments),
            "compliance": _safe(compliance),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }


# ── Module-Level Singleton ────────────────────────────────────

_fabric_client: Optional[FabricClient] = None
_sync_engine: Optional[FabricSyncEngine] = None


def get_fabric_client() -> Optional[FabricClient]:
    """Get the Fabric client singleton (None if not configured)."""
    global _fabric_client
    if _fabric_client is not None:
        return _fabric_client

    import os
    workspace_id = os.getenv("FABRIC_WORKSPACE_ID", "")
    dfs_endpoint = os.getenv("FABRIC_ONELAKE_DFS_ENDPOINT", "")

    if not workspace_id or not dfs_endpoint:
        logger.info("Fabric not configured (missing FABRIC_WORKSPACE_ID or FABRIC_ONELAKE_DFS_ENDPOINT)")
        return None

    _fabric_client = FabricClient(workspace_id, dfs_endpoint)
    logger.info(f"Fabric client initialized: workspace={workspace_id}")
    return _fabric_client


def get_sync_engine() -> Optional[FabricSyncEngine]:
    """Get the sync engine singleton (None if Fabric not configured)."""
    global _sync_engine
    if _sync_engine is not None:
        return _sync_engine

    import os
    client = get_fabric_client()
    if not client:
        return None

    lakehouse_name = os.getenv("FABRIC_LAKEHOUSE_NAME", "infraforge_lakehouse")
    _sync_engine = FabricSyncEngine(client, lakehouse_name)
    logger.info(f"Fabric sync engine initialized: lakehouse={lakehouse_name}")
    return _sync_engine
