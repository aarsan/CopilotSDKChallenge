"""Standards Import Engine — AI-powered document ingestion.

Accepts text, markdown, or URL content representing an organization's
security/compliance/governance standards documentation and converts it
into InfraForge ``org_standards`` format using the Copilot SDK.

Usage:
    from src.standards_import import import_standards_from_text

    standards = await import_standards_from_text(
        content="...",
        source_type="text",        # text | markdown | url
        copilot_client=client,
    )
    # → list[dict] ready for create_standard()
"""

import asyncio
import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ── System prompt for the LLM ────────────────────────────────

_SYSTEM_PROMPT = """\
You are an infrastructure compliance expert. Your job is to extract
structured governance and security standards from documentation text
and output them as JSON.

Each standard must be converted into this exact schema:

{
  "id": "STD-<SHORT-CODE>",
  "name": "<Human-readable standard name>",
  "description": "<Full description of what this standard enforces>",
  "category": "<one of: encryption, identity, network, monitoring, tagging, region, cost, security, compliance, general>",
  "severity": "<one of: critical, high, medium, low>",
  "scope": "<comma-separated Azure resource type globs, e.g. 'Microsoft.Storage/*,Microsoft.Sql/*' or '*' for all>",
  "enabled": true,
  "rule": {
    "type": "<one of: property, tags, allowed_values, cost_threshold>",
    ... type-specific fields (see below) ...
    "remediation": "<How to fix a resource that violates this standard>"
  }
}

Rule type schemas:

1. property — Check a resource property value
   {"type": "property", "key": "<ARM property name>", "operator": "<==|!=|>=|<=|in|exists>", "value": <expected>, "remediation": "..."}
   
   IMPORTANT property key mappings for Azure ARM:
   - TLS version → "minTlsVersion" (checks minTlsVersion/minimumTlsVersion/minimalTlsVersion per resource type)
   - HTTPS required → "httpsOnly" (checks httpsOnly or supportsHttpsTrafficOnly)
   - Managed identity → "managedIdentity" (checks identity.type on the resource)
   - Public network access → "publicNetworkAccess"
   - Encryption at rest → "encryptionAtRest"
   - Soft delete → "enableSoftDelete"
   - Purge protection → "enablePurgeProtection"
   - RBAC authorization → "enableRbacAuthorization"
   - AAD authentication → "aadAuthEnabled"
   - Blob public access → "allowBlobPublicAccess"

2. tags — Check for required resource tags
   {"type": "tags", "required_tags": ["environment", "owner", ...], "remediation": "..."}

3. allowed_values — Check a value is in an allowlist
   {"type": "allowed_values", "key": "<property>", "values": ["value1", "value2", ...], "remediation": "..."}
   Common use: allowed regions → key="location", values=["eastus", "westus2", ...]

4. cost_threshold — Monthly cost cap (informational)
   {"type": "cost_threshold", "max_monthly_usd": 500, "remediation": "..."}

CRITICAL RULES:
- Output ONLY a JSON array of standard objects — no markdown, no explanation
- Merge related requirements into single standards where possible
- Use meaningful IDs like STD-ENCRYPT-TLS, STD-TAG-REQUIRED, STD-REGION-ALLOWED
- Set appropriate severity: critical for security/data protection, high for identity/access, medium for monitoring, low for cost
- Set appropriate scope patterns — don't use '*' when a standard only applies to specific resource types
- If a requirement is vague or non-actionable as an ARM check, still include it with type "property" and a descriptive remediation
- Extract ALL standards from the document, even if there are many
"""

_USER_PROMPT_TEMPLATE = """\
Extract all governance and security standards from the following documentation.
Convert each standard into an InfraForge standard JSON object.

--- DOCUMENTATION ---
{content}
--- END DOCUMENTATION ---

Return ONLY a valid JSON array of standard objects. No markdown fences, no explanation.
"""


