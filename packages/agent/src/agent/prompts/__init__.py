"""Agent prompts (QNT-58, QNT-149, QNT-156).

The system prompt encodes ADR-003's "interpret, don't calculate" mandate so
the synthesize node sees the boundary on every call, not just in code review.
Kept in its own module so prompt edits don't ripple through ``graph.py``.
"""

from __future__ import annotations

from agent.prompts.system import (
    ANALYST_VOICE_ADR,
    ANALYST_VOICE_BLOCK,
    CLARIFY_SYSTEM_PROMPT,
    COMPARISON_SYSTEM_PROMPT,
    CONVERSATIONAL_SYSTEM_PROMPT,
    EXPLORATION_SYSTEM_PROMPT,
    FOCUSED_SYSTEM_PROMPT,
    FOLLOWUP_SYSTEM_PROMPT,
    HISTORY_TURN_LIMIT,
    NARRATE_SYSTEM_PROMPT,
    NEUTRAL_GREETING_SYSTEM_PROMPT,
    QUICK_FACT_SYSTEM_PROMPT,
    REPORT_TOOLS,
    SYSTEM_PROMPT,
    THESIS_ASPECTS,
    WARM_CONVERSATIONAL_SYSTEM_PROMPT,
    ConversationMessage,
    build_clarify_prompt,
    build_comparison_prompt,
    build_conversational_prompt,
    build_exploration_prompt,
    build_focused_prompt,
    build_followup_prompt,
    build_narrate_prompt,
    build_quick_fact_prompt,
    build_synthesis_prompt,
    trim_message_history,
)

__all__ = [
    "ANALYST_VOICE_ADR",
    "ANALYST_VOICE_BLOCK",
    "CLARIFY_SYSTEM_PROMPT",
    "COMPARISON_SYSTEM_PROMPT",
    "CONVERSATIONAL_SYSTEM_PROMPT",
    "ConversationMessage",
    "EXPLORATION_SYSTEM_PROMPT",
    "FOCUSED_SYSTEM_PROMPT",
    "FOLLOWUP_SYSTEM_PROMPT",
    "HISTORY_TURN_LIMIT",
    "NARRATE_SYSTEM_PROMPT",
    "NEUTRAL_GREETING_SYSTEM_PROMPT",
    "QUICK_FACT_SYSTEM_PROMPT",
    "REPORT_TOOLS",
    "SYSTEM_PROMPT",
    "THESIS_ASPECTS",
    "WARM_CONVERSATIONAL_SYSTEM_PROMPT",
    "build_clarify_prompt",
    "build_comparison_prompt",
    "build_conversational_prompt",
    "build_exploration_prompt",
    "build_focused_prompt",
    "build_followup_prompt",
    "build_narrate_prompt",
    "build_quick_fact_prompt",
    "build_synthesis_prompt",
    "trim_message_history",
]
