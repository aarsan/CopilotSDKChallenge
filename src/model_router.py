"""
InfraForge — Model Router
═══════════════════════════════════════════════════════════════════

Intentional model selection for every LLM task in the pipeline.

DESIGN PRINCIPLES
─────────────────
1. Not every task needs a flagship model.
2. Reasoning tasks (planning, architecture) need deep-thinking models.
3. Code generation (ARM, Bicep, Terraform) needs strong instruction-following.
4. Quick fixes and classification can use faster, cheaper models.
5. The user-selected "chat model" is independent — it's their preference
   for interactive conversation and doesn't override pipeline routing.

AVAILABLE MODELS (via GitHub Copilot SDK)
─────────────────────────────────────────
┌──────────────────┬───────────┬────────┬──────────────────────────────────────┐
│ Model ID         │ Provider  │ Tier   │ Strength                             │
├──────────────────┼───────────┼────────┼──────────────────────────────────────┤
│ gpt-4.1          │ OpenAI    │ flag   │ Best code gen & instruction follow   │
│ gpt-4.1-mini     │ OpenAI    │ fast   │ Cost-efficient, good for simple      │
│ gpt-4.1-nano     │ OpenAI    │ ultra  │ Ultra-fast, trivial tasks only       │
│ gpt-4o           │ OpenAI    │ flag   │ Multimodal flagship                  │
│ gpt-4o-mini      │ OpenAI    │ fast   │ Smaller, faster GPT-4o              │
│ o3-mini          │ OpenAI    │ reason │ Deep reasoning, chain-of-thought     │
│ claude-sonnet-4  │ Anthropic │ flag   │ Strong reasoning + code gen          │
│ claude-3.5-sonnet│ Anthropic │ flag   │ Previous-gen Anthropic flagship      │
│ gemini-2.0-flash │ Google    │ fast   │ Fast multimodal                      │
└──────────────────┴───────────┴────────┴──────────────────────────────────────┘

TASK → MODEL MAPPING
────────────────────
Each task in the InfraForge pipeline is assigned a model based on what
the task requires — reasoning depth, code quality, or speed.

┌─────────────────────┬──────────────────┬────────────────────────────────────┐
│ Task                │ Model            │ Why                                │
├─────────────────────┼──────────────────┼────────────────────────────────────┤
│ PLANNING            │ o3-mini          │ Architecture planning needs deep   │
│                     │                  │ chain-of-thought reasoning about   │
│                     │                  │ dependencies, security, trade-offs │
├─────────────────────┼──────────────────┼────────────────────────────────────┤
│ CODE_GENERATION     │ claude-sonnet-4  │ ARM/Bicep/Terraform generation     │
│                     │                  │ needs precise, correct code with   │
│                     │                  │ strong instruction adherence       │
├─────────────────────┼──────────────────┼────────────────────────────────────┤
│ CODE_FIXING         │ gpt-4.1          │ Template healing (copilot_fix)     │
│                     │                  │ needs strong instruction following │
│                     │                  │ + error message comprehension      │
├─────────────────────┼──────────────────┼────────────────────────────────────┤
│ POLICY_GENERATION   │ claude-sonnet-4  │ Policy JSON requires precise       │
│                     │                  │ structure + reasoning about what   │
│                     │                  │ the policy should enforce          │
├─────────────────────┼──────────────────┼────────────────────────────────────┤
│ VALIDATION_ANALYSIS │ o3-mini          │ Analyzing What-If results, deploy  │
│                     │                  │ errors, and policy violations      │
│                     │                  │ needs reasoning, not generation    │
├─────────────────────┼──────────────────┼────────────────────────────────────┤
│ CHAT                │ (user-selected)  │ Interactive conversation uses      │
│                     │                  │ whatever the user picked in the UI │
├─────────────────────┼──────────────────┼────────────────────────────────────┤
│ QUICK_CLASSIFY      │ gpt-4.1-nano     │ Simple classification, routing,    │
│                     │                  │ or yes/no decisions — speed wins   │
├─────────────────────┼──────────────────┼────────────────────────────────────┤
│ DESIGN_DOCUMENT     │ gpt-4.1          │ Prose generation for design docs   │
│                     │                  │ needs good writing + structure     │
└─────────────────────┴──────────────────┴────────────────────────────────────┘

VALIDATION: PLAN → EXECUTE
──────────────────────────
The validation pipeline has two distinct cognitive phases:

  PLAN (o3-mini — reasoning)
  │  Analyze the resource type, org standards, and dependencies.
  │  Produce a structured validation plan: what resources to create,
  │  what security configs are mandatory, what to test, what could
  │  go wrong, and what the acceptance criteria are.
  │
  └──▶ The plan is fed into every subsequent phase as context.
       The execution model doesn't need to "figure out" what to
       do — it just follows the plan.

  EXECUTE (claude-sonnet-4 / gpt-4.1 — code gen & fixing)
  │  Generate the ARM template guided by the plan.
  │  Fix any errors guided by the plan's acceptance criteria.
  │  Generate policies guided by the plan's security requirements.
  │
  └──▶ Each fix attempt includes the original plan so the healer
       knows what the template is supposed to achieve.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.config import get_active_model

logger = logging.getLogger(__name__)


# ── Task Types ────────────────────────────────────────────────

class Task(str, Enum):
    """Every distinct LLM task in the InfraForge pipeline."""

    # Deep reasoning — architecture, analysis, planning
    PLANNING            = "planning"
    VALIDATION_ANALYSIS = "validation_analysis"

    # Code generation — ARM, Bicep, Terraform, pipelines
    CODE_GENERATION     = "code_generation"
    POLICY_GENERATION   = "policy_generation"

    # Error recovery — template and policy healing
    CODE_FIXING         = "code_fixing"

    # Interactive — user-facing chat
    CHAT                = "chat"

    # Fast — classification, routing, trivial decisions
    QUICK_CLASSIFY      = "quick_classify"

    # Prose — design documents, summaries
    DESIGN_DOCUMENT     = "design_document"


# ── Model Assignment ─────────────────────────────────────────

@dataclass(frozen=True)
class ModelAssignment:
    """Which model to use for a task, and why."""
    model_id: str
    reason: str


# The routing table — maps each task to its optimal model.
# This is the single source of truth for model selection.
TASK_MODEL_MAP: dict[Task, ModelAssignment] = {
    Task.PLANNING: ModelAssignment(
        model_id="o3-mini",
        reason="Architecture planning requires deep chain-of-thought reasoning "
               "about resource dependencies, security trade-offs, and compliance.",
    ),
    Task.VALIDATION_ANALYSIS: ModelAssignment(
        model_id="o3-mini",
        reason="Analyzing deployment errors and policy violations requires reasoning "
               "about what went wrong and what the correct fix strategy should be.",
    ),
    Task.CODE_GENERATION: ModelAssignment(
        model_id="claude-sonnet-4",
        reason="ARM/Bicep/Terraform generation needs precise, correct code with "
               "strong instruction adherence and minimal hallucination.",
    ),
    Task.POLICY_GENERATION: ModelAssignment(
        model_id="claude-sonnet-4",
        reason="Azure Policy definitions require precise JSON structure combined "
               "with reasoning about what security controls to enforce.",
    ),
    Task.CODE_FIXING: ModelAssignment(
        model_id="gpt-4.1",
        reason="Template healing needs strong error comprehension and the ability "
               "to surgically fix JSON while preserving intent.",
    ),
    Task.CHAT: ModelAssignment(
        model_id="__user_selected__",  # Sentinel — resolved at runtime
        reason="Interactive chat uses whatever model the user selected in the UI.",
    ),
    Task.QUICK_CLASSIFY: ModelAssignment(
        model_id="gpt-4.1-nano",
        reason="Simple classification and routing decisions need speed, not depth.",
    ),
    Task.DESIGN_DOCUMENT: ModelAssignment(
        model_id="gpt-4.1",
        reason="Design documents need clear technical prose with good structure "
               "and consistent formatting.",
    ),
}


def get_model_for_task(task: Task, user_override: Optional[str] = None) -> str:
    """
    Resolve the model ID for a given task.

    Priority:
      1. Explicit user_override (only for CHAT — other tasks use the routing table)
      2. Task-specific model from TASK_MODEL_MAP
      3. Fallback to the active model (should never happen)

    Returns the model ID string.
    """
    assignment = TASK_MODEL_MAP.get(task)

    if not assignment:
        logger.warning(f"No model assignment for task {task}, falling back to active model")
        return get_active_model()

    # For CHAT, always respect the user's selected model
    if task == Task.CHAT:
        return user_override or get_active_model()

    return assignment.model_id


def get_model_display(task: Task, user_override: Optional[str] = None) -> str:
    """Human-readable model label for UI display."""
    model_id = get_model_for_task(task, user_override)
    # Map model IDs to short display names
    display_names = {
        "gpt-4.1":          "GPT-4.1",
        "gpt-4.1-mini":     "GPT-4.1 Mini",
        "gpt-4.1-nano":     "GPT-4.1 Nano",
        "gpt-4o":           "GPT-4o",
        "gpt-4o-mini":      "GPT-4o Mini",
        "o3-mini":          "o3-mini",
        "claude-sonnet-4":  "Claude Sonnet 4",
        "claude-3.5-sonnet":"Claude 3.5 Sonnet",
        "gemini-2.0-flash": "Gemini 2.0 Flash",
    }
    return display_names.get(model_id, model_id)


def get_task_reason(task: Task) -> str:
    """Why this model was chosen for this task — shown in the UI."""
    assignment = TASK_MODEL_MAP.get(task)
    return assignment.reason if assignment else "Default model"


def get_routing_table() -> list[dict]:
    """
    Return the full routing table for the API/UI.
    Shows which model handles which task and why.
    """
    from src.config import AVAILABLE_MODELS
    model_lookup = {m["id"]: m for m in AVAILABLE_MODELS}

    table = []
    for task in Task:
        assignment = TASK_MODEL_MAP.get(task)
        if not assignment:
            continue
        model_id = assignment.model_id
        if model_id == "__user_selected__":
            model_info = {"id": "(user-selected)", "name": "(User's Choice)", "tier": "varies"}
        else:
            model_info = model_lookup.get(model_id, {"id": model_id, "name": model_id, "tier": "unknown"})

        table.append({
            "task": task.value,
            "task_label": task.name.replace("_", " ").title(),
            "model_id": model_info.get("id", model_id),
            "model_name": model_info.get("name", model_id),
            "model_tier": model_info.get("tier", "unknown"),
            "reason": assignment.reason,
        })
    return table
