import os
import json
import requests
from azure.identity import DefaultAzureCredential
from azure.mgmt.resource import ResourceManagementClient

# Environment configuration
SUBSCRIPTION_ID = os.environ.get("AZURE_SUBSCRIPTION_ID")
RESOURCE_GROUP = os.environ.get("TEST_RESOURCE_GROUP", "infraforge-val-67ee172f")
TENANT_ID = os.environ.get("AZURE_TENANT_ID")

# Initialize Azure credential
credential = DefaultAzureCredential()

TEST_MANIFEST = {
    "resources_tested": [
        "ifrg-resourceName_virtualnetworks",
        "ifrg-resourceName_subnets", 
        "ifrg-resourceName_azurefirewalls",
        "ifrg-resourceName_publicipaddresses",
        "ifrg-resourceName_firewallpolicies",
        "ifrg-resourceName_networksecuritygroups",
        "ifrg-resourceName_networksecuritygroups1",
        "ifrg-resourceName_networksecuritygroups2"
    ],
    "categories_covered": ["auth", "provisioning", "api_version", "tags", "security", "network"],
    "checks": [
        {"test": "test_azure_login", "resource": "_infra", "category": "auth", "description": "Authenticate to Azure and acquire token"},
        {"test": "test_resource_group_exists", "resource": "_infra", "category": "provisioning", "description": "Resource group exists and is accessible"},
        {"test": "test_resource_group_has_resources", "resource": "_infra", "category": "provisioning", "description": "Resource group contains deployed resources"},
        {"test": "test_virtualnetwork_main_provisioning", "resource": "ifrg-resourceName_virtualnetworks", "category": "provisioning", "description": "Virtual Network provisioning state is Succeeded"},
        {"test": "test_virtualnetwork_main_api_version", "resource": "ifrg-resourceName_virtualnetworks", "category": "api_version", "description": "Virtual Network API version is valid"},
        {"test": "test_virtualnetwork_main_tags", "resource": "ifrg-resourceName_virtualnetworks", "category": "tags", "description": "Virtual Network has required tags"},
        {"test": "test_virtualnetwork_main_config", "resource": "ifrg-resourceName_virtualnetworks", "category": "network", "description": "Virtual Network address space and subnets configuration"},
        {"test": "test_virtualnetwork_subnets_provisioning", "resource": "ifrg-resourceName_subnets", "category": "provisioning", "description": "Subnets VNet provisioning state is Succeeded"},
        {"test": "test_virtualnetwork_subnets_api_version", "resource": "ifrg-resourceName_subnets", "category": "api_version", "description": "Subnets VNet API version is valid"},
        {"test": "test_virtualnetwork_subnets_tags", "resource": "ifrg-resourceName_subnets", "category": "tags", "description": "Subnets VNet has required tags"},
        {"test": "test_azure_firewall_provisioning", "resource": "ifrg-resourceName_azurefirewalls", "category": "provisioning", "description": "Azure Firewall provisioning state is Succeeded"},
        {"test": "test_azure_firewall_api_version", "resource": "ifrg-resourceName_azurefirewalls", "category": "api_version", "description": "Azure Firewall API version is valid"},
        {"test": "test_azure_firewall_tags", "resource": "ifrg-resourceName_azurefirewalls", "category": "tags", "description": "Azure Firewall has required tags"},
        {"test": "test_azure_firewall_config", "resource": "ifrg-resourceName_azurefirewalls", "category": "security", "description": "Azure Firewall SKU and threat intelligence configuration"},
        {"test": "test_public_ip_provisioning", "resource": "ifrg-resourceName_publicipaddresses", "category": "provisioning", "description": "Public IP provisioning state is Succeeded"},
        {"test": "test_public_ip_api_version", "resource": "ifrg-resourceName_publicipaddresses", "category": "api_version", "description": "Public IP API version is valid"},
        {"test": "test_public_ip_tags", "resource": "ifrg-resourceName_publicipaddresses", "category": "tags", "description": "Public IP has required tags"},
        {"test": "test_public_ip_config", "resource": "ifrg-resourceName_publicipaddresses", "category": "network", "description": "Public IP allocation method and version configuration"},
        {"test": "test_firewall_policy_provisioning", "resource": "ifrg-resourceName_firewallpolicies", "category": "provisioning", "description": "Firewall Policy provisioning state is Succeeded"},
        {"test": "test_firewall_policy_api_version", "resource": "ifrg-resourceName_firewallpolicies", "category": "api_version", "description": "Firewall Policy API version is valid"},
        {"test": "test_firewall_policy_tags", "resource": "ifrg-resourceName_firewallpolicies", "category": "tags", "description": "Firewall Policy has required tags"},
        {"test": "test_firewall_policy_config", "resource": "ifrg-resourceName_firewallpolicies", "category": "security", "description": "Firewall Policy SKU and threat intelligence configuration"},
        {"test": "test_nsg_main_provisioning", "resource": "ifrg-resourceName_networksecuritygroups", "category": "provisioning", "description": "Main NSG provisioning state is Succeeded"},
        {"test": "test_nsg_main_api_version", "resource": "ifrg-resourceName_networksecuritygroups", "category": "api_version", "description": "Main NSG API version is valid"},
        {"test": "test_nsg_main_tags", "resource": "ifrg-resourceName_networksecuritygroups", "category": "tags", "description": "Main NSG has required tags"},
        {"test": "test_nsg_main_security_rules", "resource": "ifrg-resourceName_networksecuritygroups", "category": "security", "description": "Main NSG security rules configuration"},
        {"test": "test_nsg1_provisioning", "resource": "ifrg-resourceName_networksecuritygroups1", "category": "provisioning", "description": "NSG1 provisioning state is Succeeded"},
        {"test": "test_nsg1_api_version", "resource": "ifrg-resourceName_networksecuritygroups1", "category": "api_version", "description": "NSG1 API version is valid"},
        {"test": "test_nsg2_provisioning", "resource": "ifrg-resourceName_networksecuritygroups2", "category": "provisioning", "description": "NSG2 provisioning state is Succeeded"},
        {"test": "test_nsg2_api_version", "resource": "ifrg-resourceName_networksecuritygroups2", "category": "api_version", "description": "NSG2 API version is valid"}
    ]
}

