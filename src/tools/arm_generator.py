"""ARM Template Auto-Generator.

Generates basic, policy-compliant ARM templates for Azure resource types.
Uses built-in skeletons for common resource types and falls back to the
Copilot SDK for unknown types.

Each generated template follows these principles:
- Standard parameters: location, resourceName, environment, tags
- Location always uses [resourceGroup().location] as default
- Required tags from governance policies are included
- Minimal required properties for the resource type
- Proper API versions
"""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# ARM TEMPLATE SKELETON REGISTRY
# ══════════════════════════════════════════════════════════════

_STANDARD_PARAMETERS = {
    "resourceName": {
        "type": "string",
        "metadata": {"description": "Name of the resource"},
    },
    "location": {
        "type": "string",
        "defaultValue": "[resourceGroup().location]",
        "metadata": {"description": "Azure region for deployment"},
    },
    "environment": {
        "type": "string",
        "defaultValue": "dev",
        "allowedValues": ["dev", "staging", "prod"],
        "metadata": {"description": "Deployment environment"},
    },
    "projectName": {
        "type": "string",
        "defaultValue": "infraforge",
        "metadata": {"description": "Project name for tagging"},
    },
    "ownerEmail": {
        "type": "string",
        "defaultValue": "platform-team@company.com",
        "metadata": {"description": "Owner email for tagging"},
    },
    "costCenter": {
        "type": "string",
        "defaultValue": "IT-0001",
        "metadata": {"description": "Cost center for tagging"},
    },
}

_STANDARD_TAGS = {
    "environment": "[parameters('environment')]",
    "owner": "[parameters('ownerEmail')]",
    "costCenter": "[parameters('costCenter')]",
    "project": "[parameters('projectName')]",
    "managedBy": "InfraForge",
}

_TEMPLATE_WRAPPER = {
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
    "contentVersion": "1.0.0.0",
}


def _make_template(resources: list, extra_params: dict | None = None,
                   outputs: dict | None = None) -> dict:
    """Build a complete ARM template with standard parameters + resources."""
    params = dict(_STANDARD_PARAMETERS)
    if extra_params:
        params.update(extra_params)

    template = dict(_TEMPLATE_WRAPPER)
    template["parameters"] = params
    template["variables"] = {}
    template["resources"] = resources
    template["outputs"] = outputs or {}
    return template


# ──────────────────────────────────────────────────────────────
# BUILT-IN SKELETONS — one per resource type
# ──────────────────────────────────────────────────────────────

_SKELETONS: dict[str, callable] = {}


def _register(resource_type: str):
    """Decorator to register an ARM skeleton generator."""
    def decorator(fn):
        _SKELETONS[resource_type.lower()] = fn
        return fn
    return decorator


# ── Networking ────────────────────────────────────────────────

@_register("Microsoft.Network/virtualNetworks")
def _vnet():
    return _make_template(
        resources=[{
            "type": "Microsoft.Network/virtualNetworks",
            "apiVersion": "2023-09-01",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "properties": {
                "addressSpace": {
                    "addressPrefixes": ["10.0.0.0/16"]
                },
                "subnets": [
                    {
                        "name": "default",
                        "properties": {
                            "addressPrefix": "10.0.1.0/24"
                        }
                    }
                ]
            }
        }],
        extra_params={
            "addressPrefix": {
                "type": "string",
                "defaultValue": "10.0.0.0/16",
                "metadata": {"description": "Address space CIDR for the VNet"},
            },
            "subnetPrefix": {
                "type": "string",
                "defaultValue": "10.0.1.0/24",
                "metadata": {"description": "Default subnet CIDR"},
            },
        },
        outputs={
            "vnetId": {
                "type": "string",
                "value": "[resourceId('Microsoft.Network/virtualNetworks', parameters('resourceName'))]"
            }
        }
    )


@_register("Microsoft.Network/applicationGateways")
def _appgw():
    return _make_template(
        resources=[{
            "type": "Microsoft.Network/applicationGateways",
            "apiVersion": "2023-09-01",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "properties": {
                "sku": {
                    "name": "Standard_v2",
                    "tier": "Standard_v2",
                    "capacity": 1
                },
                "gatewayIPConfigurations": [],
                "frontendIPConfigurations": [],
                "frontendPorts": [],
                "backendAddressPools": [],
                "backendHttpSettingsCollection": [],
                "httpListeners": [],
                "requestRoutingRules": []
            }
        }]
    )


# ── Compute ───────────────────────────────────────────────────

