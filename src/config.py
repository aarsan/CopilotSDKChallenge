"""
InfraForge configuration and constants.
"""

import os
from dotenv import load_dotenv

load_dotenv(override=True)

# ── App Settings ──────────────────────────────────────────────
APP_NAME = "InfraForge"
APP_VERSION = "1.0.0"
APP_DESCRIPTION = (
    "AI-powered Infrastructure-as-Code and CI/CD pipeline generator. "
    "Describe your infrastructure in plain English and get production-ready "
    "Bicep, Terraform, GitHub Actions, and Azure DevOps pipelines in seconds."
)

# ── Copilot SDK Settings ─────────────────────────────────────
COPILOT_MODEL = os.getenv("COPILOT_MODEL", "gpt-4.1")
COPILOT_LOG_LEVEL = os.getenv("COPILOT_LOG_LEVEL", "warning")

# Available LLM models — users can switch at runtime via the API/UI.
# The first model is the default. Models are exposed through the Copilot SDK
# proxy so the user doesn't need their own API keys.
AVAILABLE_MODELS = [
    {"id": "gpt-4.1",           "name": "GPT-4.1",             "provider": "OpenAI",    "tier": "flagship",  "description": "Best overall quality and instruction following"},
    {"id": "gpt-4.1-mini",      "name": "GPT-4.1 Mini",        "provider": "OpenAI",    "tier": "fast",      "description": "Fast and cost-efficient, good for simple tasks"},
    {"id": "gpt-4.1-nano",      "name": "GPT-4.1 Nano",        "provider": "OpenAI",    "tier": "fastest",   "description": "Ultra-fast, best for trivial tasks"},
    {"id": "gpt-4o",            "name": "GPT-4o",              "provider": "OpenAI",    "tier": "flagship",  "description": "Multimodal flagship model"},
    {"id": "gpt-4o-mini",       "name": "GPT-4o Mini",         "provider": "OpenAI",    "tier": "fast",      "description": "Smaller, faster GPT-4o variant"},
    {"id": "o3-mini",           "name": "o3-mini",             "provider": "OpenAI",    "tier": "reasoning", "description": "Optimized for reasoning and complex logic"},
    {"id": "claude-sonnet-4",   "name": "Claude Sonnet 4",     "provider": "Anthropic", "tier": "flagship",  "description": "Strong reasoning and code generation"},
    {"id": "claude-3.5-sonnet", "name": "Claude 3.5 Sonnet",   "provider": "Anthropic", "tier": "flagship",  "description": "Previous-gen Anthropic flagship"},
    {"id": "gemini-2.0-flash",  "name": "Gemini 2.0 Flash",    "provider": "Google",    "tier": "fast",      "description": "Google's fast multimodal model"},
]

# Mutable active model — can be changed at runtime via PUT /api/settings/model
_active_model: str = COPILOT_MODEL


def get_active_model() -> str:
    """Return the currently active LLM model ID."""
    return _active_model


def set_active_model(model_id: str) -> bool:
    """Set the active model. Returns True if valid, False if not in AVAILABLE_MODELS."""
    global _active_model
    valid_ids = {m["id"] for m in AVAILABLE_MODELS}
    if model_id not in valid_ids:
        return False
    _active_model = model_id
    return True

# ── Governance Enforcement Mode ──────────────────────────────
# "enforce" = current behavior (block/warn as configured per policy)
# "audit"   = all checks run & report findings, but nothing blocks
_enforcement_mode: str = "audit"


def get_enforcement_mode() -> str:
    """Return the current governance enforcement mode: 'enforce' or 'audit'."""
    return _enforcement_mode


def set_enforcement_mode(mode: str) -> bool:
    """Set the governance enforcement mode. Returns True if valid."""
    global _enforcement_mode
    if mode not in ("enforce", "audit"):
        return False
    _enforcement_mode = mode
    return True

# ── Output Settings ──────────────────────────────────────────
OUTPUT_DIR = os.getenv("INFRAFORGE_OUTPUT_DIR", "./output")

