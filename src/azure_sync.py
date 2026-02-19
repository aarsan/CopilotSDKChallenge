"""
InfraForge — Azure Resource Provider Sync

Pulls the full list of Azure resource types from the ARM API
(via the logged-in subscription) and caches them in the services table.

Existing governance data (status, policies, SKUs, regions, etc.) is
NEVER overwritten — only *new* resource types discovered from Azure are
inserted with status='not_approved' so that the platform team can then
review and promote them through the governance workflow.

Usage:
    from src.azure_sync import sync_azure_services
    result = await sync_azure_services()
"""

import asyncio
import logging
import time
from typing import Optional, Callable, Awaitable

logger = logging.getLogger("infraforge.azure_sync")

# Type alias for the optional progress callback.
# It receives a dict like {"phase": "scanning", "detail": "...", "progress": 0.3, ...}
ProgressCallback = Optional[Callable[[dict], Awaitable[None]]]


# ── Singleton Sync Manager ───────────────────────────────────
# Ensures only one sync runs at a time.  Multiple SSE subscribers can
# attach and all receive the same progress stream.  Late joiners get
# the full history replayed so they immediately see the current state.

class SyncManager:
    """Process-wide singleton that coordinates Azure resource sync."""

    def __init__(self):
        self.running = False
        self.history: list[dict] = []        # all progress events so far
        self._subscribers: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()
        self.last_completed: Optional[dict] = None   # summary from last run
        self.last_completed_at: Optional[float] = None

    async def broadcast(self, event: dict):
        """Send a progress event to all subscribers and record in history."""
        self.history.append(event)
        dead: list[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except Exception:
                dead.append(q)
        for q in dead:
            self._subscribers.remove(q)

    def subscribe(self) -> asyncio.Queue:
        """Create a new subscriber queue.  Replays history immediately."""
        q: asyncio.Queue = asyncio.Queue()
        # Replay everything that already happened
        for event in self.history:
            q.put_nowait(event)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def start_sync(self):
        """Begin a sync if one is not already running.  Returns True if started."""
        async with self._lock:
            if self.running:
                return False
            self.running = True
            self.history.clear()
            return True

    async def finish_sync(self, summary: Optional[dict] = None):
        async with self._lock:
            self.running = False
            if summary:
                self.last_completed = summary
                self.last_completed_at = time.time()
            # Send sentinel to all remaining subscribers so they know we're done
            for q in self._subscribers:
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
            self._subscribers.clear()

    def status(self) -> dict:
        """Return a JSON-safe status snapshot."""
        return {
            "running": self.running,
            "progress": self.history[-1] if self.history else None,
            "last_completed": self.last_completed,
            "last_completed_at_iso": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.last_completed_at))
                if self.last_completed_at else None
            ),
            "last_completed_ago_sec": (
                round(time.time() - self.last_completed_at)
                if self.last_completed_at else None
            ),
            "total_azure_resource_types": (
                self.last_completed.get("resource_types_discovered")
                if self.last_completed else None
            ),
        }


# Module-level singleton
sync_manager = SyncManager()

# ── Category mapping ─────────────────────────────────────────
# Maps Azure resource-provider namespaces to human-friendly categories.
NAMESPACE_CATEGORY_MAP: dict[str, str] = {
    "microsoft.web": "compute",
    "microsoft.app": "compute",
    "microsoft.compute": "compute",
    "microsoft.containerservice": "compute",
    "microsoft.containerinstance": "compute",
    "microsoft.containerregistry": "compute",
    "microsoft.batch": "compute",
    "microsoft.servicefabric": "compute",
    "microsoft.classiccompute": "compute",
    "microsoft.sql": "database",
    "microsoft.dbformysql": "database",
    "microsoft.dbforpostgresql": "database",
    "microsoft.dbformariadb": "database",
    "microsoft.documentdb": "database",
    "microsoft.cache": "database",
    "microsoft.kusto": "database",
    "microsoft.synapse": "database",
    "microsoft.datafactory": "database",
    "microsoft.storage": "storage",
    "microsoft.storagesync": "storage",
    "microsoft.classicstorage": "storage",
    "microsoft.netapp": "storage",
    "microsoft.elasticsan": "storage",
    "microsoft.network": "networking",
    "microsoft.cdn": "networking",
    "microsoft.frontdoor": "networking",
    "microsoft.classicnetwork": "networking",
    "microsoft.relay": "networking",
    "microsoft.keyvault": "security",
    "microsoft.managedidentity": "security",
    "microsoft.aad": "security",
    "microsoft.azureactivedirectory": "security",
    "microsoft.security": "security",
    "microsoft.authorization": "security",
    "microsoft.operationalinsights": "monitoring",
    "microsoft.insights": "monitoring",
    "microsoft.monitor": "monitoring",
    "microsoft.alertsmanagement": "monitoring",
    "microsoft.loganalytics": "monitoring",
    "microsoft.dashboard": "monitoring",
    "microsoft.cognitiveservices": "ai",
    "microsoft.machinelearningservices": "ai",
    "microsoft.search": "ai",
    "microsoft.botservice": "ai",
    "microsoft.openai": "ai",
    "microsoft.eventhub": "messaging",
    "microsoft.servicebus": "messaging",
    "microsoft.eventgrid": "messaging",
    "microsoft.signalrservice": "messaging",
    "microsoft.notificationhubs": "messaging",
    "microsoft.apimanagement": "integration",
    "microsoft.logic": "integration",
    "microsoft.appconfiguration": "integration",
    "microsoft.devices": "iot",
    "microsoft.iotcentral": "iot",
    "microsoft.digitaltwins": "iot",
    "microsoft.timeseriesinsights": "iot",
    "microsoft.devtestlab": "devtools",
    "microsoft.devcenter": "devtools",
    "microsoft.devops": "devtools",
    "microsoft.visualstudio": "devtools",
}

