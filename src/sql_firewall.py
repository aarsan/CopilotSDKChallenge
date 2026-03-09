"""
InfraForge — SQL Firewall Auto-Fix

On startup, detects the current public IP and ensures it's allowed
through the Azure SQL Server firewall. Eliminates the need for manual
`az sql server firewall-rule create` commands when the developer's IP
changes (VPN reconnect, network change, etc.).

Requires: Azure CLI (`az`) installed and authenticated.
"""

import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger("infraforge.firewall")

# Firewall rule name managed by InfraForge (won't touch other rules)
_RULE_NAME = "infraforge-dev-auto"

# On Windows, `az` is a .cmd batch wrapper — subprocess needs shell=True
# or the full path to find it.  We resolve once at import time.
_IS_WIN = sys.platform == "win32"
_AZ = shutil.which("az") or "az"


async def ensure_sql_firewall() -> None:
    """Ensure the current IP is allowed through the Azure SQL firewall.

    Steps:
    1. Detect current public IP via https://api.ipify.org
    2. Check if the rule already matches
    3. Create/update the rule if needed
    4. Also ensure public network access is enabled

    This is best-effort — failures are logged but don't block startup.
    """
    import asyncio

    try:
        server = os.environ.get("AZURE_SQL_SERVER", "infraforgesql")
        rg = os.environ.get("AZURE_RESOURCE_GROUP", "InfraForge")

        # Detect current public IP
        ip = await asyncio.get_event_loop().run_in_executor(None, _get_public_ip)
        if not ip:
            logger.warning("Could not detect public IP — skipping firewall check")
            return

        # Check existing rule
        current_ip = await asyncio.get_event_loop().run_in_executor(
            None, _get_existing_rule_ip, server, rg
        )

        if current_ip == ip:
            logger.info(f"SQL firewall rule '{_RULE_NAME}' already set to {ip}")
            return

        # Update the rule
        logger.info(f"Updating SQL firewall rule '{_RULE_NAME}': {current_ip or '(none)'} → {ip}")
        ok = await asyncio.get_event_loop().run_in_executor(
            None, _update_firewall_rule, server, rg, ip
        )
        if ok:
            logger.info(f"SQL firewall rule updated to {ip}")
        else:
            logger.warning("Failed to update SQL firewall rule — may need manual fix")

        # Ensure public network access is enabled
        await asyncio.get_event_loop().run_in_executor(
            None, _ensure_public_access, server, rg
        )

    except Exception as e:
        logger.warning(f"SQL firewall auto-fix failed (non-fatal): {e}")


def _get_public_ip() -> str | None:
    """Get the current public IP via ipify."""
    import urllib.request
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as resp:
            return resp.read().decode().strip()
    except Exception:
        return None


def _get_existing_rule_ip(server: str, rg: str) -> str | None:
    """Check if our firewall rule exists and what IP it's set to."""
    try:
        result = subprocess.run(
            [_AZ, "sql", "server", "firewall-rule", "show",
             "--server", server, "--resource-group", rg,
             "--name", _RULE_NAME,
             "--query", "startIpAddress", "-o", "tsv"],
            capture_output=True, text=True, timeout=30,
            shell=_IS_WIN,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        if result.returncode != 0 and result.stderr.strip():
            logger.debug(f"Firewall rule lookup: {result.stderr.strip()}")
    except Exception as e:
        logger.debug(f"Firewall rule lookup failed: {e}")
    return None


def _update_firewall_rule(server: str, rg: str, ip: str) -> bool:
    """Create or update the firewall rule with the current IP."""
    try:
        result = subprocess.run(
            [_AZ, "sql", "server", "firewall-rule", "create",
             "--server", server, "--resource-group", rg,
             "--name", _RULE_NAME,
             "--start-ip-address", ip, "--end-ip-address", ip,
             "-o", "none"],
            capture_output=True, text=True, timeout=30,
            shell=_IS_WIN,
        )
        if result.returncode != 0:
            logger.warning(f"az firewall-rule create failed: {result.stderr.strip()}")
        return result.returncode == 0
    except Exception as e:
        logger.warning(f"az firewall-rule create exception: {e}")
        return False


def _ensure_public_access(server: str, rg: str) -> None:
    """Ensure public network access is enabled on the SQL server."""
    try:
        result = subprocess.run(
            [_AZ, "sql", "server", "update",
             "--name", server, "--resource-group", rg,
             "--enable-public-network", "true",
             "-o", "none"],
            capture_output=True, text=True, timeout=30,
            shell=_IS_WIN,
        )
        if result.returncode != 0:
            logger.debug(f"Enable public access failed: {result.stderr.strip()}")
    except Exception as e:
        logger.debug(f"Enable public access exception: {e}")