# ── Web Server Settings ──────────────────────────────────────
WEB_HOST = os.getenv("INFRAFORGE_WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("INFRAFORGE_WEB_PORT", "8080"))
SESSION_SECRET = os.getenv("INFRAFORGE_SESSION_SECRET", "infraforge-dev-secret-change-in-prod")

# ── Entra ID (Azure AD) Authentication ───────────────────────
# Configure these to enable corporate SSO. When not set, login is
# disabled and users will see a configuration error.
ENTRA_CLIENT_ID = os.getenv("ENTRA_CLIENT_ID", "")
ENTRA_TENANT_ID = os.getenv("ENTRA_TENANT_ID", "")
ENTRA_CLIENT_SECRET = os.getenv("ENTRA_CLIENT_SECRET", "")
ENTRA_REDIRECT_URI = os.getenv("ENTRA_REDIRECT_URI", f"http://localhost:{WEB_PORT}/api/auth/callback")
ENTRA_AUTHORITY = f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}" if ENTRA_TENANT_ID else ""
ENTRA_SCOPES = ["User.Read"]

# ── GitHub Integration ────────────────────────────────────────
# Service-level GitHub credential for publishing repos and PRs.
# End users authenticate via Entra ID only — the app uses this
# token to push generated infrastructure to GitHub on their behalf.
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_ORG = os.getenv("GITHUB_ORG", "")  # GitHub org or user to create repos under
GITHUB_API_URL = os.getenv("GITHUB_API_URL", "https://api.github.com")

# ── Database ───────────────────────────────────────────────
# Azure SQL Database with Azure AD auth (pyodbc + DefaultAzureCredential).
AZURE_SQL_CONNECTION_STRING = os.getenv("AZURE_SQL_CONNECTION_STRING", "")

# ── Microsoft Fabric Integration ─────────────────────────────
# Fabric workspace for analytics sync (OneLake DFS).
FABRIC_WORKSPACE_ID = os.getenv("FABRIC_WORKSPACE_ID", "")
FABRIC_ONELAKE_DFS_ENDPOINT = os.getenv("FABRIC_ONELAKE_DFS_ENDPOINT", "")
FABRIC_LAKEHOUSE_NAME = os.getenv("FABRIC_LAKEHOUSE_NAME", "infraforge_lakehouse")

# ── Supported IaC Formats ────────────────────────────────────
IAC_FORMATS = ["bicep", "terraform", "arm"]

# ── Supported Pipeline Formats ───────────────────────────────
PIPELINE_FORMATS = ["github-actions", "azure-devops"]

# ── Azure Regions ─────────────────────────────────────────────
DEFAULT_AZURE_REGION = "eastus2"
AZURE_REGIONS = [
    "eastus", "eastus2", "westus", "westus2", "westus3",
    "centralus", "northcentralus", "southcentralus",
    "westeurope", "northeurope", "uksouth", "ukwest",
    "southeastasia", "eastasia", "japaneast", "japanwest",
    "australiaeast", "australiasoutheast",
    "canadacentral", "canadaeast",
    "brazilsouth",
]

# Canonical abbreviations used in resource names.
# Must stay in sync with the Naming Conventions prompt in static/app.js.
REGION_ABBREVIATIONS: dict[str, str] = {
    "eastus":              "eus",
    "eastus2":             "eus2",
    "westus":              "wus",
    "westus2":             "wus2",
    "westus3":             "wus3",
    "centralus":           "cus",
    "northcentralus":      "ncus",
    "southcentralus":      "scus",
    "westeurope":          "weu",
    "northeurope":         "neu",
    "uksouth":             "uks",
    "ukwest":              "ukw",
    "southeastasia":       "sea",
    "eastasia":            "ea",
    "japaneast":           "jpe",
    "japanwest":           "jpw",
    "australiaeast":       "aue",
    "australiasoutheast":  "ause",
    "canadacentral":       "cac",
    "canadaeast":          "cae",
    "brazilsouth":         "brs",
}


def region_abbr(region: str) -> str:
    """Return the abbreviated form of an Azure region for use in resource names."""
    return REGION_ABBREVIATIONS.get(region.lower(), region.lower())

# ── Policy / Governance Defaults ─────────────────────────────
# NOTE: Governance policies are now stored in the database (governance_policies table).
# They are seeded automatically on first run by database.seed_governance_data().
# The DEFAULT_POLICIES dict below is retained ONLY as a last-resort fallback if
# the database is unreachable.  At runtime, policy_checker.py reads from the DB.
DEFAULT_POLICIES = {
    "require_tags": ["environment", "owner", "costCenter", "project"],
    "allowed_regions": ["eastus2", "westus2", "westeurope"],
    "naming_convention": "{resourceType}-{project}-{environment}-{region}-{instance}",
    "require_https": True,
    "require_managed_identity": True,
    "require_private_endpoints": False,
    "max_public_ips": 0,
}
