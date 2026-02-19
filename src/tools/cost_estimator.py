"""
Azure cost estimation tool.
Provides approximate monthly cost estimates for Azure resources.
"""

from pydantic import BaseModel, Field
from copilot import define_tool


# ── Approximate Azure pricing (USD/month) ────────────────────
# These are rough estimates for estimation purposes.
# Real pricing varies by region, tier, and usage.
AZURE_PRICING = {
    # Compute
    "app_service_b1": 13.14,
    "app_service_s1": 69.35,
    "app_service_p1v3": 138.70,
    "app_service_p2v3": 277.40,
    "vm_b1s": 7.59,
    "vm_b2s": 30.37,
    "vm_d2s_v3": 70.08,
    "vm_d4s_v3": 140.16,
    "vm_d8s_v3": 280.32,
    "aks_node_d2s_v3": 70.08,
    "aks_node_d4s_v3": 140.16,
    "container_app_consumption": 0.00,  # Pay per use
    "function_app_consumption": 0.00,   # Pay per use

    # Databases
    "sql_db_basic": 4.90,
    "sql_db_s0": 15.03,
    "sql_db_s1": 30.05,
    "sql_db_s2": 75.13,
    "sql_db_p1": 465.00,
    "cosmos_db_serverless": 0.25,  # Per RU
    "cosmos_db_400ru": 23.36,
    "postgresql_flexible_b1ms": 12.41,
    "postgresql_flexible_gp_d2s_v3": 98.55,
    "mysql_flexible_b1ms": 12.41,
    "redis_c0": 16.00,
    "redis_c1": 40.56,

    # Storage
    "storage_account_lrs": 0.018,  # Per GB
    "storage_account_grs": 0.036,  # Per GB
    "blob_storage_hot_100gb": 1.80,
    "blob_storage_cool_100gb": 0.90,

    # Networking
    "load_balancer_basic": 0.00,
    "load_balancer_standard": 18.25,
    "application_gateway_v2": 175.20,
    "front_door_standard": 35.00,
    "cdn_standard": 0.08,  # Per GB
    "public_ip_standard": 3.65,
    "vnet": 0.00,
    "nat_gateway": 32.85,
    "private_endpoint": 7.30,

    # Security & Identity
    "key_vault_standard": 0.03,  # Per 10K operations
    "key_vault_premium": 1.00,
    "managed_identity": 0.00,

    # Monitoring
    "log_analytics_per_gb": 2.76,  # Per GB ingested
    "app_insights": 2.76,  # Per GB
    "monitor_alerts": 0.10,  # Per alert rule

    # AI / ML
    "openai_gpt4_1k_tokens": 0.03,
    "cognitive_services_s0": 1.00,
}

# ── Environment multipliers ──────────────────────────────────
ENV_MULTIPLIERS = {
    "dev": 0.5,       # Smaller SKUs
    "staging": 0.75,  # Medium SKUs
    "prod": 1.0,      # Full production SKUs
}


class EstimateCostParams(BaseModel):
    resources: list[str] = Field(
        description=(
            "List of Azure resources to estimate costs for. "
            "Examples: ['app_service_s1', 'sql_db_s1', 'redis_c1', 'key_vault_standard']"
        )
    )
    environment: str = Field(
        default="prod",
        description="Environment for cost scaling: dev, staging, or prod",
    )
    quantity: dict[str, int] = Field(
        default={},
        description=(
            "Optional quantity overrides for resources. "
            "Example: {'aks_node_d2s_v3': 3, 'private_endpoint': 5}"
        ),
    )


@define_tool(description=(
    "Estimate monthly Azure costs for a set of resources. "
    "Use this tool when the user asks about infrastructure costs or wants a cost breakdown. "
    "Provide the list of Azure resource types and it will return approximate monthly costs. "
    "Available resource keys include: app_service_b1/s1/p1v3, vm_b1s/d2s_v3/d4s_v3, "
    "sql_db_basic/s0/s1/p1, cosmos_db_serverless/400ru, postgresql_flexible_b1ms, "
    "redis_c0/c1, storage_account_lrs/grs, load_balancer_standard, application_gateway_v2, "
    "key_vault_standard, log_analytics_per_gb, app_insights, and more."
))
async def estimate_azure_cost(params: EstimateCostParams) -> str:
    """Calculate approximate monthly Azure costs."""
    multiplier = ENV_MULTIPLIERS.get(params.environment, 1.0)

    lines = []
    total = 0.0

    lines.append(f"## Azure Cost Estimate — {params.environment.upper()} Environment\n")
    lines.append("| Resource | Qty | Unit Cost | Adjusted | Monthly |")
    lines.append("|----------|-----|-----------|----------|---------|")

    for resource in params.resources:
        resource_key = resource.lower().replace("-", "_").replace(" ", "_")
        unit_cost = AZURE_PRICING.get(resource_key, None)

        if unit_cost is None:
            lines.append(f"| {resource} | — | Unknown | — | ⚠️ Not in pricing catalog |")
            continue

        qty = params.quantity.get(resource, 1)
        adjusted = unit_cost * multiplier
        monthly = adjusted * qty
        total += monthly

        lines.append(
            f"| {resource} | {qty} | ${unit_cost:,.2f} | ${adjusted:,.2f} | ${monthly:,.2f} |"
        )

    lines.append(f"| **TOTAL** | | | | **${total:,.2f}/mo** |")
    lines.append("")
    lines.append(f"_Estimated annual: ${total * 12:,.2f}_")
    lines.append("")
    lines.append("**Notes:**")
    lines.append(f"- Environment multiplier ({params.environment}): {multiplier}x")
    lines.append("- Prices are approximate USD list prices and may vary by region")
    lines.append("- Consumption-based services (Functions, Container Apps) depend on usage")
    lines.append("- Enterprise agreements and reservations can reduce costs 30-60%")

    return "\n".join(lines)