# ── Resource types to skip (noise, internal, or not user-facing) ──
SKIP_SUFFIXES = {
    "/operations",
    "/operationresults",
    "/operationstatuses",
    "/locations",
    "/usages",
    "/checknameavailability",
    "/skus",
    "/providers",
    "/diagnosticsettings",
    "/metricdefinitions",
    "/logdefinitions",
    "/eventtypes",
    "/capabilities",
    "/quotas",
}

SKIP_NAMESPACES = {
    "microsoft.addons",
    "microsoft.advisor",
    "microsoft.billing",
    "microsoft.capacity",
    "microsoft.changeanalysis",
    "microsoft.commerce",
    "microsoft.consumption",
    "microsoft.costmanagement",
    "microsoft.costmanagementexports",
    "microsoft.features",
    "microsoft.guestconfiguration",
    "microsoft.hybridcompute",
    "microsoft.maintenance",
    "microsoft.managedservices",
    "microsoft.marketplace",
    "microsoft.marketplacenotifications",
    "microsoft.marketplaceordering",
    "microsoft.policyinsights",
    "microsoft.portal",
    "microsoft.providerhub",
    "microsoft.quota",
    "microsoft.resourcegraph",
    "microsoft.resourcehealth",
    "microsoft.resources",
    "microsoft.serialconsole",
    "microsoft.softwareplan",
    "microsoft.solutions",
    "microsoft.subscription",
    "microsoft.support",
}

# Max nesting depth for resource type paths (e.g. servers/databases = 2)
MAX_TYPE_DEPTH = 2


def _classify_category(namespace: str) -> str:
    """Map a provider namespace to a service category."""
    return NAMESPACE_CATEGORY_MAP.get(namespace.lower(), "other")


def _friendly_name(namespace: str, resource_type: str) -> str:
    """Generate a human-readable name from a provider namespace and resource type.

    Examples:
        Microsoft.Web / sites → "Web Sites"
        Microsoft.Sql / servers/databases → "SQL Servers Databases"
    """
    # Strip the 'Microsoft.' prefix
    ns_short = namespace.split(".")[-1] if "." in namespace else namespace

    # Convert camelCase/PascalCase parts to spaced words
    parts = resource_type.replace("/", " ").split()
    words = []
    for part in parts:
        # Insert space before uppercase letters in camelCase
        word = ""
        for i, ch in enumerate(part):
            if ch.isupper() and i > 0 and part[i - 1].islower():
                word += " "
            word += ch
        words.append(word)

    # Capitalize the namespace short name nicely
    return f"{ns_short} — {' '.join(words)}".title()


def _should_skip(namespace: str, resource_type: str) -> bool:
    """Return True if this resource type should be excluded from the catalog."""
    ns_lower = namespace.lower()

    # Skip entire namespaces that aren't user-facing resources
    if ns_lower in SKIP_NAMESPACES:
        return True

    # Only sync namespaces we've classified — unknown ones are usually
    # internal/infra-only and create noise (this cuts ~2000 types down to ~400)
    if ns_lower not in NAMESPACE_CATEGORY_MAP:
        return True

    # Skip deeply nested types (usually child operations, not top-level resources)
    if resource_type.count("/") >= MAX_TYPE_DEPTH:
        return True

    # Skip operational / metadata endpoints
    rt_lower = resource_type.lower()
    for suffix in SKIP_SUFFIXES:
        if rt_lower.endswith(suffix.lower().lstrip("/")):
            return True

    return False


async def run_sync_managed() -> bool:
    """Start a managed sync through the SyncManager singleton.

    Returns True if a new sync was started, False if one is already running.
    Progress is automatically broadcast to all subscribers.
    """
    started = await sync_manager.start_sync()
    if not started:
        return False

    async def _run():
        try:
            summary = await sync_azure_services(on_progress=sync_manager.broadcast)
            await sync_manager.finish_sync(summary)
        except Exception as e:
            await sync_manager.broadcast({"phase": "error", "detail": str(e), "progress": 0})
            await sync_manager.finish_sync()

    asyncio.create_task(_run())
    return True


