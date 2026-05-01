"""Agent prompts (QNT-58, QNT-149, QNT-156).

The system prompt encodes ADR-003's "interpret, don't calculate" mandate so
the synthesize node sees the boundary on every call, not just in code review.
Kept in its own module so prompt edits don't ripple through ``graph.py``.
"""

from __future__ import annotations

from agent.prompts.system import (
    COMPARISON_SYSTEM_PROMPT,
    CONVERSATIONAL_SYSTEM_PROMPT,
    QUICK_FACT_SYSTEM_PROMPT,
    REPORT_TOOLS,
    SYSTEM_PROMPT,
    THESIS_SECTIONS,
    build_comparison_prompt,
    build_conversational_prompt,
    build_quick_fact_prompt,
    build_synthesis_prompt,
)

__all__ = [
    "COMPARISON_SYSTEM_PROMPT",
    "CONVERSATIONAL_SYSTEM_PROMPT",
    "QUICK_FACT_SYSTEM_PROMPT",
    "REPORT_TOOLS",
    "SYSTEM_PROMPT",
    "THESIS_SECTIONS",
    "build_comparison_prompt",
    "build_conversational_prompt",
    "build_quick_fact_prompt",
    "build_synthesis_prompt",
]
