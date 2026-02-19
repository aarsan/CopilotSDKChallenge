"""
Architecture diagram generator tool.
Generates Mermaid diagrams from infrastructure descriptions for stakeholder review.
"""

from pydantic import BaseModel, Field
from copilot import define_tool


# ── Mermaid styling for Azure resource types ──────────────────
RESOURCE_SHAPES = {
    # Compute
    "app_service": ("🌐", "App Service"),
    "function_app": ("⚡", "Function App"),
    "container_app": ("📦", "Container App"),
    "aks": ("☸️", "AKS Cluster"),
    "vm": ("🖥️", "Virtual Machine"),
    "vmss": ("🖥️", "VM Scale Set"),
    # Data
    "sql_database": ("🗄️", "SQL Database"),
    "sql_server": ("🗄️", "SQL Server"),
    "cosmos_db": ("🌍", "Cosmos DB"),
    "postgresql": ("🐘", "PostgreSQL"),
    "mysql": ("🐬", "MySQL"),
    "redis": ("⚡", "Redis Cache"),
    "storage_account": ("📁", "Storage Account"),
    # Security
    "key_vault": ("🔐", "Key Vault"),
    "managed_identity": ("🪪", "Managed Identity"),
    "nsg": ("🛡️", "NSG"),
    "firewall": ("🧱", "Azure Firewall"),
    "front_door": ("🚪", "Front Door"),
    "waf": ("🛡️", "WAF Policy"),
    # Networking
    "vnet": ("🌐", "Virtual Network"),
    "subnet": ("📡", "Subnet"),
    "private_endpoint": ("🔒", "Private Endpoint"),
    "load_balancer": ("⚖️", "Load Balancer"),
    "app_gateway": ("🚦", "App Gateway"),
    "dns_zone": ("🌍", "DNS Zone"),
    # Monitoring
    "log_analytics": ("📊", "Log Analytics"),
    "app_insights": ("🔍", "App Insights"),
    "monitor": ("📈", "Azure Monitor"),
    # Integration
    "service_bus": ("🚌", "Service Bus"),
    "event_hub": ("📡", "Event Hub"),
    "api_management": ("🔌", "API Management"),
    "logic_app": ("🔄", "Logic App"),
    # AI / ML
    "cognitive_services": ("🧠", "Cognitive Services"),
    "openai": ("🤖", "Azure OpenAI"),
}

# Connection types for different relationships
CONNECTION_STYLES = {
    "data_flow": "-->",          # Normal data flow
    "secure_access": "-.->",     # Dotted = secure/private access
    "monitoring": "-.->",        # Dotted = telemetry
    "network": "===",            # Thick = network boundary
    "identity": "-.->",         # Dotted = identity/RBAC
}


class DiagramResource(BaseModel):
    id: str = Field(description="Unique short identifier for this resource, e.g. 'app1', 'sqldb', 'kv'")
    type: str = Field(description="Resource type key, e.g. 'app_service', 'sql_database', 'key_vault', 'storage_account', 'vnet', 'log_analytics', 'redis', 'aks', 'container_app'")
    label: str = Field(description="Display label for the resource, e.g. 'Order API', 'Customer DB'")
    tier: str = Field(default="", description="Optional SKU/tier info to show, e.g. 'S1', 'P1v3', 'Standard'")


class DiagramConnection(BaseModel):
    from_id: str = Field(description="Source resource id")
    to_id: str = Field(description="Target resource id")
    label: str = Field(default="", description="Optional connection label, e.g. 'HTTPS', 'SQL Auth', 'Managed Identity'")
    style: str = Field(
        default="data_flow",
        description="Connection style: 'data_flow' (solid arrow), 'secure_access' (dotted), 'monitoring' (dotted), 'identity' (dotted)"
    )


