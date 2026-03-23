"""InfraForge SQL firewall remediation helpers."""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

from src.config import (
    AZURE_RESOURCE_GROUP,
    AZURE_SQL_SERVER,
    SQL_FIREWALL_CONNECT_RETRY_DELAY_SEC,
    SQL_FIREWALL_IP_LOOKUP_TIMEOUT_SEC,
    SQL_FIREWALL_PROPAGATION_INTERVAL_SEC,
    SQL_FIREWALL_PROPAGATION_TIMEOUT_SEC,
    SQL_FIREWALL_RULE_NAME,
)

logger = logging.getLogger("infraforge.firewall")

_IS_WIN = sys.platform == "win32"
_AZ = shutil.which("az") or "az"


@dataclass(slots=True)
class FirewallEnsureResult:
    success: bool
    attempted: bool
    changed: bool
    verified: bool
    server: str | None
    resource_group: str | None
    ip: str | None
    reason: str
    message: str = ""


def is_sql_firewall_block_error(error_message: str) -> bool:
    """Return True when Azure SQL rejected the client IP."""
    normalized = error_message.lower()
    return (
        "is not allowed to access the server" in normalized
        or "client with ip address" in normalized and "not allowed" in normalized
    )


def extract_blocked_ip(error_message: str) -> str | None:
    """Extract the blocked client IPv4 address from an Azure SQL error."""
    ip_match = re.search(r"Client with IP address '([^']+)'", error_message, re.IGNORECASE)
    return ip_match.group(1) if ip_match else None


def get_firewall_retry_delay(attempt_index: int) -> float:
    """Return the bounded retry delay used between connection attempts."""
    return max(SQL_FIREWALL_CONNECT_RETRY_DELAY_SEC, 0.0) * max(1, 2 ** attempt_index)


def _parse_server_from_connection_string() -> str | None:
    """Extract the SQL server short name from AZURE_SQL_CONNECTION_STRING."""
    connection_string = os.environ.get("AZURE_SQL_CONNECTION_STRING", "")
    match = re.search(
        r"Server\s*=\s*tcp:([^.]+)\.database\.windows\.net",
        connection_string,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _resolve_sql_server() -> str | None:
    return AZURE_SQL_SERVER or _parse_server_from_connection_string()


def _validate_az_cli_ready() -> tuple[bool, str]:
    if shutil.which("az") is None and _AZ == "az":
        return False, "Azure CLI not found in PATH"

    try:
        version_result = subprocess.run(
            [_AZ, "version"],
            capture_output=True,
            text=True,
            timeout=15,
            shell=_IS_WIN,
        )
    except Exception as exc:
        return False, f"Azure CLI invocation failed: {exc}"

    if version_result.returncode != 0:
        return False, version_result.stderr.strip() or "Azure CLI version check failed"

    account_result = subprocess.run(
        [_AZ, "account", "show", "-o", "none"],
        capture_output=True,
        text=True,
        timeout=15,
        shell=_IS_WIN,
    )
    if account_result.returncode != 0:
        return False, account_result.stderr.strip() or "Azure CLI is not authenticated"

    return True, ""


async def ensure_sql_firewall(blocked_ip: str | None = None) -> FirewallEnsureResult:
    """Ensure the current IP is allowed through the Azure SQL firewall."""
    try:
        server = _resolve_sql_server()
        resource_group = AZURE_RESOURCE_GROUP
        if not server:
            message = "Cannot determine SQL server name — set AZURE_SQL_SERVER or AZURE_SQL_CONNECTION_STRING"
            logger.warning(message)
            return FirewallEnsureResult(False, False, False, False, None, resource_group, None, "missing_server", message)

        ip = blocked_ip
        if not ip:
            ip = await asyncio.get_running_loop().run_in_executor(None, _get_public_ip)
        if not ip:
            message = "Could not detect public IP — skipping firewall remediation"
            logger.warning(message)
            return FirewallEnsureResult(False, False, False, False, server, resource_group, None, "ip_not_detected", message)

        az_ok, az_message = await asyncio.get_running_loop().run_in_executor(None, _validate_az_cli_ready)
        if not az_ok:
            logger.warning("SQL firewall auto-fix unavailable: %s", az_message)
            return FirewallEnsureResult(False, False, False, False, server, resource_group, ip, "az_not_ready", az_message)

        public_access_ok = await asyncio.get_running_loop().run_in_executor(
            None,
            _ensure_public_access,
            server,
            resource_group,
        )
        if not public_access_ok:
            logger.warning("Could not verify Azure SQL public network access for server '%s'", server)

        current_ip = await asyncio.get_running_loop().run_in_executor(
            None,
            _get_existing_rule_ip,
            server,
            resource_group,
        )

        if current_ip == ip:
            logger.info("SQL firewall rule '%s' already set to %s", SQL_FIREWALL_RULE_NAME, ip)
            return FirewallEnsureResult(True, True, False, True, server, resource_group, ip, "already_configured")

        logger.info(
            "Updating SQL firewall rule '%s': %s -> %s",
            SQL_FIREWALL_RULE_NAME,
            current_ip or "(none)",
            ip,
        )
        updated = await asyncio.get_running_loop().run_in_executor(
            None,
            _update_firewall_rule,
            server,
            resource_group,
            ip,
        )
        if not updated:
            message = "Failed to update SQL firewall rule — may need manual fix"
            logger.warning(message)
            return FirewallEnsureResult(False, True, False, False, server, resource_group, ip, "rule_update_failed", message)

        verified = await _wait_for_rule_ip(server, resource_group, ip)
        if verified:
            logger.info("SQL firewall rule '%s' verified for %s", SQL_FIREWALL_RULE_NAME, ip)
            return FirewallEnsureResult(True, True, True, True, server, resource_group, ip, "updated")

        message = (
            f"Firewall rule '{SQL_FIREWALL_RULE_NAME}' was updated but did not propagate within "
            f"{SQL_FIREWALL_PROPAGATION_TIMEOUT_SEC} seconds"
        )
        logger.warning(message)
        return FirewallEnsureResult(False, True, True, False, server, resource_group, ip, "propagation_timeout", message)
    except Exception as exc:
        message = f"SQL firewall auto-fix failed: {exc}"
        logger.warning(message)
        return FirewallEnsureResult(
            False,
            True,
            False,
            False,
            _resolve_sql_server(),
            AZURE_RESOURCE_GROUP,
            blocked_ip,
            "exception",
            message,
        )


def _get_public_ip() -> str | None:
    """Get the current public IP via ipify."""
    import urllib.request

    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=SQL_FIREWALL_IP_LOOKUP_TIMEOUT_SEC) as response:
            return response.read().decode().strip()
    except Exception:
        return None


