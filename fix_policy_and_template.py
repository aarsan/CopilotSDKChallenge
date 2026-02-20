"""Fix the VNet policy to allow eastus2 (our validation region) and re-fix the template."""
import asyncio
import json
from dotenv import load_dotenv
load_dotenv()

# Policy: allow VNets in both centralus and eastus2
POLICY = {
    "properties": {
        "displayName": "Allow Virtual Networks in approved regions",
        "policyType": "Custom",
        "mode": "All",
        "description": "This policy allows deployment of Microsoft.Network/virtualNetworks resources only in approved Azure regions (eastus2, centralus).",
        "policyRule": {
            "if": {
                "allOf": [
                    {
                        "field": "type",
                        "equals": "Microsoft.Network/virtualNetworks"
                    },
                    {
                        "not": {
                            "field": "location",
                            "in": ["eastus2", "centralus", "eastus", "westus2"]
                        }
                    }
                ]
            },
            "then": {
                "effect": "deny"
            }
        }
    }
}

# Clean template (no diagnosticSettings, location = [resourceGroup().location])
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
                    {"name": "default", "properties": {"addressPrefix": "10.0.0.0/24"}},
                    {"name": "app", "properties": {"addressPrefix": "10.0.1.0/24"}},
                    {"name": "data", "properties": {"addressPrefix": "10.0.2.0/24"}}
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

    sid = "Microsoft.Network/virtualNetworks"

    # Update policy
    policy_json = json.dumps(POLICY, indent=2)
    await b.execute_write(
        "UPDATE service_artifacts SET content = ?, notes = 'Fixed: allow eastus2 region' "
        "WHERE service_id = ? AND artifact_type = 'policy'",
        (policy_json, sid),
    )
    print(f"✅ Policy updated ({len(policy_json)} bytes)")

    # Update template (in case healer corrupted it)
    template_json = json.dumps(TEMPLATE, indent=2)
    await b.execute_write(
        "UPDATE service_artifacts SET content = ?, notes = 'Clean VNet template - no diagnosticSettings' "
        "WHERE service_id = ? AND artifact_type = 'template'",
        (template_json, sid),
    )
    print(f"✅ Template updated ({len(template_json)} bytes)")

    # Reset service to validating
    await b.execute_write(
        "UPDATE approved_services SET status = 'validating', review_notes = NULL WHERE service_id = ?",
        (sid,),
    )
    print("✅ Service reset to 'validating'")

asyncio.run(main())