def test_azure_login():
    """Verify we can authenticate to Azure and acquire a management token."""
    token = credential.get_token("https://management.azure.com/.default")
    assert token.token, "Failed to acquire Azure token — DefaultAzureCredential not configured"
    print(f"AUTH OK — token acquired (expires {token.expires_on})")

def test_resource_group_exists():
    """Verify the target resource group exists and is in Succeeded state."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    rg = client.resource_groups.get(RESOURCE_GROUP)
    assert rg.properties.provisioning_state == "Succeeded", \
        f"Resource group state: {rg.properties.provisioning_state}"
    print(f"RG OK — {RESOURCE_GROUP} exists in {rg.location}")

def test_resource_group_has_resources():
    """Verify the resource group contains deployed resources."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resources = list(client.resources.list_by_resource_group(RESOURCE_GROUP))
    assert len(resources) > 0, f"Resource group {RESOURCE_GROUP} is empty"
    print(f"RESOURCES OK — {len(resources)} resources found: {[r.name for r in resources[:10]]}")

def test_virtualnetwork_main_provisioning():
    """Verify main virtual network provisioning state is Succeeded."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/virtualNetworks/ifrg-resourceName_virtualnetworks"
    resource = client.resources.get_by_id(resource_id, "2023-09-01")
    assert resource.properties["provisioningState"] == "Succeeded", \
        f"VNet provisioning state: {resource.properties['provisioningState']}"
    print(f"VNET MAIN OK — provisioning state: {resource.properties['provisioningState']}")

def test_virtualnetwork_main_api_version():
    """Verify main virtual network API version is valid."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    provider = client.providers.get("Microsoft.Network")
    vnet_resource_type = None
    for rt in provider.resource_types:
        if rt.resource_type == "virtualNetworks":
            vnet_resource_type = rt
            break
    assert vnet_resource_type is not None, "Microsoft.Network/virtualNetworks resource type not found"
    valid_versions = vnet_resource_type.api_versions
    template_version = "2023-09-01"
    assert template_version in valid_versions, f"API version {template_version} not in valid versions: {valid_versions[:5]}"
    print(f"VNET MAIN API OK — version {template_version} is valid")