class DiagramSubgraph(BaseModel):
    id: str = Field(description="Subgraph identifier")
    label: str = Field(description="Subgraph display label, e.g. 'Production VNet', 'Data Tier', 'Frontend'")
    resource_ids: list[str] = Field(description="List of resource ids contained in this subgraph")
    style: str = Field(default="", description="Optional: 'secure' for security boundary, 'network' for network boundary")


class GenerateDiagramParams(BaseModel):
    title: str = Field(description="Architecture diagram title, e.g. 'Order Management System - Production'")
    resources: list[DiagramResource] = Field(description="List of infrastructure resources to include in the diagram")
    connections: list[DiagramConnection] = Field(description="List of connections between resources showing data flow, access patterns, and dependencies")
    subgraphs: list[DiagramSubgraph] = Field(
        default=[],
        description="Optional groupings of resources into logical tiers or network boundaries (e.g., 'Frontend', 'Backend', 'Data Tier', 'VNet')"
    )
    direction: str = Field(
        default="TB",
        description="Diagram flow direction: 'TB' (top-bottom), 'LR' (left-right), 'BT' (bottom-top), 'RL' (right-left)"
    )
    environment: str = Field(default="production", description="Target environment for context in the diagram")
    show_security_boundaries: bool = Field(default=True, description="Whether to visually highlight security boundaries and private endpoints")


def _get_resource_node(resource: DiagramResource) -> str:
    """Generate a Mermaid node definition for a resource."""
    icon, default_label = RESOURCE_SHAPES.get(resource.type, ("📦", resource.type))
    label = resource.label or default_label
    tier_info = f"<br/><i>{resource.tier}</i>" if resource.tier else ""
    # Use stadium shape for compute, cylinder for data, hexagon for security
    if resource.type in ("sql_database", "sql_server", "cosmos_db", "postgresql", "mysql",
                         "redis", "storage_account"):
        return f'    {resource.id}[("{icon} {label}{tier_info}")]'
    elif resource.type in ("key_vault", "managed_identity", "nsg", "firewall", "waf",
                           "front_door"):
        return f'    {resource.id}{{{{{icon} {label}{tier_info}}}}}'
    elif resource.type in ("vnet", "subnet", "private_endpoint"):
        return f'    {resource.id}[{icon} {label}{tier_info}]'
    else:
        return f'    {resource.id}([{icon} {label}{tier_info}])'


def _get_connection(conn: DiagramConnection) -> str:
    """Generate a Mermaid connection line."""
    arrow = CONNECTION_STYLES.get(conn.style, "-->")
    if conn.label:
        return f'    {conn.from_id} {arrow}|{conn.label}| {conn.to_id}'
    else:
        return f'    {conn.from_id} {arrow} {conn.to_id}'


