"""Fix the VNet ARM template — remove diagnosticSettings that requires Log Analytics workspace."""
import asyncio
import json
import os
from dotenv import load_dotenv
load_dotenv()

TEMPLATE = {
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {
        "projectName": {
            "type": "string",
            "defaultValue": "myproject",
            "metadata": {"description": "Project name for resource naming and tagging."}
        },
        "environment": {
            "type": "string",
            "allowedValues": ["dev", "test", "stage", "prod"],
            "defaultValue": "dev",
            "metadata": {"description": "Deployment environment."}
        },
        "location": {
            "type": "string",
            "defaultValue": "[resourceGroup().location]",
            "metadata": {"description": "Azure region for deployment."}
        }
    },
    "variables": {
        "vnetName": "[format('{0}-{1}-vnet', parameters('projectName'), parameters('environment'))]"
    },
    "resources": [
        {
            "type": "Microsoft.Network/virtualNetworks",
            "apiVersion": "2023-09-01",
            "name": "[variables('vnetName')]",
            "location": "[parameters('location')]",
            "tags": {
                "project": "[parameters('projectName')]",
                "environment": "[parameters('environment')]",
                "costCenter": "TBD",
                "managedBy": "InfraForge"
            },
            "properties": {
                "addressSpace": {
                    "addressPrefixes": ["10.0.0.0/16"]
                },
                "subnets": [
                    {
                        "name": "default",
                        "properties": {
                            "addressPrefix": "10.0.0.0/24"
                        }
                    },
                    {
                        "name": "app",
                        "properties": {
                            "addressPrefix": "10.0.1.0/24"
                        }
                    },
                    {
                        "name": "data",
                        "properties": {
                            "addressPrefix": "10.0.2.0/24"
                        }
                    }
                ],
                "enableDdosProtection": False
            }
        }
    ],
    "outputs": {
        "vnetName": {"type": "string", "value": "[variables('vnetName')]"},
        "vnetResourceId": {"type": "string", "value": "[resourceId('Microsoft.Network/virtualNetworks', variables('vnetName'))]"}
    }
}


async def main():
    from src.database import init_db, get_backend
    await init_db()
    b = await get_backend()

    template_json = json.dumps(TEMPLATE, indent=2)
    await b.execute_write(
        "UPDATE service_artifacts SET content = ?, notes = 'Fixed: removed diagnosticSettings (requires Log Analytics workspace)' "
        "WHERE service_id = ? AND artifact_type = ?",
        (template_json, "Microsoft.Network/virtualNetworks", "template"),
    )
    print(f"✅ Template updated ({len(template_json)} bytes)")

    # Verify
    rows = await b.fetch_all(
        "SELECT LEN(content) as sz, notes FROM service_artifacts WHERE service_id = ? AND artifact_type = ?",
        ("Microsoft.Network/virtualNetworks", "template"),
    )
    for r in rows:
        print(f"   DB size={r['sz']}, notes={r['notes']}")

asyncio.run(main())
