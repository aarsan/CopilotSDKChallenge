"""ARM template generation and editing helpers.

InfraForge now generates ARM templates exclusively through the Copilot SDK.
This module keeps the shared template metadata/constants plus helper routines
for generation, modification, and template post-processing.
"""

import json
import logging
import re

logger = logging.getLogger("infraforge.tools.arm_generator")


_STANDARD_PARAMETERS = {
    "resourceName": {
        "type": "string",
        "defaultValue": "infraforge-resource",
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


def strip_foreign_resources(template_json: str, service_id: str) -> str:
    """Remove resources that don't match the service's own resource type."""
    try:
        tpl = json.loads(template_json)
    except (json.JSONDecodeError, TypeError):
        return template_json

    resources = tpl.get("resources", [])
    if not resources:
        return template_json

    own_type = service_id.lower()
    kept = []
    removed_ids: set[str] = set()

    for res in resources:
        rtype = (res.get("type") or "").lower()
        if rtype == own_type or rtype.startswith(own_type + "/"):
            kept.append(res)
        else:
            removed_ids.add(rtype)
            rname = res.get("name", "")
            if rname:
                removed_ids.add(rname.lower())

    if len(kept) == len(resources):
        return template_json

    removed_types = [r.get("type", "?") for r in resources if r not in kept]

    if not kept:
        logger.error(
            f"[strip_foreign] {service_id}: would remove ALL {len(resources)} "
            f"resource(s) — none matched '{service_id}'. Types present: "
            f"{removed_types}. Returning original template unchanged."
        )
        return template_json

    logger.info(
        f"[strip_foreign] {service_id}: removed {len(removed_types)} foreign "
        f"resource(s): {removed_types}"
    )

    for res in kept:
        depends = res.get("dependsOn", [])
        if depends:
            cleaned = [
                d for d in depends
                if not any(rid in d.lower() for rid in removed_ids)
            ]
            if len(cleaned) != len(depends):
                if cleaned:
                    res["dependsOn"] = cleaned
                else:
                    del res["dependsOn"]

    tpl["resources"] = kept
    return json.dumps(tpl, indent=2)


async def modify_arm_template_with_copilot(
    existing_template: str,
    modification_prompt: str,
    resource_type: str,
    copilot_client,
    model: str = "gpt-4.1",
) -> str:
    """Use the Copilot SDK to modify an existing ARM template based on a user prompt."""
    prompt = (
        f"You are modifying an existing ARM template for Azure resource type '{resource_type}'.\n\n"
        f"--- CURRENT ARM TEMPLATE ---\n"
        f"{existing_template}\n"
        f"--- END CURRENT TEMPLATE ---\n\n"
        f"--- REQUESTED MODIFICATION ---\n"
        f"{modification_prompt}\n"
        f"--- END MODIFICATION ---\n\n"
        "Apply the requested modification to the ARM template above.\n"
        "RULES:\n"
        "- Return the COMPLETE modified ARM template as a single JSON object\n"
        "- Preserve ALL existing parameters, resources, outputs, and tags unless the modification explicitly asks to remove them\n"
        "- EVERY parameter MUST keep a defaultValue\n"
        "- Keep contentVersion, $schema, and metadata sections intact\n"
        "- Maintain correct property nesting: sku, identity, tags, kind at resource root; resource-specific config inside 'properties'\n"
        "- Use the LATEST STABLE API version — never downgrade\n"
        "- Return ONLY the raw JSON — no markdown fences, no explanation\n"
        "- If the modification asks for something that doesn't make sense for this resource type, still return the template with a best-effort change and add a comment in the template metadata\n"
    )

    from src.copilot_helpers import copilot_send
    from src.agents import ARM_MODIFIER

    max_attempts = 3
    last_error = ""

    for attempt in range(1, max_attempts + 1):
        try:
            actual_prompt = prompt
            if attempt > 1:
                actual_prompt += (
                    f"\n\nPREVIOUS ATTEMPT FAILED: {last_error}\n"
                    "Return ONLY the raw JSON object starting with {{ and ending with }}. "
                    "No markdown fences, no explanation text."
                )

            raw = await copilot_send(
                copilot_client,
                model=model,
                system_prompt=ARM_MODIFIER.system_prompt,
                prompt=actual_prompt,
                timeout=90,
                agent_name="ARM_MODIFIER",
            )
            result = _extract_json_from_llm_response(raw)

            parsed = json.loads(result)

            if not isinstance(parsed, dict) or ("resources" not in parsed and "$schema" not in parsed):
                raise json.JSONDecodeError("Response is valid JSON but not an ARM template", result, 0)

            logger.info(f"Copilot modified ARM template for {resource_type} (attempt {attempt})")
            return result

        except (json.JSONDecodeError, ValueError) as e:
            last_error = str(e)
            logger.warning(f"ARM modification attempt {attempt}/{max_attempts} for {resource_type} failed: {last_error}")
            if attempt == max_attempts:
                logger.error(f"Copilot failed to modify ARM template for {resource_type} after {max_attempts} attempts")
                raise ValueError(f"Failed to modify ARM template for {resource_type}: {last_error}")


async def generate_arm_template_with_copilot(
    resource_type: str,
    service_name: str,
    copilot_client,
    model: str = "gpt-4.1",
    standards_context: str = "",
    planning_context: str = "",
    region: str = "",
    governance_context: str = "",
) -> str:
    """Use the Copilot SDK to generate an ARM template for a resource type."""
    prompt = (
        f"Generate a minimal ARM template (JSON) for deploying the Azure resource type "
        f"'{resource_type}' (service: {service_name}).\n\n"
    )

    from src.template_engine import get_parent_resource_type
    _parent_type = get_parent_resource_type(resource_type)
    if _parent_type:
        prompt += (
            f"## CRITICAL — CHILD RESOURCE TYPE\n"
            f"'{resource_type}' is a CHILD resource of '{_parent_type}'. Child resources CANNOT exist without their parent in Azure.\n"
            f"You MUST include the parent resource ({_parent_type}) in the same template.\n"
            f"Use one of these approaches:\n"
            f"1. Define the parent resource first, then define the child as a separate resource with name like \"[concat(parameters('parentName'), '/childName')]\" and a dependsOn reference to the parent\n"
            f"2. Define the child as a nested resource inside the parent's resources array\n\n"
            f"The parent resource should have minimal configuration — just enough to be deployable. The child resource is the primary focus.\n\n"
        )

    from src.config import region_abbr as _region_abbr
    _region = region or "eastus2"
    _abbr = _region_abbr(_region)
    prompt += (
        f"### Naming Convention\n"
        f"Deployment region: {_region} (abbreviation: {_abbr})\n"
        f"Resource names MUST include the region abbreviation '{_abbr}' — NEVER use a different region in the name.\n"
        f"Pattern: {{resourceType}}-{{project}}-{{environment}}-{_abbr}-{{instance}}\n"
        f"Example: infraforge-sql-dev-{_abbr}-001\n\n"
    )

    if planning_context:
        prompt += (
            "--- ARCHITECTURE PLAN (follow this plan precisely) ---\n"
            f"{planning_context}\n"
            "--- END PLAN ---\n\n"
            "Follow the architecture plan above. It specifies the resources, security configurations, parameters, and properties to include.\n\n"
            "CRITICAL: EVERY parameter in the template MUST have a \"defaultValue\". This template is deployed with parameters={} for validation, so any parameter without a default will cause a deployment failure. Use sensible defaults (e.g. resourceName → \"infraforge-resource\", location → \"[resourceGroup().location]\").\n\n"
            "MINIMAL INFRASTRUCTURE: Unless the plan explicitly says otherwise, use a SINGLE availability zone (or omit 'zones' entirely) — NEVER specify zones: [\"1\",\"2\",\"3\"]. NAT Gateways and many resources FAIL with multiple zones. Set zoneRedundant=false, geoRedundantBackup='Disabled', requestedBackupStorageRedundancy='Local'. Only add HA/redundancy if the plan explicitly requests it.\n\n"
        )
    else:
        prompt += (
            "Requirements:\n"
            "- Include standard parameters — EVERY parameter MUST have a defaultValue (this template is deployed with parameters={} for validation):\n"
            "  resourceName (string, default \"infraforge-resource\"), location (string, default \"[resourceGroup().location]\"), environment (string, default \"dev\"), projectName (string, default \"infraforge\"), ownerEmail (string, default \"platform-team@company.com\"), costCenter (string, default \"IT-0001\")\n"
            "- Include tags: environment, owner, costCenter, project, managedBy=InfraForge\n"
            "- Use the LATEST STABLE (GA) API version for this resource type — never use preview versions unless no GA version exists\n"
            "- Include minimal required properties only\n"
            "- SINGLE ZONE / NO REDUNDANCY: Do NOT specify multiple availability zones. Either omit the 'zones' property entirely or set it to at most [\"1\"]. Many resources (NAT Gateways, some LBs) fail with multiple zones. Use requestedBackupStorageRedundancy='Local', geoRedundantBackup='Disabled', zoneRedundant=false. Only add HA/redundancy if explicitly requested.\n"
            "- Enable managed identity (SystemAssigned) if the resource supports it — identity block goes at RESOURCE ROOT, not inside properties\n"
            "- sku goes at RESOURCE ROOT level, not inside properties\n"
            "- Set httpsOnly/minTlsVersion where applicable (inside properties)\n"
            "- Disable public network access where applicable\n"
            "- NEVER use utcNow() except in a parameter defaultValue expression; do not place utcNow() in variables, tags, resource properties, or outputs\n"
            "- Do NOT include diagnostic settings or Log Analytics dependencies\n\n"
            "### Property Nesting (CRITICAL — wrong nesting causes deployment failures)\n"
            "Resource root level: type, apiVersion, name, location, kind, sku, identity, tags, zones, dependsOn\n"
            "Inside 'properties': ALL resource-specific configuration\n"
            "WRONG: {\"properties\": {\"sku\": ...}}  CORRECT: {\"sku\": ..., \"properties\": {...}}\n"
        )

    prompt += (
        "- CRITICAL: The template MUST contain ONLY resources of type "
        f"'{resource_type}' (or its child types). Do NOT include dependency resources like VNets, NICs, Public IPs, Managed Identities, etc. Dependencies are handled separately by the composition layer. Use parameters to reference external dependency resource IDs.\n"
        "- Return ONLY the raw JSON — no markdown fences, no explanation\n"
    )

    if standards_context:
        prompt += (
            f"\n--- ORGANIZATION STANDARDS (MANDATORY — the template MUST satisfy ALL of these) ---\n"
            f"{standards_context}\n"
            f"--- END STANDARDS ---\n"
        )

    if governance_context:
        prompt += (
            f"\n--- SECURITY & GOVERNANCE REQUIREMENTS (MANDATORY — CISO will block non-compliant templates) ---\n"
            f"{governance_context}\n"
            f"--- END SECURITY REQUIREMENTS ---\n"
        )

    from src.copilot_helpers import copilot_send
    from src.agents import ARM_GENERATOR

    max_attempts = 3
    last_error = ""

    for attempt in range(1, max_attempts + 1):
        try:
            actual_prompt = prompt
            if attempt > 1:
                actual_prompt += (
                    f"\n\nPREVIOUS ATTEMPT FAILED: {last_error}\n"
                    "Return ONLY the raw JSON object starting with {{ and ending with }}. No markdown fences, no explanation text, no comments."
                )

            raw = await copilot_send(
                copilot_client,
                model=model,
                system_prompt=ARM_GENERATOR.system_prompt,
                prompt=actual_prompt,
                timeout=60,
                agent_name="ARM_GENERATOR",
            )
            result = _extract_json_from_llm_response(raw)

            parsed = json.loads(result)

            if not isinstance(parsed, dict) or ("resources" not in parsed and "$schema" not in parsed):
                raise json.JSONDecodeError("Response is valid JSON but not an ARM template", result, 0)

            _generated_types = [
                r.get("type", "").lower()
                for r in parsed.get("resources", [])
                if isinstance(r, dict) and r.get("type")
            ]
            _expected_type = resource_type.lower()
            _expected_parent = "/".join(resource_type.split("/")[:2]).lower() if resource_type.count("/") >= 2 else None
            _type_match = any(
                _expected_type in t or (_expected_parent and _expected_parent in t)
                for t in _generated_types
            )
            if not _type_match and _generated_types:
                raise ValueError(
                    f"Generated template contains {_generated_types} but expected "
                    f"'{resource_type}'. The template MUST include a resource of type '{resource_type}'. Do not generate unrelated resource types."
                )

            logger.info(f"Copilot generated ARM template for {resource_type} (attempt {attempt})")
            return result

        except (json.JSONDecodeError, ValueError) as e:
            last_error = str(e)
            logger.warning(f"ARM generation attempt {attempt}/{max_attempts} for {resource_type} failed: {last_error}")
            if attempt == max_attempts:
                logger.error(f"Copilot returned invalid JSON for {resource_type} after {max_attempts} attempts")
                raise ValueError(f"Failed to generate valid ARM template for {resource_type}")


def _extract_json_from_llm_response(text: str) -> str:
    """Extract JSON from an LLM response that may contain markdown fences or extra text."""
    text = text.strip()

    fence_match = re.search(r'```(?:json)?\s*\n?(.*?)```', text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    if text.startswith('{'):
        return text

    brace_start = text.find('{')
    if brace_start != -1:
        depth = 0
        in_string = False
        escape_next = False
        for i in range(brace_start, len(text)):
            c = text[i]
            if escape_next:
                escape_next = False
                continue
            if c == '\\' and in_string:
                escape_next = True
                continue
            if c == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return text[brace_start:i + 1]

    return text