@_register("Microsoft.Web/serverfarms")
def _app_service_plan():
    return _make_template(
        resources=[{
            "type": "Microsoft.Web/serverfarms",
            "apiVersion": "2023-12-01",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "sku": {
                "name": "B1",
                "tier": "Basic",
                "size": "B1",
                "capacity": 1
            },
            "kind": "linux",
            "properties": {
                "reserved": True
            }
        }],
        extra_params={
            "skuName": {
                "type": "string",
                "defaultValue": "B1",
                "allowedValues": ["B1", "B2", "B3", "S1", "S2", "S3", "P1v3", "P2v3"],
                "metadata": {"description": "App Service Plan SKU"},
            },
        },
        outputs={
            "planId": {
                "type": "string",
                "value": "[resourceId('Microsoft.Web/serverfarms', parameters('resourceName'))]"
            }
        }
    )


@_register("Microsoft.Web/sites")
def _app_service():
    return _make_template(
        resources=[{
            "type": "Microsoft.Web/sites",
            "apiVersion": "2023-12-01",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "kind": "app,linux",
            "identity": {
                "type": "SystemAssigned"
            },
            "properties": {
                "httpsOnly": True,
                "siteConfig": {
                    "minTlsVersion": "1.2",
                    "ftpsState": "Disabled",
                    "remoteDebuggingEnabled": False,
                    "http20Enabled": True
                }
            }
        }],
        outputs={
            "defaultHostname": {
                "type": "string",
                "value": "[reference(parameters('resourceName')).defaultHostName]"
            }
        }
    )


@_register("Microsoft.ContainerInstance/containerGroups")
def _aci():
    return _make_template(
        resources=[{
            "type": "Microsoft.ContainerInstance/containerGroups",
            "apiVersion": "2023-05-01",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "properties": {
                "containers": [
                    {
                        "name": "[parameters('resourceName')]",
                        "properties": {
                            "image": "mcr.microsoft.com/hello-world",
                            "resources": {
                                "requests": {
                                    "cpu": 1,
                                    "memoryInGB": 1.5
                                }
                            },
                            "ports": [{"port": 80, "protocol": "TCP"}]
                        }
                    }
                ],
                "osType": "Linux",
                "restartPolicy": "OnFailure",
                "ipAddress": {
                    "type": "Private",
                    "ports": [{"port": 80, "protocol": "TCP"}]
                }
            }
        }]
    )


@_register("Microsoft.App/containerApps")
def _container_apps():
    return _make_template(
        resources=[{
            "type": "Microsoft.App/containerApps",
            "apiVersion": "2024-03-01",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "identity": {
                "type": "SystemAssigned"
            },
            "properties": {
                "configuration": {
                    "ingress": {
                        "external": False,
                        "targetPort": 80,
                        "transport": "http"
                    }
                },
                "template": {
                    "containers": [
                        {
                            "name": "[parameters('resourceName')]",
                            "image": "mcr.microsoft.com/hello-world",
                            "resources": {
                                "cpu": 0.5,
                                "memory": "1Gi"
                            }
                        }
                    ],
                    "scale": {
                        "minReplicas": 0,
                        "maxReplicas": 3
                    }
                }
            }
        }]
    )


@_register("Microsoft.ContainerService/managedClusters")
def _aks():
    return _make_template(
        resources=[{
            "type": "Microsoft.ContainerService/managedClusters",
            "apiVersion": "2024-01-01",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "identity": {
                "type": "SystemAssigned"
            },
            "properties": {
                "dnsPrefix": "[parameters('resourceName')]",
                "agentPoolProfiles": [
                    {
                        "name": "default",
                        "count": 1,
                        "vmSize": "Standard_DS2_v2",
                        "mode": "System",
                        "osType": "Linux"
                    }
                ],
                "networkProfile": {
                    "networkPlugin": "azure",
                    "networkPolicy": "calico"
                },
                "addonProfiles": {
                    "azurePolicy": {"enabled": True}
                }
            }
        }],
        extra_params={
            "nodeCount": {
                "type": "int",
                "defaultValue": 1,
                "minValue": 1,
                "maxValue": 10,
                "metadata": {"description": "Number of nodes in the default pool"},
            },
            "vmSize": {
                "type": "string",
                "defaultValue": "Standard_DS2_v2",
                "metadata": {"description": "VM size for cluster nodes"},
            },
        }
    )