async def sync_azure_services(
    subscription_id: Optional[str] = None,
    on_progress: ProgressCallback = None,
) -> dict:
    """
    Pull resource providers from Azure ARM API and sync new ones into the DB.

    - Uses DefaultAzureCredential (same as the rest of InfraForge)
    - Only INSERTs services that don't already exist in the DB
    - Never overwrites existing governance data (status, policies, SKUs, etc.)
    - New services get status='not_approved' so the platform team can review them
    - If ``on_progress`` is supplied it is awaited with status dicts at each phase.

    Returns a summary dict with counts of discovered, new, and skipped services.
    """
    import os
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.resource import ResourceManagementClient

    async def _emit(data: dict):
        if on_progress:
            await on_progress(data)

    # Resolve subscription ID
    if not subscription_id:
        subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID", "")

    if not subscription_id:
        # Try to get it from az cli
        try:
            import subprocess
            result = subprocess.run(
                ["az", "account", "show", "--query", "id", "-o", "tsv"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                subscription_id = result.stdout.strip()
        except Exception:
            pass

    if not subscription_id:
        raise ValueError(
            "No Azure subscription ID available. Set AZURE_SUBSCRIPTION_ID "
            "or log in with `az login`."
        )

    logger.info(f"Syncing Azure resource providers from subscription {subscription_id}")

    await _emit({"phase": "connecting", "detail": "Authenticating to Azure ARM API…", "progress": 0.05})

    # ── 1. Fetch resource providers from ARM API ──────────────
    credential = DefaultAzureCredential(
        exclude_workload_identity_credential=True,
        exclude_managed_identity_credential=True,
    )

    client = ResourceManagementClient(credential, subscription_id)

    await _emit({"phase": "scanning", "detail": "Listing Azure resource providers (this may take a moment)…", "progress": 0.10})

    # The Azure mgmt SDK is synchronous — run in a thread so we don't
    # block the async event loop (especially important at startup).
    import asyncio
    loop = asyncio.get_event_loop()
    providers = await loop.run_in_executor(None, lambda: list(client.providers.list()))

    await _emit({"phase": "scanning", "detail": f"Scanned {len(providers)} resource providers", "progress": 0.40})

    # ── 2. Build a flat list of resource types ────────────────
    discovered: list[dict] = []
    skipped = 0

    for provider in providers:
        namespace = provider.namespace or ""
        if not namespace:
            continue

        for rt in (provider.resource_types or []):
            type_name = rt.resource_type or ""
            if not type_name:
                continue

            if _should_skip(namespace, type_name):
                skipped += 1
                continue

            service_id = f"{namespace}/{type_name}"
            category = _classify_category(namespace)
            friendly = _friendly_name(namespace, type_name)

            # Collect available locations for this resource type
            locations = sorted(set(
                loc.lower().replace(" ", "")
                for loc in (rt.locations or [])
                if loc and loc.lower() not in {"global", ""}
            ))

            discovered.append({
                "id": service_id,
                "name": friendly,
                "category": category,
                "azure_locations": locations,
            })

    logger.info(
        f"Discovered {len(discovered)} resource types from Azure "
        f"({skipped} skipped as non-user-facing)"
    )

    await _emit({"phase": "filtering", "detail": f"Found {len(discovered)} resource types ({skipped} noise filtered out)", "progress": 0.55, "discovered": len(discovered), "skipped": skipped})

    # ── 3. Upsert into DB (only new services) ────────────────
    from src.database import get_all_services, bulk_insert_services

    existing_services = await get_all_services()
    existing_ids = {svc["id"] for svc in existing_services}

    to_insert = [s for s in discovered if s["id"] not in existing_ids]
    await _emit({"phase": "inserting", "detail": f"{len(to_insert)} new services to add ({len(existing_ids)} already cataloged)", "progress": 0.60, "new_total": len(to_insert), "existing": len(existing_ids)})

    new_count = 0
    insert_total = len(to_insert)
    BATCH_SIZE = 50

    for batch_start in range(0, insert_total, BATCH_SIZE):
        batch = to_insert[batch_start : batch_start + BATCH_SIZE]
        batch_records = [
            {
                "id": svc["id"],
                "name": svc["name"],
                "category": svc["category"],
                "status": "not_approved",
                "risk_tier": "medium",
                "review_notes": "Auto-discovered from Azure. Pending platform team review.",
                "contact": "platform-team@contoso.com",
                "approved_regions": svc.get("azure_locations", [])[:10],
            }
            for svc in batch
        ]
        inserted = await bulk_insert_services(batch_records)
        new_count += inserted

        pct = 0.60 + 0.35 * (new_count / max(insert_total, 1))
        await _emit({"phase": "inserting", "detail": f"Added {new_count} / {insert_total} services…", "progress": round(pct, 2), "added": new_count, "of": insert_total})

        # Yield control so the event loop can serve read requests (table refresh)
        await asyncio.sleep(0)

    logger.info(f"Sync complete: {new_count} new services added, {len(existing_ids)} unchanged")

    summary = {
        "subscription_id": subscription_id,
        "providers_scanned": len(providers),
        "resource_types_discovered": len(discovered),
        "skipped": skipped,
        "new_services_added": new_count,
        "existing_unchanged": len(existing_ids),
        "total_in_catalog": len(existing_ids) + new_count,
    }
    await _emit({"phase": "done", "detail": "Sync complete!", "progress": 1.0, **summary})
    return summary