def _get_existing_rule_ip(server: str, resource_group: str) -> str | None:
    """Check if the managed firewall rule exists and what IP it is set to."""
    try:
        result = subprocess.run(
            [
                _AZ,
                "sql",
                "server",
                "firewall-rule",
                "show",
                "--server",
                server,
                "--resource-group",
                resource_group,
                "--name",
                SQL_FIREWALL_RULE_NAME,
                "--query",
                "startIpAddress",
                "-o",
                "tsv",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            shell=_IS_WIN,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        if result.returncode != 0 and result.stderr.strip():
            logger.debug("Firewall rule lookup: %s", result.stderr.strip())
    except Exception as exc:
        logger.debug("Firewall rule lookup failed: %s", exc)
    return None


def _update_firewall_rule(server: str, resource_group: str, ip: str) -> bool:
    """Create or update the managed firewall rule with the current IP."""
    try:
        result = subprocess.run(
            [
                _AZ,
                "sql",
                "server",
                "firewall-rule",
                "create",
                "--server",
                server,
                "--resource-group",
                resource_group,
                "--name",
                SQL_FIREWALL_RULE_NAME,
                "--start-ip-address",
                ip,
                "--end-ip-address",
                ip,
                "-o",
                "none",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            shell=_IS_WIN,
        )
        if result.returncode != 0:
            logger.warning("az firewall-rule create failed: %s", result.stderr.strip())
        return result.returncode == 0
    except Exception as exc:
        logger.warning("az firewall-rule create exception: %s", exc)
        return False


def _ensure_public_access(server: str, resource_group: str) -> bool:
    """Ensure public network access is enabled on the SQL server."""
    try:
        result = subprocess.run(
            [
                _AZ,
                "sql",
                "server",
                "update",
                "--name",
                server,
                "--resource-group",
                resource_group,
                "--enable-public-network",
                "true",
                "-o",
                "none",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            shell=_IS_WIN,
        )
        if result.returncode != 0:
            logger.debug("Enable public access failed: %s", result.stderr.strip())
            return False
        return True
    except Exception as exc:
        logger.debug("Enable public access exception: %s", exc)
        return False


async def _wait_for_rule_ip(server: str, resource_group: str, expected_ip: str) -> bool:
    deadline = time.monotonic() + max(SQL_FIREWALL_PROPAGATION_TIMEOUT_SEC, 0)
    interval = max(SQL_FIREWALL_PROPAGATION_INTERVAL_SEC, 0.1)

    while True:
        current_ip = await asyncio.get_running_loop().run_in_executor(
            None,
            _get_existing_rule_ip,
            server,
            resource_group,
        )
        if current_ip == expected_ip:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(interval)
