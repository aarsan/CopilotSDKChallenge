"""
InfraForge — Template Dependency Engine

Templates are the unit of deployment in InfraForge.
Services are never deployed directly — they are building blocks inside templates.

Each template declares:
  - provides:      resource types this template creates
  - requires:      existing resources that must be supplied at deploy time
  - optional_refs: existing resources that CAN be referenced but aren't required

Template types:
  - foundation:  deploys standalone (networking, security, monitoring)
  - workload:    requires existing infrastructure (VMs, apps, databases)
  - composite:   bundles foundation + workload — self-contained

At deploy time, InfraForge queries Azure Resource Graph to populate
resource pickers for required dependencies — one lightweight API call
per dependency type, NOT a full CMDB scan.
"""

import logging
from typing import Optional

logger = logging.getLogger("infraforge.template_engine")


# ══════════════════════════════════════════════════════════════
# TEMPLATE TYPES
# ══════════════════════════════════════════════════════════════

TEMPLATE_TYPES = {
    "foundation": {
        "label": "Foundation",
        "description": "Deploys standalone — creates shared infrastructure (networking, security, monitoring)",
        "icon": "🏗️",
        "deployable_standalone": True,
    },
    "workload": {
        "label": "Workload",
        "description": "Requires existing infrastructure — deploys application resources into existing foundations",
        "icon": "⚙️",
        "deployable_standalone": False,
    },
    "composite": {
        "label": "Composite",
        "description": "Bundles foundation + workload — self-contained, deploys everything needed",
        "icon": "📦",
        "deployable_standalone": True,
    },
}


# ══════════════════════════════════════════════════════════════
# RESOURCE DEPENDENCY MAP
# ══════════════════════════════════════════════════════════════

# Known Azure resource type dependencies.
# Maps: resource_type → list of resources it typically needs.
# `required=True`         → must exist before deploying
# `created_by_template`   → auto-created within the same template (e.g. NIC for VM)

RESOURCE_DEPENDENCIES: dict[str, list[dict]] = {
    # ── Compute ──
    "Microsoft.Compute/virtualMachines": [
        {"type": "Microsoft.Network/virtualNetworks", "reason": "VM must be connected to a VNet", "required": True},
        {"type": "Microsoft.Network/virtualNetworks/subnets", "reason": "VM NIC needs a subnet", "required": True},
        {"type": "Microsoft.Network/networkInterfaces", "reason": "VM needs a NIC", "required": True, "created_by_template": True},
        {"type": "Microsoft.Network/publicIPAddresses", "reason": "Public IP for direct access (not recommended for prod)", "required": False},
        {"type": "Microsoft.KeyVault/vaults", "reason": "Store VM credentials securely", "required": False},
        {"type": "Microsoft.Network/networkSecurityGroups", "reason": "NSG for subnet security rules", "required": False},
    ],
    "Microsoft.Web/sites": [
        {"type": "Microsoft.Web/serverfarms", "reason": "App Service requires an App Service Plan", "required": True, "created_by_template": True},
        {"type": "Microsoft.Network/virtualNetworks", "reason": "VNet integration for private networking", "required": False},
        {"type": "Microsoft.KeyVault/vaults", "reason": "App configuration secrets", "required": False},
        {"type": "Microsoft.Insights/components", "reason": "Application Insights monitoring", "required": False},
    ],
    "Microsoft.Web/serverfarms": [],  # Foundation — no deps
    "Microsoft.ContainerService/managedClusters": [
        {"type": "Microsoft.Network/virtualNetworks", "reason": "AKS needs a VNet for CNI networking", "required": True},
        {"type": "Microsoft.Network/virtualNetworks/subnets", "reason": "AKS node pool subnet", "required": True},
        {"type": "Microsoft.ContainerRegistry/registries", "reason": "Container image registry", "required": False},
        {"type": "Microsoft.KeyVault/vaults", "reason": "Secrets and certificate management", "required": False},
        {"type": "Microsoft.OperationalInsights/workspaces", "reason": "Log Analytics for monitoring", "required": False},
    ],
    "Microsoft.App/containerApps": [
        {"type": "Microsoft.App/managedEnvironments", "reason": "Container Apps need a managed environment", "required": True, "created_by_template": True},
        {"type": "Microsoft.Network/virtualNetworks", "reason": "VNet integration", "required": False},
        {"type": "Microsoft.ContainerRegistry/registries", "reason": "Container image registry", "required": False},
    ],
    "Microsoft.ContainerInstance/containerGroups": [
        {"type": "Microsoft.Network/virtualNetworks", "reason": "VNet integration for private access", "required": False},
    ],

    # ── Database ──
    "Microsoft.Sql/servers": [
        {"type": "Microsoft.Network/virtualNetworks", "reason": "Private endpoint networking", "required": False},
        {"type": "Microsoft.KeyVault/vaults", "reason": "Store admin credentials", "required": False},
    ],
    "Microsoft.Sql/servers/databases": [
        {"type": "Microsoft.Sql/servers", "reason": "Database requires a SQL Server", "required": True, "created_by_template": True},
        {"type": "Microsoft.Network/virtualNetworks", "reason": "Private endpoint networking", "required": False},
    ],
    "Microsoft.DBforPostgreSQL/flexibleServers": [
        {"type": "Microsoft.Network/virtualNetworks", "reason": "VNet integration for private access", "required": False},
        {"type": "Microsoft.Network/privateDnsZones", "reason": "Private DNS for VNet-integrated server", "required": False},
    ],
    "Microsoft.DocumentDB/databaseAccounts": [
        {"type": "Microsoft.Network/virtualNetworks", "reason": "Private endpoint networking", "required": False},
    ],
    "Microsoft.Cache/Redis": [
        {"type": "Microsoft.Network/virtualNetworks", "reason": "VNet injection for premium tier", "required": False},
    ],

    # ── Security ──
    "Microsoft.KeyVault/vaults": [],  # Foundation — no deps
    "Microsoft.ManagedIdentity/userAssignedIdentities": [],  # Foundation

    # ── Storage ──
    "Microsoft.Storage/storageAccounts": [
        {"type": "Microsoft.Network/virtualNetworks", "reason": "Private endpoint networking", "required": False},
    ],

    # ── Monitoring ──
    "Microsoft.OperationalInsights/workspaces": [],  # Foundation
    "Microsoft.Insights/components": [
        {"type": "Microsoft.OperationalInsights/workspaces", "reason": "Log Analytics workspace for data storage", "required": False},
    ],

    # ── Networking ──
    "Microsoft.Network/virtualNetworks": [],  # Foundation
    "Microsoft.Network/networkSecurityGroups": [],  # Foundation
    "Microsoft.Network/publicIPAddresses": [],  # Foundation
    "Microsoft.Network/applicationGateways": [
        {"type": "Microsoft.Network/virtualNetworks", "reason": "App Gateway needs a dedicated subnet", "required": True},
        {"type": "Microsoft.Network/virtualNetworks/subnets", "reason": "Dedicated subnet for App Gateway", "required": True},
        {"type": "Microsoft.Network/publicIPAddresses", "reason": "Frontend public IP", "required": True, "created_by_template": True},
    ],

    # ── AI ──
    "Microsoft.CognitiveServices/accounts": [
        {"type": "Microsoft.Network/virtualNetworks", "reason": "Private endpoint networking", "required": False},
    ],
    "Microsoft.MachineLearningServices/workspaces": [
        {"type": "Microsoft.Storage/storageAccounts", "reason": "ML workspace requires a storage account", "required": True, "created_by_template": True},
        {"type": "Microsoft.KeyVault/vaults", "reason": "Secrets and model keys", "required": True, "created_by_template": True},
        {"type": "Microsoft.Insights/components", "reason": "Application Insights for experiment tracking", "required": True, "created_by_template": True},
        {"type": "Microsoft.ContainerRegistry/registries", "reason": "Container registry for model images", "required": False},
        {"type": "Microsoft.Network/virtualNetworks", "reason": "VNet integration", "required": False},
    ],
}

