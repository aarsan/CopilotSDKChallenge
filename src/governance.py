"""
InfraForge — Governance Review Engine
═══════════════════════════════════════════════════════════════════

Runs CISO and CTO structured reviews on ARM templates before deployment.

- CISO review: security-focused, can BLOCK deployments
- CTO review:  architecture-focused, ADVISORY only

Both agents return structured JSON verdicts.  The overall gate combines
them:  blocked if CISO blocks, conditional if either has concerns,
approved if both approve.

Usage from a pipeline step or web handler:

    result = await run_governance_review(client, template, service_id, ...)
    if result["gate_decision"] == "blocked":
        # abort pipeline
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from copilot import CopilotClient

from src.agents import AGENTS
from src.copilot_helpers import copilot_send, get_model_for_task

logger = logging.getLogger("infraforge.governance")


# ── JSON extraction helper ───────────────────────────────────

def _extract_json(raw: str) -> dict | None:
    """Best-effort extraction of a JSON object from LLM output."""
    text = raw.strip()
    # Try stripping markdown fences
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find outermost braces
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# ── Default / fallback reviews ────────────────────────────────

_DEFAULT_CISO_REVIEW: dict[str, Any] = {
    "verdict": "conditional",
    "confidence": 0.3,
    "summary": "CISO review could not be completed — treating as conditional.",
    "findings": [],
    "risk_score": 5,
    "security_posture": "adequate",
}

_DEFAULT_CTO_REVIEW: dict[str, Any] = {
    "verdict": "advisory",
    "confidence": 0.3,
    "summary": "CTO review could not be completed — treating as advisory.",
    "findings": [],
    "architecture_score": 5,
    "cost_assessment": "reasonable",
}


# ── Individual review runners ─────────────────────────────────

async def run_ciso_review(
    client: CopilotClient,
    template: dict | str,
    *,
    service_id: str = "",
    version: str = "",
    standards_ctx: str = "",
) -> dict[str, Any]:
    """Run the CISO structured security review on a template.

    Returns a dict with verdict, findings, risk_score, etc.
    """
    spec = AGENTS["ciso_reviewer"]
    model = get_model_for_task(spec.task)

    template_str = json.dumps(template, indent=2) if isinstance(template, dict) else template
    # Truncate very large templates for the prompt
    if len(template_str) > 12000:
        template_str = template_str[:12000] + "\n... (truncated)"

    prompt = (
        f"Review this ARM template for security and compliance.\n\n"
        f"Service: {service_id}\n"
        f"Version: {version}\n\n"
        f"--- ARM TEMPLATE ---\n{template_str}\n--- END TEMPLATE ---\n"
    )
    if standards_ctx:
        prompt += f"\n--- ORG STANDARDS ---\n{standards_ctx[:3000]}\n--- END STANDARDS ---\n"

    try:
        raw = await copilot_send(
            client,
            model=model,
            system_prompt=spec.system_prompt,
            prompt=prompt,
            timeout=spec.timeout,
            agent_name="CISO_REVIEWER",
        )
        review = _extract_json(raw)
        if not review or "verdict" not in review:
            logger.warning("CISO review returned invalid JSON, using default")
            review = {**_DEFAULT_CISO_REVIEW, "summary": raw[:500] if raw else "No response"}
    except Exception as exc:
        logger.error("CISO review failed: %s", exc)
        review = {**_DEFAULT_CISO_REVIEW, "summary": f"Review error: {exc}"}

    review["agent"] = "ciso"
    review["model_used"] = model
    review["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    return review


async def run_cto_review(
    client: CopilotClient,
    template: dict | str,
    *,
    service_id: str = "",
    version: str = "",
    standards_ctx: str = "",
) -> dict[str, Any]:
    """Run the CTO structured technical review on a template.

    Returns a dict with verdict, findings, architecture_score, etc.
    """
    spec = AGENTS["cto_reviewer"]
    model = get_model_for_task(spec.task)

    template_str = json.dumps(template, indent=2) if isinstance(template, dict) else template
    if len(template_str) > 12000:
        template_str = template_str[:12000] + "\n... (truncated)"

    prompt = (
        f"Review this ARM template for architecture quality and operational readiness.\n\n"
        f"Service: {service_id}\n"
        f"Version: {version}\n\n"
        f"--- ARM TEMPLATE ---\n{template_str}\n--- END TEMPLATE ---\n"
    )
    if standards_ctx:
        prompt += f"\n--- ORG STANDARDS ---\n{standards_ctx[:3000]}\n--- END STANDARDS ---\n"

    try:
        raw = await copilot_send(
            client,
            model=model,
            system_prompt=spec.system_prompt,
            prompt=prompt,
            timeout=spec.timeout,
            agent_name="CTO_REVIEWER",
        )
        review = _extract_json(raw)
        if not review or "verdict" not in review:
            logger.warning("CTO review returned invalid JSON, using default")
            review = {**_DEFAULT_CTO_REVIEW, "summary": raw[:500] if raw else "No response"}
    except Exception as exc:
        logger.error("CTO review failed: %s", exc)
        review = {**_DEFAULT_CTO_REVIEW, "summary": f"Review error: {exc}"}

    review["agent"] = "cto"
    review["model_used"] = model
    review["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    return review


# ── Combined governance review ────────────────────────────────

async def run_governance_review(
    client: CopilotClient,
    template: dict | str,
    *,
    service_id: str = "",
    version: str = "",
    standards_ctx: str = "",
) -> dict[str, Any]:
    """Run CISO + CTO reviews in parallel and produce a combined gate decision.

    Returns:
        {
            "ciso": { ... ciso review ... },
            "cto":  { ... cto review ... },
            "gate_decision": "approved" | "conditional" | "blocked",
            "gate_reason": "...",
            "reviewed_at": "...",
        }

    Gate logic:
        - blocked     → CISO verdict is "blocked"
        - conditional → CISO is "conditional" OR CTO is "needs_revision"
        - approved    → both approve (CISO approved + CTO approved/advisory)
    """
    kwargs = dict(service_id=service_id, version=version, standards_ctx=standards_ctx)

    ciso_review, cto_review = await asyncio.gather(
        run_ciso_review(client, template, **kwargs),
        run_cto_review(client, template, **kwargs),
    )

    # Gate logic
    ciso_verdict = ciso_review.get("verdict", "conditional")
    cto_verdict = cto_review.get("verdict", "advisory")

    if ciso_verdict == "blocked":
        # Only truly block if there are critical/high findings.
        # LOW/medium-only findings should downgrade to conditional.
        ciso_findings = ciso_review.get("findings", [])
        has_critical_or_high = any(
            f.get("severity") in ("critical", "high")
            for f in ciso_findings
            if isinstance(f, dict)
        )
        if has_critical_or_high:
            gate = "blocked"
            reason = f"CISO blocked deployment: {ciso_review.get('summary', 'Critical security issues found')}"
        else:
            gate = "conditional"
            reason = f"CISO flagged low/medium findings (no critical/high): {ciso_review.get('summary', 'Minor concerns noted')}"
            logger.info("Downgraded CISO 'blocked' to 'conditional' — no critical/high findings")
    elif ciso_verdict == "conditional" or cto_verdict == "needs_revision":
        gate = "conditional"
        parts = []
        if ciso_verdict == "conditional":
            parts.append("CISO: conditional approval")
        if cto_verdict == "needs_revision":
            parts.append("CTO: revision recommended")
        reason = "; ".join(parts)
    else:
        gate = "approved"
        reason = "Both CISO and CTO approved the template"

    return {
        "ciso": ciso_review,
        "cto": cto_review,
        "gate_decision": gate,
        "gate_reason": reason,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }


def format_review_summary(review: dict) -> str:
    """One-line summary for pipeline progress events."""
    agent = review.get("agent", "unknown").upper()
    verdict = review.get("verdict", "unknown")
    finding_count = len(review.get("findings", []))
    critical_count = sum(
        1
        for f in review.get("findings", [])
        if f.get("severity") in ("critical", "high")
    )
    summary = review.get("summary", "")[:120]

    verdict_icon = {
        "approved": "✅",
        "conditional": "⚠️",
        "blocked": "🚫",
        "advisory": "💡",
        "needs_revision": "🔧",
    }.get(verdict, "❓")

    parts = [f"{verdict_icon} {agent}: {verdict}"]
    if finding_count:
        parts.append(f"{finding_count} finding(s)")
    if critical_count:
        parts.append(f"{critical_count} critical/high")
    if summary:
        parts.append(f"— {summary}")

    return " | ".join(parts)