def test_virtualnetwork_main_tags():
    """Verify main virtual network has required tags."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/virtualNetworks/ifrg-resourceName_virtualnetworks"
    resource = client.resources.get_by_id(resource_id, "2023-09-01")
    tags = resource.tags or {}
    required_tags = ["environment", "owner", "costCenter"]
    for tag in required_tags:
        assert tag in tags, f"Required tag '{tag}' missing from VNet main"
    print(f"VNET MAIN TAGS OK — has required tags: {required_tags}")

def test_virtualnetwork_main_config():
    """Verify main virtual network address space and subnets configuration."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/virtualNetworks/ifrg-resourceName_virtualnetworks"
    resource = client.resources.get_by_id(resource_id, "2023-09-01")
    
    address_space = resource.properties.get("addressSpace", {})
    assert "addressPrefixes" in address_space, "Address space prefixes missing"
    assert len(address_space["addressPrefixes"]) > 0, "No address prefixes configured"
    
    subnets = resource.properties.get("subnets", [])
    assert len(subnets) > 0, "No subnets configured"
    print(f"VNET MAIN CONFIG OK — address spaces: {address_space['addressPrefixes']}, subnets: {len(subnets)}")

def test_virtualnetwork_subnets_provisioning():
    """Verify subnets virtual network provisioning state is Succeeded."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/virtualNetworks/ifrg-resourceName_subnets"
    resource = client.resources.get_by_id(resource_id, "2023-09-01")
    assert resource.properties["provisioningState"] == "Succeeded", \
        f"VNet subnets provisioning state: {resource.properties['provisioningState']}"
    print(f"VNET SUBNETS OK — provisioning state: {resource.properties['provisioningState']}")

def test_virtualnetwork_subnets_api_version():
    """Verify subnets virtual network API version is valid."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    provider = client.providers.get("Microsoft.Network")
    vnet_resource_type = None
    for rt in provider.resource_types:
        if rt.resource_type == "virtualNetworks":
            vnet_resource_type = rt
            break
    assert vnet_resource_type is not None, "Microsoft.Network/virtualNetworks resource type not found"
    valid_versions = vnet_resource_type.api_versions
    template_version = "2023-09-01"
    assert template_version in valid_versions, f"API version {template_version} not in valid versions: {valid_versions[:5]}"
    print(f"VNET SUBNETS API OK — version {template_version} is valid")

def test_virtualnetwork_subnets_tags():
    """Verify subnets virtual network has required tags."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/virtualNetworks/ifrg-resourceName_subnets"
    resource = client.resources.get_by_id(resource_id, "2023-09-01")
    tags = resource.tags or {}
    required_tags = ["environment", "owner", "costCenter"]
    for tag in required_tags:
        assert tag in tags, f"Required tag '{tag}' missing from VNet subnets"
    print(f"VNET SUBNETS TAGS OK — has required tags: {required_tags}")

def test_azure_firewall_provisioning():
    """Verify Azure Firewall provisioning state is Succeeded."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/azureFirewalls/ifrg-resourceName_azurefirewalls"
    resource = client.resources.get_by_id(resource_id, "2023-09-01")
    assert resource.properties["provisioningState"] == "Succeeded", \
        f"Azure Firewall provisioning state: {resource.properties['provisioningState']}"
    print(f"FIREWALL OK — provisioning state: {resource.properties['provisioningState']}")

def test_azure_firewall_api_version():
    """Verify Azure Firewall API version is valid."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    provider = client.providers.get("Microsoft.Network")
    firewall_resource_type = None
    for rt in provider.resource_types:
        if rt.resource_type == "azureFirewalls":
            firewall_resource_type = rt
            break
    assert firewall_resource_type is not None, "Microsoft.Network/azureFirewalls resource type not found"
    valid_versions = firewall_resource_type.api_versions
    template_version = "2023-09-01"
    assert template_version in valid_versions, f"API version {template_version} not in valid versions: {valid_versions[:5]}"
    print(f"FIREWALL API OK — version {template_version} is valid")

def test_azure_firewall_tags():
    """Verify Azure Firewall has required tags."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/azureFirewalls/ifrg-resourceName_azurefirewalls"
    resource = client.resources.get_by_id(resource_id, "2023-09-01")
    tags = resource.tags or {}
    required_tags = ["environment", "owner", "costCenter"]
    for tag in required_tags:
        assert tag in tags, f"Required tag '{tag}' missing from Azure Firewall"
    print(f"FIREWALL TAGS OK — has required tags: {required_tags}")