# Resource types that are pure foundations (no dependencies)
FOUNDATION_TYPES = {
    rtype for rtype, deps in RESOURCE_DEPENDENCIES.items()
    if not deps
}


# ══════════════════════════════════════════════════════════════
# DEPENDENCY ANALYSIS
# ══════════════════════════════════════════════════════════════

def analyze_dependencies(service_ids: list[str]) -> dict:
    """
    Analyze a list of service IDs to determine:
    - template_type: foundation / workload / composite
    - provides: what resource types this template creates
    - requires: what existing infrastructure must be supplied
    - optional_refs: what existing resources CAN be referenced
    - auto_created: what supporting resources are auto-created
    """
    provides = set(service_ids)
    requires = []
    optional = []
    auto_created = []
    seen = set()

    for svc_id in service_ids:
        deps = RESOURCE_DEPENDENCIES.get(svc_id, [])
        for dep in deps:
            dep_type = dep["type"]
            if dep_type in provides or dep_type in seen:
                continue
            seen.add(dep_type)

            if dep.get("created_by_template"):
                # This supporting resource is auto-created within the template
                auto_created.append({
                    "type": dep_type,
                    "reason": dep["reason"],
                })
                provides.add(dep_type)
            elif dep["required"]:
                # This resource MUST exist before deployment
                requires.append({
                    "type": dep_type,
                    "reason": dep["reason"],
                    "parameter": _make_param_name(dep_type),
                })
            else:
                # This resource CAN be referenced but isn't mandatory
                optional.append({
                    "type": dep_type,
                    "reason": dep["reason"],
                    "parameter": _make_param_name(dep_type),
                })

    # Determine template type based on dependencies
    if not requires:
        # No external dependencies → foundation or composite
        if len(service_ids) == 1 and service_ids[0] in FOUNDATION_TYPES:
            template_type = "foundation"
        elif len(service_ids) <= 2 and all(s in FOUNDATION_TYPES for s in service_ids):
            template_type = "foundation"
        else:
            template_type = "composite"
    else:
        template_type = "workload"

    # If it has required deps but also bundles multiple services, check if
    # it could be composite (if it includes its own foundation)
    if template_type == "workload" and len(service_ids) >= 3:
        has_networking = any(
            s.startswith("Microsoft.Network/") for s in service_ids
        )
        if has_networking:
            # Re-check: does the template include the required infra?
            all_covered = all(
                r["type"] in provides for r in requires
            )
            if all_covered:
                template_type = "composite"
                requires = []  # All covered internally

    return {
        "template_type": template_type,
        "provides": sorted(provides),
        "requires": requires,
        "optional_refs": optional,
        "auto_created": auto_created,
        "deployable_standalone": template_type in ("foundation", "composite"),
    }