@_register("Microsoft.Compute/virtualMachines")
def _vm():
    return _make_template(
        resources=[{
            "type": "Microsoft.Compute/virtualMachines",
            "apiVersion": "2024-03-01",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "identity": {
                "type": "SystemAssigned"
            },
            "properties": {
                "hardwareProfile": {
                    "vmSize": "Standard_DS2_v2"
                },
                "osProfile": {
                    "computerName": "[parameters('resourceName')]",
                    "adminUsername": "azureuser",
                    "linuxConfiguration": {
                        "disablePasswordAuthentication": True,
                        "patchSettings": {
                            "patchMode": "AutomaticByPlatform"
                        }
                    }
                },
                "storageProfile": {
                    "imageReference": {
                        "publisher": "Canonical",
                        "offer": "ubuntu-24_04-lts",
                        "sku": "server",
                        "version": "latest"
                    },
                    "osDisk": {
                        "createOption": "FromImage",
                        "managedDisk": {
                            "storageAccountType": "Premium_LRS"
                        }
                    }
                },
                "networkProfile": {
                    "networkInterfaces": []
                }
            }
        }],
        extra_params={
            "vmSize": {
                "type": "string",
                "defaultValue": "Standard_DS2_v2",
                "metadata": {"description": "VM size"},
            },
        }
    )


# ── Databases ─────────────────────────────────────────────────

@_register("Microsoft.Sql/servers")
def _sql_server():
    return _make_template(
        resources=[{
            "type": "Microsoft.Sql/servers",
            "apiVersion": "2023-08-01-preview",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "identity": {
                "type": "SystemAssigned"
            },
            "properties": {
                "minimalTlsVersion": "1.2",
                "publicNetworkAccess": "Disabled",
                "administrators": {
                    "azureADOnlyAuthentication": True,
                    "administratorType": "ActiveDirectory",
                    "principalType": "Group",
                    "login": "SQL Admins",
                    "sid": "00000000-0000-0000-0000-000000000000",
                    "tenantId": "[subscription().tenantId]"
                }
            }
        }],
        outputs={
            "serverId": {
                "type": "string",
                "value": "[resourceId('Microsoft.Sql/servers', parameters('resourceName'))]"
            },
            "fqdn": {
                "type": "string",
                "value": "[reference(parameters('resourceName')).fullyQualifiedDomainName]"
            }
        }
    )


@_register("Microsoft.Sql/servers/databases")
def _sql_db():
    return _make_template(
        resources=[{
            "type": "Microsoft.Sql/servers/databases",
            "apiVersion": "2023-08-01-preview",
            "name": "[format('{0}/{1}', parameters('serverName'), parameters('resourceName'))]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "sku": {
                "name": "GP_S_Gen5_1",
                "tier": "GeneralPurpose"
            },
            "properties": {
                "collation": "SQL_Latin1_General_CP1_CI_AS",
                "maxSizeBytes": 34359738368,
                "zoneRedundant": False,
                "requestedBackupStorageRedundancy": "Local"
            }
        }],
        extra_params={
            "serverName": {
                "type": "string",
                "metadata": {"description": "Name of the parent SQL Server"},
            },
        }
    )


@_register("Microsoft.DBforPostgreSQL/flexibleServers")
def _pg_flex():
    return _make_template(
        resources=[{
            "type": "Microsoft.DBforPostgreSQL/flexibleServers",
            "apiVersion": "2023-12-01-preview",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "sku": {
                "name": "Standard_B1ms",
                "tier": "Burstable"
            },
            "properties": {
                "version": "16",
                "storage": {
                    "storageSizeGB": 32
                },
                "backup": {
                    "backupRetentionDays": 7,
                    "geoRedundantBackup": "Disabled"
                },
                "highAvailability": {
                    "mode": "Disabled"
                },
                "authConfig": {
                    "activeDirectoryAuth": "Enabled",
                    "passwordAuth": "Disabled"
                }
            }
        }]
    )


@_register("Microsoft.DocumentDB/databaseAccounts")
def _cosmos():
    return _make_template(
        resources=[{
            "type": "Microsoft.DocumentDB/databaseAccounts",
            "apiVersion": "2024-02-15-preview",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "kind": "GlobalDocumentDB",
            "identity": {
                "type": "SystemAssigned"
            },
            "properties": {
                "databaseAccountOfferType": "Standard",
                "consistencyPolicy": {
                    "defaultConsistencyLevel": "Session"
                },
                "locations": [
                    {
                        "locationName": "[parameters('location')]",
                        "failoverPriority": 0
                    }
                ],
                "publicNetworkAccess": "Disabled",
                "minimalTlsVersion": "Tls12",
                "disableLocalAuth": True
            }
        }]
    )


@_register("Microsoft.Cache/Redis")
def _redis():
    return _make_template(
        resources=[{
            "type": "Microsoft.Cache/Redis",
            "apiVersion": "2023-08-01",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "properties": {
                "sku": {
                    "name": "Basic",
                    "family": "C",
                    "capacity": 0
                },
                "enableNonSslPort": False,
                "minimumTlsVersion": "1.2",
                "publicNetworkAccess": "Disabled",
                "redisConfiguration": {}
            }
        }]
    )