def test_azure_firewall_config():
    """Verify Azure Firewall SKU and threat intelligence configuration."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/azureFirewalls/ifrg-resourceName_azurefirewalls"
    resource = client.resources.get_by_id(resource_id, "2023-09-01")
    
    sku = resource.properties.get("sku", {})
    assert sku.get("name") == "AZFW_VNet", f"Expected SKU name AZFW_VNet, got {sku.get('name')}"
    assert sku.get("tier") == "Standard", f"Expected SKU tier Standard, got {sku.get('tier')}"
    
    threat_intel_mode = resource.properties.get("threatIntelMode")
    assert threat_intel_mode == "Alert", f"Expected threatIntelMode Alert, got {threat_intel_mode}"
    print(f"FIREWALL CONFIG OK — SKU: {sku['name']}/{sku['tier']}, ThreatIntel: {threat_intel_mode}")

def test_public_ip_provisioning():
    """Verify Public IP provisioning state is Succeeded."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/publicIPAddresses/ifrg-resourceName_publicipaddresses"
    resource = client.resources.get_by_id(resource_id, "2023-09-01")
    assert resource.properties["provisioningState"] == "Succeeded", \
        f"Public IP provisioning state: {resource.properties['provisioningState']}"
    print(f"PUBLIC IP OK — provisioning state: {resource.properties['provisioningState']}")

def test_public_ip_api_version():
    """Verify Public IP API version is valid."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    provider = client.providers.get("Microsoft.Network")
    pip_resource_type = None
    for rt in provider.resource_types:
        if rt.resource_type == "publicIPAddresses":
            pip_resource_type = rt
            break
    assert pip_resource_type is not None, "Microsoft.Network/publicIPAddresses resource type not found"
    valid_versions = pip_resource_type.api_versions
    template_version = "2023-09-01"
    assert template_version in valid_versions, f"API version {template_version} not in valid versions: {valid_versions[:5]}"
    print(f"PUBLIC IP API OK — version {template_version} is valid")

def test_public_ip_tags():
    """Verify Public IP has required tags."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/publicIPAddresses/ifrg-resourceName_publicipaddresses"
    resource = client.resources.get_by_id(resource_id, "2023-09-01")
    tags = resource.tags or {}
    required_tags = ["environment", "owner", "costCenter"]
    for tag in required_tags:
        assert tag in tags, f"Required tag '{tag}' missing from Public IP"
    print(f"PUBLIC IP TAGS OK — has required tags: {required_tags}")

def test_public_ip_config():
    """Verify Public IP allocation method and version configuration."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/publicIPAddresses/ifrg-resourceName_publicipaddresses"
    resource = client.resources.get_by_id(resource_id, "2023-09-01")
    
    allocation_method = resource.properties.get("publicIPAllocationMethod")
    assert allocation_method == "Static", f"Expected Static allocation, got {allocation_method}"
    
    ip_version = resource.properties.get("publicIPAddressVersion")
    assert ip_version == "IPv4", f"Expected IPv4, got {ip_version}"
    print(f"PUBLIC IP CONFIG OK — allocation: {allocation_method}, version: {ip_version}")

def test_firewall_policy_provisioning():
    """Verify Firewall Policy provisioning state is Succeeded."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/firewallPolicies/ifrg-resourceName_firewallpolicies"
    resource = client.resources.get_by_id(resource_id, "2021-05-01")
    assert resource.properties["provisioningState"] == "Succeeded", \
        f"Firewall Policy provisioning state: {resource.properties['provisioningState']}"
    print(f"FIREWALL POLICY OK — provisioning state: {resource.properties['provisioningState']}")

