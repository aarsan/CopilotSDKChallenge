"""
InfraForge — Database Backup & Restore

Exports all Azure SQL tables to a single JSON file and restores from it.
Designed for disaster recovery, environment cloning, and data migration.

Usage (from API):
    POST /api/admin/backup          → download JSON backup
    POST /api/admin/restore         → upload JSON to restore
    GET  /api/admin/backups         → list available backups

Usage (CLI):
    python -m scripts.backup        → writes to backups/ directory
    python -m scripts.restore <file> → restores from backup file
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.database import get_backend

logger = logging.getLogger("infraforge.backup")

# ── Tables to back up, in dependency order (parents before children) ──

BACKUP_TABLES = [
    # Independent tables (no FK dependencies)
    "user_sessions",
    "usage_logs",
    "security_standards",
    "compliance_frameworks",
    "governance_policies",
    "services",
    "orchestration_processes",
    "org_standards",
    "projects",
    # Tables with FKs → parent tables above
    "chat_messages",
    "approval_requests",
    "compliance_controls",
    "service_policies",
    "service_approved_skus",
    "service_approved_regions",
    "compliance_assessments",
    "service_artifacts",
    "service_versions",
    "catalog_templates",
    "template_versions",
    "process_steps",
    "org_standards_history",
    # Deployments (independent but logically last)
    "deployments",
]

BACKUPS_DIR = Path(__file__).parent.parent / "backups"


async def create_backup(
    include_sessions: bool = False,
    note: str = "",
) -> dict:
    """Export all database tables to a JSON structure.

    Args:
        include_sessions: If False (default), skip user_sessions and chat_messages
                          (they're ephemeral and contain tokens).
        note: Optional human-readable note to include in the backup metadata.

    Returns:
        dict with:
          - metadata: timestamp, table counts, version
          - tables: { table_name: [ {row}, {row}, ... ] }
    """
    backend = await get_backend()
    now = datetime.now(timezone.utc).isoformat()

    skip_tables = set()
    if not include_sessions:
        skip_tables = {"user_sessions", "chat_messages"}

    tables_data: dict[str, list[dict]] = {}
    table_counts: dict[str, int] = {}

    for table in BACKUP_TABLES:
        if table in skip_tables:
            continue
        try:
            rows = await backend.execute(f"SELECT * FROM [{table}]", ())
            # Convert any non-serializable values to strings
            clean_rows = []
            for row in rows:
                clean = {}
                for k, v in row.items():
                    if isinstance(v, (bytes, bytearray)):
                        clean[k] = v.hex()
                    elif isinstance(v, float) and (v != v):  # NaN check
                        clean[k] = None
                    else:
                        clean[k] = v
                clean_rows.append(clean)
            tables_data[table] = clean_rows
            table_counts[table] = len(clean_rows)
            logger.info(f"Backed up {table}: {len(clean_rows)} rows")
        except Exception as e:
            logger.warning(f"Skipped table {table}: {e}")
            tables_data[table] = []
            table_counts[table] = 0

    total_rows = sum(table_counts.values())

    backup = {
        "metadata": {
            "app": "InfraForge",
            "version": "1.0.0",
            "created_at": now,
            "note": note,
            "include_sessions": include_sessions,
            "tables_backed_up": len(tables_data),
            "total_rows": total_rows,
            "table_counts": table_counts,
        },
        "tables": tables_data,
    }

    logger.info(
        f"Backup complete: {len(tables_data)} tables, {total_rows} rows"
    )
    return backup


async def save_backup_to_file(
    include_sessions: bool = False,
    note: str = "",
    directory: Optional[str] = None,
) -> str:
    """Create a backup and save it to a JSON file.

    Returns the file path of the saved backup.
    """
    backup = await create_backup(include_sessions=include_sessions, note=note)

    out_dir = Path(directory) if directory else BACKUPS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"infraforge_backup_{timestamp}.json"
    filepath = out_dir / filename

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(backup, f, indent=2, default=str, ensure_ascii=False)

    size_mb = filepath.stat().st_size / (1024 * 1024)
    logger.info(f"Backup saved: {filepath} ({size_mb:.1f} MB)")
    return str(filepath)


async def restore_from_backup(
    backup_data: dict,
    mode: str = "replace",
    skip_tables: Optional[list[str]] = None,
) -> dict:
    """Restore database tables from a backup JSON structure.

    Args:
        backup_data: The backup dict (from create_backup or loaded from file).
        mode: 'replace' (default) — DELETE existing rows then INSERT.
              'merge'   — INSERT only, skip rows that conflict on PK.
        skip_tables: Optional list of table names to skip during restore.

    Returns:
        dict with restore summary (tables restored, row counts, errors).
    """
    backend = await get_backend()
    tables = backup_data.get("tables", {})
    metadata = backup_data.get("metadata", {})
    skip = set(skip_tables or [])

    summary: dict = {
        "mode": mode,
        "backup_created_at": metadata.get("created_at", "unknown"),
        "tables_restored": [],
        "tables_skipped": [],
        "errors": [],
        "total_rows_restored": 0,
    }

    # Get column info for each table to build INSERT statements
    async def _get_columns(table_name: str) -> list[str]:
        """Get column names for a table, excluding IDENTITY columns."""
        rows = await backend.execute(
            """
            SELECT c.name
              FROM sys.columns c
              JOIN sys.tables t ON c.object_id = t.object_id
             WHERE t.name = ?
               AND c.is_identity = 0
             ORDER BY c.column_id
            """,
            (table_name,),
        )
        return [r["name"] for r in rows]

    # Restore in dependency order (BACKUP_TABLES is already ordered)
    # For 'replace' mode, delete in REVERSE order (children first)
    if mode == "replace":
        for table in reversed(BACKUP_TABLES):
            if table in skip or table not in tables:
                continue
            try:
                await backend.execute_write(f"DELETE FROM [{table}]", ())
                logger.info(f"Cleared {table}")
            except Exception as e:
                logger.warning(f"Could not clear {table}: {e}")
                summary["errors"].append(
                    {"table": table, "phase": "delete", "error": str(e)}
                )

    # Insert rows in dependency order (parents first)
    for table in BACKUP_TABLES:
        if table in skip or table not in tables:
            if table in skip:
                summary["tables_skipped"].append(table)
            continue

        rows = tables[table]
        if not rows:
            summary["tables_restored"].append({"table": table, "rows": 0})
            continue

        try:
            db_columns = await _get_columns(table)
            if not db_columns:
                summary["errors"].append(
                    {"table": table, "phase": "schema", "error": "No columns found (table may not exist)"}
                )
                continue

            # Use only columns that exist in BOTH the backup data and the DB
            sample_row = rows[0]
            backup_columns = set(sample_row.keys())
            valid_columns = [c for c in db_columns if c in backup_columns]

            if not valid_columns:
                summary["errors"].append(
                    {"table": table, "phase": "columns", "error": "No matching columns between backup and DB"}
                )
                continue

            col_list = ", ".join(f"[{c}]" for c in valid_columns)
            placeholders = ", ".join("?" for _ in valid_columns)
            insert_sql = f"INSERT INTO [{table}] ({col_list}) VALUES ({placeholders})"

            restored = 0
            for row in rows:
                values = tuple(row.get(c) for c in valid_columns)
                try:
                    await backend.execute_write(insert_sql, values)
                    restored += 1
                except Exception as row_err:
                    err_str = str(row_err)
                    if mode == "merge" and (
                        "duplicate" in err_str.lower()
                        or "unique" in err_str.lower()
                        or "primary" in err_str.lower()
                        or "violation" in err_str.lower()
                    ):
                        continue  # Skip duplicates in merge mode
                    # Log first few errors per table, then silently skip
                    if restored == 0:
                        summary["errors"].append(
                            {"table": table, "phase": "insert", "error": err_str[:200]}
                        )

            summary["tables_restored"].append({"table": table, "rows": restored})
            summary["total_rows_restored"] += restored
            logger.info(f"Restored {table}: {restored}/{len(rows)} rows")

        except Exception as e:
            summary["errors"].append(
                {"table": table, "phase": "insert", "error": str(e)[:200]}
            )
            logger.error(f"Failed to restore {table}: {e}")

    logger.info(
        f"Restore complete: {len(summary['tables_restored'])} tables, "
        f"{summary['total_rows_restored']} rows"
    )
    return summary


async def restore_from_file(
    filepath: str,
    mode: str = "replace",
    skip_tables: Optional[list[str]] = None,
) -> dict:
    """Load a backup file and restore it.

    Returns the restore summary dict.
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Backup file not found: {filepath}")

    with open(path, "r", encoding="utf-8") as f:
        backup_data = json.load(f)

    if "tables" not in backup_data:
        raise ValueError("Invalid backup file: missing 'tables' key")

    return await restore_from_backup(
        backup_data, mode=mode, skip_tables=skip_tables
    )


def list_backup_files(directory: Optional[str] = None) -> list[dict]:
    """List available backup files with metadata.

    Returns list of dicts with: filename, path, size_mb, created_at, metadata.
    """
    out_dir = Path(directory) if directory else BACKUPS_DIR
    if not out_dir.exists():
        return []

    results = []
    for f in sorted(out_dir.glob("infraforge_backup_*.json"), reverse=True):
        try:
            size_mb = f.stat().st_size / (1024 * 1024)
            # Quick-read just the metadata without loading the full file
            with open(f, "r", encoding="utf-8") as fp:
                # Read first ~2KB to extract metadata
                head = fp.read(2048)
            # Try to extract metadata from the head
            try:
                partial = json.loads(head + '}}')  # Close the JSON
                meta = partial.get("metadata", {})
            except Exception:
                meta = {}

            results.append({
                "filename": f.name,
                "path": str(f),
                "size_mb": round(size_mb, 2),
                "modified_at": datetime.fromtimestamp(
                    f.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
                "metadata": meta,
            })
        except Exception:
            continue

    return results