def _make_param_name(resource_type: str) -> str:
    """Generate a parameter name from a resource type, e.g. 'existingVirtualNetworksId'."""
    short = resource_type.split("/")[-1]
    # Capitalize first letter
    return f"existing{short[0].upper()}{short[1:]}Id"


# ══════════════════════════════════════════════════════════════
# AZURE RESOURCE GRAPH DISCOVERY (DEPLOY-TIME)
# ══════════════════════════════════════════════════════════════

async def discover_existing_resources(
    resource_type: str,
    subscription_id: Optional[str] = None,
) -> list[dict]:
    """
    Query Azure Resource Graph to find existing resources of a given type.
    Used at deploy time to populate resource pickers for template dependencies.

    This is a LIGHTWEIGHT query:
    - One API call per resource type
    - Read-only (uses existing DefaultAzureCredential)
    - Returns at most 100 resources
    - No state to maintain — query live at deploy time
    """
    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.resource import ResourceManagementClient

        credential = DefaultAzureCredential(
            exclude_workload_identity_credential=True,
            exclude_managed_identity_credential=True,
            exclude_developer_cli_credential=True,
            exclude_powershell_credential=True,
            exclude_visual_studio_code_credential=True,
            exclude_interactive_browser_credential=True,
        )

        # If no subscription_id provided, get the default one
        if not subscription_id:
            from azure.mgmt.resource import SubscriptionClient
            sub_client = SubscriptionClient(credential)
            import asyncio
            loop = asyncio.get_event_loop()
            subs = await loop.run_in_executor(
                None, lambda: list(sub_client.subscriptions.list())
            )
            if subs:
                subscription_id = subs[0].subscription_id
            else:
                logger.warning("No Azure subscriptions found for resource discovery")
                return []

        client = ResourceManagementClient(credential, subscription_id)
        import asyncio
        loop = asyncio.get_event_loop()

        # List resources by type — lightweight, one API call
        resources_iter = await loop.run_in_executor(
            None,
            lambda: list(client.resources.list(
                filter=f"resourceType eq '{resource_type}'",
                top=100,
            ))
        )

        results = []
        for r in resources_iter:
            results.append({
                "id": r.id,
                "name": r.name,
                "resource_group": r.id.split("/")[4] if r.id and len(r.id.split("/")) > 4 else "",
                "location": r.location or "",
                "subscription_id": subscription_id,
                "tags": dict(r.tags) if r.tags else {},
                "type": r.type,
            })

        logger.info(f"Discovered {len(results)} existing {resource_type} resources")
        return results

    except ImportError:
        logger.warning("azure-mgmt-resource not available for resource discovery")
        return []
    except Exception as e:
        logger.warning(f"Resource discovery failed for {resource_type}: {e}")
        return []


async def discover_subnets_for_vnet(
    vnet_resource_id: str,
    subscription_id: Optional[str] = None,
) -> list[dict]:
    """
    Get subnets for a specific VNet.
    Used when a user selects a VNet and we need to show available subnets.
    """
    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.network import NetworkManagementClient
        import asyncio

        credential = DefaultAzureCredential(
            exclude_workload_identity_credential=True,
            exclude_managed_identity_credential=True,
            exclude_developer_cli_credential=True,
            exclude_powershell_credential=True,
            exclude_visual_studio_code_credential=True,
            exclude_interactive_browser_credential=True,
        )

        # Parse VNet resource ID to get subscription, rg, and vnet name
        parts = vnet_resource_id.split("/")
        sub_id = parts[2] if len(parts) > 2 else subscription_id
        rg_name = parts[4] if len(parts) > 4 else ""
        vnet_name = parts[8] if len(parts) > 8 else ""

        if not (sub_id and rg_name and vnet_name):
            return []

        client = NetworkManagementClient(credential, sub_id)
        loop = asyncio.get_event_loop()

        subnets = await loop.run_in_executor(
            None,
            lambda: list(client.subnets.list(rg_name, vnet_name))
        )

        results = []
        for s in subnets:
            results.append({
                "id": s.id,
                "name": s.name,
                "address_prefix": s.address_prefix or "",
                "nsg": s.network_security_group.id if s.network_security_group else None,
                "available_ips": getattr(s, "available_ip_address_count", None),
            })

        return results

    except Exception as e:
        logger.warning(f"Subnet discovery failed: {e}")
        return []