def test_firewall_policy_api_version():
    """Verify Firewall Policy API version is valid."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    provider = client.providers.get("Microsoft.Network")
    policy_resource_type = None
    for rt in provider.resource_types:
        if rt.resource_type == "firewallPolicies":
            policy_resource_type = rt
            break
    assert policy_resource_type is not None, "Microsoft.Network/firewallPolicies resource type not found"
    valid_versions = policy_resource_type.api_versions
    template_version = "2021-05-01"
    assert template_version in valid_versions, f"API version {template_version} not in valid versions: {valid_versions[:5]}"
    print(f"FIREWALL POLICY API OK — version {template_version} is valid")

def test_firewall_policy_tags():
    """Verify Firewall Policy has required tags."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/firewallPolicies/ifrg-resourceName_firewallpolicies"
    resource = client.resources.get_by_id(resource_id, "2021-05-01")
    tags = resource.tags or {}
    required_tags = ["environment", "owner", "costCenter"]
    for tag in required_tags:
        assert tag in tags, f"Required tag '{tag}' missing from Firewall Policy"
    print(f"FIREWALL POLICY TAGS OK — has required tags: {required_tags}")

def test_firewall_policy_config():
    """Verify Firewall Policy SKU and threat intelligence configuration."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/firewallPolicies/ifrg-resourceName_firewallpolicies"
    resource = client.resources.get_by_id(resource_id, "2021-05-01")
    
    sku = resource.properties.get("sku", {})
    assert sku.get("tier") == "Standard", f"Expected SKU tier Standard, got {sku.get('tier')}"
    
    threat_intel_mode = resource.properties.get("threatIntelMode")
    assert threat_intel_mode == "Alert", f"Expected threatIntelMode Alert, got {threat_intel_mode}"
    print(f"FIREWALL POLICY CONFIG OK — SKU tier: {sku['tier']}, ThreatIntel: {threat_intel_mode}")

def test_nsg_main_provisioning():
    """Verify main Network Security Group provisioning state is Succeeded."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/networkSecurityGroups/ifrg-resourceName_networksecuritygroups"
    resource = client.resources.get_by_id(resource_id, "2023-09-01")
    assert resource.properties["provisioningState"] == "Succeeded", \
        f"NSG main provisioning state: {resource.properties['provisioningState']}"
    print(f"NSG MAIN OK — provisioning state: {resource.properties['provisioningState']}")

def test_nsg_main_api_version():
    """Verify main Network Security Group API version is valid."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    provider = client.providers.get("Microsoft.Network")
    nsg_resource_type = None
    for rt in provider.resource_types:
        if rt.resource_type == "networkSecurityGroups":
            nsg_resource_type = rt
            break
    assert nsg_resource_type is not None, "Microsoft.Network/networkSecurityGroups resource type not found"
    valid_versions = nsg_resource_type.api_versions
    template_version = "2023-09-01"
    assert template_version in valid_versions, f"API version {template_version} not in valid versions: {valid_versions[:5]}"
    print(f"NSG MAIN API OK — version {template_version} is valid")

def test_nsg_main_tags():
    """Verify main Network Security Group has required tags."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/networkSecurityGroups/ifrg-resourceName_networksecuritygroups"
    resource = client.resources.get_by_id(resource_id, "2023-09-01")
    tags = resource.tags or {}
    required_tags = ["environment", "owner", "costCenter"]
    for tag in required_tags:
        assert tag in tags, f"Required tag '{tag}' missing from NSG main"
    print(f"NSG MAIN TAGS OK — has required tags: {required_tags}")

def test_nsg_main_security_rules():
    """Verify main Network Security Group security rules configuration."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/networkSecurityGroups/ifrg-resourceName_networksecuritygroups"
    resource = client.resources.get_by_id(resource_id, "2023-09-01")
    
    security_rules = resource.properties.get("securityRules", [])
    assert len(security_rules) > 0, "No security rules configured"
    
    # Check for the DenyAllInbound rule from template
    deny_rule = None
    for rule in security_rules:
        if rule.get("name") == "DenyAllInbound":
            deny_rule = rule
            break
    
    assert deny_rule is not None, "DenyAllInbound rule not found"
    rule_props = deny_rule.get("properties", {})
    assert rule_props.get("access") == "Deny", "DenyAllInbound rule should have Deny access"
    assert rule_props.get("direction") == "Inbound", "DenyAllInbound rule should be Inbound"
    print(f"NSG MAIN RULES OK — {len(security_rules)} rules, DenyAllInbound rule present")

def test_nsg1_provisioning():
    """Verify Network Security Group 1 provisioning state is Succeeded."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/networkSecurityGroups/ifrg-resourceName_networksecuritygroups1"
    resource = client.resources.get_by_id(resource_id, "2023-09-01")
    assert resource.properties["provisioningState"] == "Succeeded", \
        f"NSG1 provisioning state: {resource.properties['provisioningState']}"
    print(f"NSG1 OK — provisioning state: {resource.properties['provisioningState']}")