async def import_standards_from_text(
    content: str,
    source_type: str = "text",
    copilot_client=None,
) -> list[dict]:
    """Import standards from text content using LLM extraction.

    Args:
        content: The standards documentation text (plain text or markdown)
        source_type: "text", "markdown", or "url" (for future URL fetching)
        copilot_client: A CopilotClient instance (from ensure_copilot_client)

    Returns:
        List of standard dicts in InfraForge org_standards format,
        ready to pass to create_standard().

    Raises:
        ValueError: If content is empty or LLM returns invalid JSON
        RuntimeError: If Copilot SDK is not available
    """
    if not content or not content.strip():
        raise ValueError("Standards documentation content is empty")

    if copilot_client is None:
        raise RuntimeError("Copilot SDK client is required for standards import")

    # Truncate very long documents to avoid token limits
    MAX_CHARS = 50_000
    if len(content) > MAX_CHARS:
        content = content[:MAX_CHARS] + "\n\n[... document truncated ...]"
        logger.warning(f"Standards document truncated from {len(content)} to {MAX_CHARS} chars")

    prompt = _USER_PROMPT_TEMPLATE.format(content=content)

    # ── Call the LLM ──────────────────────────────────────────
    session = None
    try:
        session = await copilot_client.create_session({
            "model": "gpt-4.1",
            "streaming": True,
            "tools": [],
            "system_message": {"content": _SYSTEM_PROMPT},
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
            await asyncio.wait_for(done_ev.wait(), timeout=120)
        finally:
            unsub()

        raw = "".join(chunks).strip()

    except Exception as e:
        logger.error(f"Standards import LLM call failed: {e}")
        raise RuntimeError(f"LLM call failed: {e}") from e
    finally:
        if session:
            try:
                await session.close()
            except Exception:
                pass

    # ── Parse the LLM response ────────────────────────────────
    standards = _parse_standards_response(raw)

    logger.info(f"Standards import: extracted {len(standards)} standards from {source_type} document ({len(content)} chars)")
    return standards


def _parse_standards_response(raw: str) -> list[dict]:
    """Parse the LLM response into a list of standard dicts.

    Handles common LLM formatting issues (markdown fences, trailing text).
    """
    # Strip markdown code fences
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json) and last line (```)
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Try to find JSON array in the text
    if not text.startswith("["):
        # Look for the first [ and last ]
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            text = text[start:end + 1]

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse standards JSON: {e}\nRaw: {text[:500]}")
        raise ValueError(f"LLM returned invalid JSON: {e}") from e

    if not isinstance(parsed, list):
        raise ValueError("LLM response is not a JSON array")

    # Validate and normalize each standard
    valid_standards: list[dict] = []
    for i, std in enumerate(parsed):
        if not isinstance(std, dict):
            logger.warning(f"Skipping non-dict item at index {i}")
            continue

        normalized = _normalize_standard(std, i)
        if normalized:
            valid_standards.append(normalized)

    return valid_standards


def _normalize_standard(std: dict, index: int) -> Optional[dict]:
    """Normalize and validate a single standard dict."""
    name = std.get("name", "").strip()
    if not name:
        logger.warning(f"Skipping standard at index {index}: no name")
        return None

    # Ensure required fields
    std_id = std.get("id", f"STD-IMPORT-{index + 1:03d}")

    # Validate category
    valid_categories = {
        "encryption", "identity", "network", "monitoring",
        "tagging", "region", "cost", "security", "compliance", "general",
    }
    category = std.get("category", "general").lower()
    if category not in valid_categories:
        category = "general"

    # Validate severity
    valid_severities = {"critical", "high", "medium", "low"}
    severity = std.get("severity", "high").lower()
    if severity not in valid_severities:
        severity = "high"

    # Validate rule
    rule = std.get("rule", {})
    if not isinstance(rule, dict):
        rule = {}
    rule_type = rule.get("type", "property")
    valid_types = {"property", "property_check", "tags", "allowed_values", "cost_threshold"}
    if rule_type not in valid_types:
        rule["type"] = "property"

    return {
        "id": std_id,
        "name": name,
        "description": std.get("description", ""),
        "category": category,
        "severity": severity,
        "scope": std.get("scope", "*"),
        "enabled": bool(std.get("enabled", True)),
        "rule": rule,
    }