# ── Security & Identity ──────────────────────────────────────

@_register("Microsoft.KeyVault/vaults")
def _keyvault():
    return _make_template(
        resources=[{
            "type": "Microsoft.KeyVault/vaults",
            "apiVersion": "2023-07-01",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "properties": {
                "tenantId": "[subscription().tenantId]",
                "sku": {
                    "family": "A",
                    "name": "standard"
                },
                "enableRbacAuthorization": True,
                "enableSoftDelete": True,
                "softDeleteRetentionInDays": 90,
                "enablePurgeProtection": True,
                "publicNetworkAccess": "Disabled",
                "networkAcls": {
                    "defaultAction": "Deny",
                    "bypass": "AzureServices"
                }
            }
        }]
    )


@_register("Microsoft.ManagedIdentity/userAssignedIdentities")
def _managed_identity():
    return _make_template(
        resources=[{
            "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
            "apiVersion": "2023-01-31",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
        }],
        outputs={
            "principalId": {
                "type": "string",
                "value": "[reference(parameters('resourceName')).principalId]"
            },
            "clientId": {
                "type": "string",
                "value": "[reference(parameters('resourceName')).clientId]"
            }
        }
    )


# ── Storage ───────────────────────────────────────────────────

@_register("Microsoft.Storage/storageAccounts")
def _storage():
    return _make_template(
        resources=[{
            "type": "Microsoft.Storage/storageAccounts",
            "apiVersion": "2023-05-01",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "kind": "StorageV2",
            "sku": {
                "name": "Standard_LRS"
            },
            "properties": {
                "supportsHttpsTrafficOnly": True,
                "minimumTlsVersion": "TLS1_2",
                "allowBlobPublicAccess": False,
                "publicNetworkAccess": "Disabled",
                "encryption": {
                    "services": {
                        "blob": {"enabled": True, "keyType": "Account"},
                        "file": {"enabled": True, "keyType": "Account"}
                    },
                    "keySource": "Microsoft.Storage"
                },
                "networkAcls": {
                    "defaultAction": "Deny",
                    "bypass": "AzureServices"
                }
            }
        }],
        extra_params={
            "skuName": {
                "type": "string",
                "defaultValue": "Standard_LRS",
                "allowedValues": ["Standard_LRS", "Standard_GRS", "Standard_ZRS",
                                  "Premium_LRS", "Premium_ZRS"],
                "metadata": {"description": "Storage account SKU"},
            }
        }
    )


# ── Monitoring ────────────────────────────────────────────────

@_register("Microsoft.OperationalInsights/workspaces")
def _log_analytics():
    return _make_template(
        resources=[{
            "type": "Microsoft.OperationalInsights/workspaces",
            "apiVersion": "2023-09-01",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "properties": {
                "sku": {
                    "name": "PerGB2018"
                },
                "retentionInDays": 30,
                "publicNetworkAccessForIngestion": "Enabled",
                "publicNetworkAccessForQuery": "Enabled"
            }
        }],
        outputs={
            "workspaceId": {
                "type": "string",
                "value": "[reference(parameters('resourceName')).customerId]"
            }
        }
    )


@_register("Microsoft.Insights/components")
def _app_insights():
    return _make_template(
        resources=[{
            "type": "Microsoft.Insights/components",
            "apiVersion": "2020-02-02",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "kind": "web",
            "properties": {
                "Application_Type": "web",
                "RetentionInDays": 90,
                "publicNetworkAccessForIngestion": "Enabled",
                "publicNetworkAccessForQuery": "Enabled"
            }
        }],
        outputs={
            "instrumentationKey": {
                "type": "string",
                "value": "[reference(parameters('resourceName')).InstrumentationKey]"
            },
            "connectionString": {
                "type": "string",
                "value": "[reference(parameters('resourceName')).ConnectionString]"
            }
        }
    )


# ── AI ────────────────────────────────────────────────────────

@_register("Microsoft.CognitiveServices/accounts")
def _cognitive():
    return _make_template(
        resources=[{
            "type": "Microsoft.CognitiveServices/accounts",
            "apiVersion": "2024-04-01-preview",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "kind": "CognitiveServices",
            "identity": {
                "type": "SystemAssigned"
            },
            "sku": {
                "name": "S0"
            },
            "properties": {
                "publicNetworkAccess": "Disabled",
                "disableLocalAuth": True,
                "customSubDomainName": "[parameters('resourceName')]",
                "networkAcls": {
                    "defaultAction": "Deny"
                }
            }
        }]
    )