def test_nsg1_api_version():
    """Verify Network Security Group 1 API version is valid."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    provider = client.providers.get("Microsoft.Network")
    nsg_resource_type = None
    for rt in provider.resource_types:
        if rt.resource_type == "networkSecurityGroups":
            nsg_resource_type = rt
            break
    assert nsg_resource_type is not None, "Microsoft.Network/networkSecurityGroups resource type not found"
    valid_versions = nsg_resource_type.api_versions
    template_version = "2023-09-01"
    assert template_version in valid_versions, f"API version {template_version} not in valid versions: {valid_versions[:5]}"
    print(f"NSG1 API OK — version {template_version} is valid")

def test_nsg2_provisioning():
    """Verify Network Security Group 2 provisioning state is Succeeded."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    resource_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/networkSecurityGroups/ifrg-resourceName_networksecuritygroups2"
    resource = client.resources.get_by_id(resource_id, "2023-09-01")
    assert resource.properties["provisioningState"] == "Succeeded", \
        f"NSG2 provisioning state: {resource.properties['provisioningState']}"
    print(f"NSG2 OK — provisioning state: {resource.properties['provisioningState']}")

def test_nsg2_api_version():
    """Verify Network Security Group 2 API version is valid."""
    client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    provider = client.providers.get("Microsoft.Network")
    nsg_resource_type = None
    for rt in provider.resource_types:
        if rt.resource_type == "networkSecurityGroups":
            nsg_resource_type = rt
            break
    assert nsg_resource_type is not None, "Microsoft.Network/networkSecurityGroups resource type not found"
    valid_versions = nsg_resource_type.api_versions
    template_version = "2023-09-01"
    assert template_version in valid_versions, f"API version {template_version} not in valid versions: {valid_versions[:5]}"
    print(f"NSG2 API OK — version {template_version} is valid")

if __name__ == "__main__":
    import traceback
    
    test_functions = [
        test_azure_login,
        test_resource_group_exists,
        test_resource_group_has_resources,
        test_virtualnetwork_main_provisioning,
        test_virtualnetwork_main_api_version,
        test_virtualnetwork_main_tags,
        test_virtualnetwork_main_config,
        test_virtualnetwork_subnets_provisioning,
        test_virtualnetwork_subnets_api_version,
        test_virtualnetwork_subnets_tags,
        test_azure_firewall_provisioning,
        test_azure_firewall_api_version,
        test_azure_firewall_tags,
        test_azure_firewall_config,
        test_public_ip_provisioning,
        test_public_ip_api_version,
        test_public_ip_tags,
        test_public_ip_config,
        test_firewall_policy_provisioning,
        test_firewall_policy_api_version,
        test_firewall_policy_tags,
        test_firewall_policy_config,
        test_nsg_main_provisioning,
        test_nsg_main_api_version,
        test_nsg_main_tags,
        test_nsg_main_security_rules,
        test_nsg1_provisioning,
        test_nsg1_api_version,
        test_nsg2_provisioning,
        test_nsg2_api_version
    ]
    
    passed = 0
    failed = 0
    
    print(f"Running {len(test_functions)} infrastructure tests...\n")
    
    for test_func in test_functions:
        try:
            print(f"Running {test_func.__name__}...")
            test_func()
            passed += 1
            print(f"✓ PASS: {test_func.__name__}\n")
        except Exception as e:
            failed += 1
            print(f"✗ FAIL: {test_func.__name__}")
            print(f"Error: {str(e)}")
            print(f"Traceback: {traceback.format_exc()}\n")
    
    print(f"Test Results: {passed} passed, {failed} failed")
    
    if failed > 0:
        exit(1)
    else:
        print("All tests passed!")