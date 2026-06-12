"""Single source of truth for the prompt-version hash (QNT-187, QNT-230 #11).

``agent.graph`` and ``agent.evals.golden_set`` both need this hash but cannot
share a module-level import of each other: ``golden_set`` imports ``build_graph``
from ``graph``, and ``graph`` computes the version at import time. Historically
each kept a hand-synced copy and the two had silently drifted (graph hashed nine
system prompts, golden_set only five) -- so a Langfuse ``prompt_version`` filter
meant different things depending on which path wrote the row.

This module imports only the prompt constants + the classifier prompt and takes
the plan-prompt builders as arguments, so it depends on neither ``graph`` nor
``golden_set``; both call it with their own builder references and get the SAME
hash by construction. The classify + plan prompts (QNT-230 #11) are folded in so
a routing-prompt edit shows up as a new version instead of silently reusing the
old one.
"""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256

# Fixed sample inputs for rendering the plan-prompt builders. The interpolated
# values are constant, so they don't affect the hash beyond making the template
# text concrete -- a change to the builder's prose (the bias / scope guidance)
# changes the rendered output and therefore the version.
_PLAN_SAMPLE_TICKER = "TICKER"
_PLAN_SAMPLE_QUESTION = "QUESTION"
_PLAN_SAMPLE_AVAILABLE = ["company", "fundamental", "technical", "news"]


def compute_prompt_version(
    plan_prompt_builder: Callable[..., str],
    thesis_plan_prompt_builder: Callable[..., str],
) -> str:
    """Return a stable 10-char hash of every agent prompt + the tool registry.

    ``plan_prompt_builder`` is ``agent.graph._build_plan_prompt`` and
    ``thesis_plan_prompt_builder`` is ``agent.graph._build_thesis_plan_prompt``;
    both callers pass the same functions so the hash is identical across the
    graph and the golden-set harness.
    """
    from agent.intent import _CLASSIFY_PROMPT
    from agent.prompts import (
        CLARIFY_SYSTEM_PROMPT,
        COMPARISON_SYSTEM_PROMPT,
        CONVERSATIONAL_SYSTEM_PROMPT,
        EXPLORATION_SYSTEM_PROMPT,
        FOCUSED_SYSTEM_PROMPT,
        FOLLOWUP_SYSTEM_PROMPT,
        NEUTRAL_GREETING_SYSTEM_PROMPT,
        QUICK_FACT_SYSTEM_PROMPT,
        REPORT_TOOLS,
        SYSTEM_PROMPT,
        WARM_CONVERSATIONAL_SYSTEM_PROMPT,
    )

    payload = "\n".join(
        (
            SYSTEM_PROMPT,
            QUICK_FACT_SYSTEM_PROMPT,
            COMPARISON_SYSTEM_PROMPT,
            CONVERSATIONAL_SYSTEM_PROMPT,
            WARM_CONVERSATIONAL_SYSTEM_PROMPT,
            NEUTRAL_GREETING_SYSTEM_PROMPT,
            FOCUSED_SYSTEM_PROMPT,
            EXPLORATION_SYSTEM_PROMPT,
            FOLLOWUP_SYSTEM_PROMPT,
            CLARIFY_SYSTEM_PROMPT,
            # QNT-230 #11: classify + plan prompts were outside the hash, so a
            # routing-prompt change shipped with an unchanged prompt_version.
            _CLASSIFY_PROMPT,
            plan_prompt_builder(
                _PLAN_SAMPLE_TICKER, _PLAN_SAMPLE_QUESTION, _PLAN_SAMPLE_AVAILABLE, "quick_fact"
            ),
            plan_prompt_builder(
                _PLAN_SAMPLE_TICKER, _PLAN_SAMPLE_QUESTION, _PLAN_SAMPLE_AVAILABLE, "thesis"
            ),
            thesis_plan_prompt_builder(
                _PLAN_SAMPLE_TICKER, _PLAN_SAMPLE_QUESTION, _PLAN_SAMPLE_AVAILABLE
            ),
            ",".join(sorted(REPORT_TOOLS)),
        )
    )
    return sha256(payload.encode("utf-8")).hexdigest()[:10]


__all__ = ["compute_prompt_version"]
