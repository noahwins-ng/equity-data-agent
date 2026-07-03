"""QNT-294 (AC1): structured-output primitives + plan-prompt builders + prompt version.

Extracted from graph.py. Tier-0 (no graph.py dependency): the shared
``_linked_invoke`` / ``_coerce`` primitives, the plan-prompt builders, and the
module-load ``_PROMPT_VERSION`` hash. ``_structured_call`` itself stays in
graph.py because it calls the ``get_llm`` seam the tests monkeypatch on
``agent.graph``.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field, field_validator

from agent.intent import Intent
from agent.policy import ReportToolName

logger = logging.getLogger("agent.graph")


class ThesisPlan(BaseModel):
    """Structured plan for a thesis run.

    The plan LLM picks a narrow report set, while the code keeps a
    deterministic all-tools fallback if this shape cannot be produced.
    """

    tools: list[ReportToolName] = Field(
        min_length=2,
        max_length=4,
        description="Two to four report tools to fetch. Always include company.",
    )
    rationale: str = Field(
        min_length=1,
        description=(
            "One or two analyst-voice sentences explaining why these tools fit the question."
        ),
    )

    @field_validator("tools")
    @classmethod
    def _validate_tools(cls, value: list[ReportToolName]) -> list[ReportToolName]:
        if len(set(value)) != len(value):
            raise ValueError("tools must be unique")
        if "company" not in value:
            raise ValueError("tools must include company")
        return value


def _prompt_version() -> str:
    """Stable 10-char hash of all agent prompts + tool registry (QNT-187, QNT-230).

    Delegates to :func:`agent.prompt_version.compute_prompt_version`, the single
    source of truth shared with ``agent.evals.golden_set`` -- the two used to
    keep hand-synced copies (circular-import workaround) and had silently
    drifted. Passing the local plan-prompt builders folds the classify + plan
    prompts into the version (QNT-230 #11). Called once at module load, after
    the builders below are defined, and cached in ``_PROMPT_VERSION``.
    """
    from agent.prompt_version import compute_prompt_version

    return compute_prompt_version(_build_plan_prompt, _build_thesis_plan_prompt)


def _linked_invoke(
    runnable: Any,
    prompt: list[Any] | str,
    config: RunnableConfig,
    prompt_name: str,
) -> Any:
    """Invoke ``runnable`` with prompt version metadata + native Langfuse prompt link.

    When Langfuse Prompt Management is available, wraps the pre-built message list
    in a ChatPromptTemplate (via MessagesPlaceholder — no template expansion, safe
    for report content with curly braces) with ``langfuse_prompt`` metadata, then
    chains to ``runnable``. The CallbackHandler reads ``langfuse_prompt`` from the
    PromptTemplate step and creates a native trace → Prompt panel link in Langfuse.

    Falls back to direct invoke when Langfuse keys are unset (CI, local dev).
    Always sets ``prompt_version`` so the version is visible in trace metadata
    regardless of whether native linking is active.
    """
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

    from agent.tracing import get_langfuse_prompt

    prompt_obj = get_langfuse_prompt(prompt_name)
    version = str(prompt_obj.version) if prompt_obj is not None else _PROMPT_VERSION
    existing: dict[str, object] = config.get("metadata") or {}
    cfg: RunnableConfig = {**config, "metadata": {**existing, "prompt_version": version}}

    if prompt_obj is not None:
        template = ChatPromptTemplate.from_messages(
            [MessagesPlaceholder(variable_name="messages")]
        ).with_config(metadata={"langfuse_prompt": prompt_obj})
        chain = template | runnable
        return chain.invoke({"messages": prompt}, config=cfg)

    return runnable.invoke(prompt, config=cfg)


def _coerce[T: BaseModel](response: object, schema: type[T]) -> T | None:
    """Normalise whatever ``llm.invoke`` hands back into an instance of ``schema``.

    QNT-294 (AC5): the one generic coercion that replaces the seven near-identical
    ``_coerce_*`` helpers. Structured-output runnables can return the model
    directly, or -- with ``include_raw=True`` / some provider quirks -- a dict whose
    ``"parsed"`` key carries the model. Accept both so a LiteLLM provider quirk
    doesn't leak into the calling node.
    """
    if isinstance(response, schema):
        return response
    if isinstance(response, dict):
        parsed = response.get("parsed")
        if isinstance(parsed, schema):
            return parsed
    return None


def _build_plan_prompt(
    ticker: str,
    question: str,
    available: list[str],
    intent: Intent = "thesis",
) -> str:
    options = ", ".join(available)
    if intent == "quick_fact":
        # Quick-fact path narrows aggressively — the user asked one question,
        # we want the one report that answers it. Over-fetching is the wrong
        # default here because it pulls news/fundamental tools the question
        # doesn't touch and burns provider quota. ``company`` is explicitly
        # excluded too — single-metric asks don't benefit from a static
        # business profile (QNT-175).
        bias = (
            "The user asked a single-metric question; pick ONLY the report(s) "
            "directly needed to answer it. Omit anything not strictly required, "
            "including the 'company' report — static business context never "
            "answers a single-number question. If unsure, prefer the smallest "
            "plan that can answer the question."
        )
    else:
        # Both ``thesis`` and ``comparison`` over-fetch — the comparison path
        # then re-runs the same plan against each ticker, so a narrow plan
        # would starve the second ticker too. ``company`` is always included:
        # it's the static business-context report (description, competitors,
        # risks, watch metrics) the QNT-175 thesis upgrade leans on for
        # qualitative grounding.
        bias = (
            "Include every report that is even marginally relevant; omit only "
            "reports that are clearly irrelevant to the question. Always "
            "include the 'company' report when it is in the available set — "
            "it grounds the thesis in the company's actual business and is "
            "cheap to fetch."
        )
    return (
        f"You are planning which reports to fetch for an investment analysis of {ticker}.\n"
        f"Question: {question or '(general thesis)'}\n"
        f"Available reports: {options}\n\n"
        "Respond with a comma-separated list of report names to fetch from the available set. "
        f"{bias} Respond with the list only, no prose."
    )


def _build_thesis_plan_prompt(ticker: str, question: str, available: list[str]) -> str:
    """Prompt the thesis planner to choose a narrow, explainable report set."""
    options = ", ".join(available)
    return (
        f"You are planning which reports to fetch for an investment thesis on {ticker}.\n"
        f"Question: {question or '(general thesis)'}\n"
        f"Available reports: {options}\n\n"
        "Pick the available reports that match the user's requested thesis scope. "
        "A broad thesis request means the user wants the full investment picture; "
        "for broad thesis requests, select every available report: company, "
        "fundamental, technical, and news. Do not narrow a broad thesis to only "
        "company and fundamentals. Narrow only when the user explicitly asks for "
        "a specific lens: choose fundamental for valuation, earnings, margins, "
        "or balance-sheet questions; choose technical for chart, trend, momentum, "
        "RSI, or setup questions; choose news for headlines, catalysts, events, "
        "sentiment, or what changed recently. Always include company when it is "
        "available; it is cheap context that anchors the analysis in the business.\n\n"
        "Return a structured plan with:\n"
        "- tools: the selected report names only\n"
        "- rationale: 1-2 analyst-note sentences that cite what the question is asking "
        "about and why these reports match that scope. For a broad thesis, the "
        "rationale should say the question asks for a full thesis, so all reports "
        "are needed. For a narrow lens, example voice: Your question is about "
        "valuation, so I'll use fundamentals and the company profile."
    )


# Computed once at module load — deterministic over a process lifetime.
# Propagated to every LLM call's config metadata so Langfuse traces are
# filterable by prompt version (QNT-187). Defined here, after the plan-prompt
# builders, because ``_prompt_version`` now folds them into the hash (QNT-230).
_PROMPT_VERSION: str = _prompt_version()