@define_tool(description=(
    "Generate a Mermaid architecture diagram from an infrastructure design. "
    "Use this tool AFTER searching the catalog and composing/generating infrastructure, "
    "to create a visual diagram for stakeholder review and approval. "
    "The diagram shows resources, connections, data flows, security boundaries, "
    "and network topology. Output is a Mermaid flowchart that can be rendered in "
    "markdown, GitHub, Azure DevOps wikis, or exported to PNG/SVG. "
    "Provide resources with types and labels, connections showing data flow and access "
    "patterns, and optional subgraphs for logical groupings (tiers, VNets, etc.)."
))
async def generate_architecture_diagram(params: GenerateDiagramParams) -> str:
    """Generate a Mermaid architecture diagram."""
    lines = []

    # Header
    lines.append(f"```mermaid")
    lines.append(f"---")
    lines.append(f"title: {params.title}")
    lines.append(f"---")
    lines.append(f"flowchart {params.direction}")

    # Collect which resource ids are inside subgraphs
    subgraph_resource_ids = set()
    for sg in params.subgraphs:
        subgraph_resource_ids.update(sg.resource_ids)

    # Render subgraphs with their contained resources
    for sg in params.subgraphs:
        style_comment = ""
        if sg.style == "secure":
            style_comment = " 🔒"
        elif sg.style == "network":
            style_comment = " 🌐"
        lines.append(f"")
        lines.append(f"    subgraph {sg.id}[\"{sg.label}{style_comment}\"]")

        for resource in params.resources:
            if resource.id in sg.resource_ids:
                lines.append(f"    {_get_resource_node(resource)}")

        lines.append(f"    end")

    # Render resources NOT in any subgraph
    orphan_resources = [r for r in params.resources if r.id not in subgraph_resource_ids]
    if orphan_resources:
        lines.append(f"")
        for resource in orphan_resources:
            lines.append(_get_resource_node(resource))

    # Render connections
    lines.append(f"")
    lines.append(f"    %% Connections")
    for conn in params.connections:
        lines.append(_get_connection(conn))

    # Add styling
    lines.append(f"")
    lines.append(f"    %% Styling")
    lines.append(f"    classDef compute fill:#0078D4,stroke:#005A9E,color:#fff")
    lines.append(f"    classDef data fill:#50E6FF,stroke:#0078D4,color:#000")
    lines.append(f"    classDef security fill:#FF8C00,stroke:#D4700A,color:#fff")
    lines.append(f"    classDef network fill:#7FBA00,stroke:#5E8A00,color:#fff")
    lines.append(f"    classDef monitoring fill:#B4A0FF,stroke:#7B68EE,color:#000")

    # Apply styles based on resource types
    compute_ids = []
    data_ids = []
    security_ids = []
    network_ids = []
    monitoring_ids = []

    for r in params.resources:
        if r.type in ("app_service", "function_app", "container_app", "aks", "vm", "vmss"):
            compute_ids.append(r.id)
        elif r.type in ("sql_database", "sql_server", "cosmos_db", "postgresql", "mysql",
                         "redis", "storage_account"):
            data_ids.append(r.id)
        elif r.type in ("key_vault", "managed_identity", "nsg", "firewall", "waf", "front_door"):
            security_ids.append(r.id)
        elif r.type in ("vnet", "subnet", "private_endpoint", "load_balancer", "app_gateway",
                         "dns_zone"):
            network_ids.append(r.id)
        elif r.type in ("log_analytics", "app_insights", "monitor"):
            monitoring_ids.append(r.id)

    if compute_ids:
        lines.append(f"    class {','.join(compute_ids)} compute")
    if data_ids:
        lines.append(f"    class {','.join(data_ids)} data")
    if security_ids:
        lines.append(f"    class {','.join(security_ids)} security")
    if network_ids:
        lines.append(f"    class {','.join(network_ids)} network")
    if monitoring_ids:
        lines.append(f"    class {','.join(monitoring_ids)} monitoring")

    lines.append(f"```")

    # Build the full output
    mermaid_code = "\n".join(lines)

    # Summary
    summary_parts = [
        f"\n## Architecture Diagram: {params.title}",
        f"",
        f"**Environment:** {params.environment}",
        f"**Resources:** {len(params.resources)} | **Connections:** {len(params.connections)}"
        f" | **Groupings:** {len(params.subgraphs)}",
        f"",
        mermaid_code,
        f"",
        f"---",
        f"**Legend:**",
        f"- 🟦 Blue = Compute (App Service, Functions, AKS, VMs)",
        f"- 🟦 Cyan = Data (SQL, Cosmos, Storage, Redis)",
        f"- 🟧 Orange = Security (Key Vault, Managed Identity, Firewall)",
        f"- 🟩 Green = Networking (VNet, Subnets, Private Endpoints)",
        f"- 🟪 Purple = Monitoring (Log Analytics, App Insights)",
        f"- Solid arrows (→) = Data flow",
        f"- Dotted arrows (⇢) = Secure access / monitoring / identity",
        f"",
        f"> **Render this diagram** in GitHub markdown, Azure DevOps wiki, VS Code preview,",
        f"> or paste into [mermaid.live](https://mermaid.live) for interactive editing.",
    ]

    return "\n".join(summary_parts)