@_register("Microsoft.MachineLearningServices/workspaces")
def _ml_workspace():
    return _make_template(
        resources=[{
            "type": "Microsoft.MachineLearningServices/workspaces",
            "apiVersion": "2024-04-01",
            "name": "[parameters('resourceName')]",
            "location": "[parameters('location')]",
            "tags": _STANDARD_TAGS,
            "identity": {
                "type": "SystemAssigned"
            },
            "properties": {
                "friendlyName": "[parameters('resourceName')]",
                "publicNetworkAccess": "Disabled"
            }
        }]
    )


# ══════════════════════════════════════════════════════════════
# GENERATOR API
# ══════════════════════════════════════════════════════════════

def get_supported_resource_types() -> list[str]:
    """Return the list of resource types with built-in skeletons."""
    return sorted(_SKELETONS.keys(), key=str.lower)


def has_builtin_skeleton(resource_type: str) -> bool:
    """Check if a built-in ARM skeleton exists for this resource type."""
    return resource_type.lower() in _SKELETONS


def generate_arm_template(resource_type: str) -> Optional[dict]:
    """Generate a basic ARM template for the given Azure resource type.

    Returns the template as a dict, or None if no built-in skeleton exists.
    For unknown types, use generate_arm_template_with_copilot().
    """
    gen_fn = _SKELETONS.get(resource_type.lower())
    if gen_fn is None:
        return None

    template = gen_fn()
    logger.info(
        f"Generated ARM template for {resource_type}: "
        f"{len(template.get('resources', []))} resource(s), "
        f"{len(template.get('parameters', {}))} parameter(s)"
    )
    return template


def generate_arm_template_json(resource_type: str) -> Optional[str]:
    """Generate and return as a formatted JSON string."""
    template = generate_arm_template(resource_type)
    if template is None:
        return None
    return json.dumps(template, indent=2)


async def generate_arm_template_with_copilot(
    resource_type: str,
    service_name: str,
    copilot_client,
    model: str = "claude-sonnet-4",
) -> str:
    """Use the Copilot SDK to generate an ARM template for an unknown resource type.

    This is the fallback when no built-in skeleton exists.
    Returns the ARM template as a JSON string.
    """
    import asyncio

    prompt = (
        f"Generate a minimal ARM template (JSON) for deploying the Azure resource type "
        f"'{resource_type}' (service: {service_name}).\n\n"
        "Requirements:\n"
        "- Include standard parameters: resourceName (string), location (string, default "
        "\"[resourceGroup().location]\"), environment (string, default \"dev\"), "
        "projectName (string, default \"infraforge\"), ownerEmail (string), costCenter (string)\n"
        "- Include tags: environment, owner, costCenter, project, managedBy=InfraForge\n"
        "- Use a recent stable API version\n"
        "- Include minimal required properties only\n"
        "- Enable managed identity (SystemAssigned) if the resource supports it\n"
        "- Set httpsOnly/minTlsVersion where applicable\n"
        "- Disable public network access where applicable\n"
        "- Do NOT include diagnostic settings or Log Analytics dependencies\n"
        "- Return ONLY the raw JSON — no markdown fences, no explanation\n"
    )

    session = None
    try:
        session = await copilot_client.create_session({
            "model": model,
            "streaming": True,
            "tools": [],
            "system_message": {
                "content": (
                    "You are an Azure infrastructure expert. "
                    "Generate production-ready ARM templates. "
                    "Return ONLY raw JSON — no markdown, no code fences, no explanation."
                )
            },
        })

        chunks: list[str] = []
        done_ev = asyncio.Event()

        def on_event(ev):
            try:
                if ev.type.value == "assistant.message_delta":
                    chunks.append(ev.data.delta_content or "")
                elif ev.type.value in ("assistant.message", "session.idle"):
                    done_ev.set()
            except Exception:
                done_ev.set()

        unsub = session.on(on_event)
        try:
            await session.send({"prompt": prompt})
            await asyncio.wait_for(done_ev.wait(), timeout=60)
        finally:
            unsub()

        result = "".join(chunks).strip()
        if result.startswith("```"):
            lines = result.split("\n")[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            result = "\n".join(lines).strip()

        # Validate it's valid JSON
        json.loads(result)
        logger.info(f"Copilot generated ARM template for {resource_type}")
        return result

    except json.JSONDecodeError:
        logger.error(f"Copilot returned invalid JSON for {resource_type}")
        raise ValueError(f"Failed to generate valid ARM template for {resource_type}")
    finally:
        if session:
            try:
                await session.destroy()
            except Exception:
                pass
