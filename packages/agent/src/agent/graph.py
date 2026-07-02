"""LangGraph research agent: classify -> plan -> gather -> synthesize (ADR-007, QNT-149, QNT-156).

The graph is the executive layer of the three-role architecture: it reasons
over pre-computed report strings returned by FastAPI tools and never does
arithmetic or touches the database.

Tools are injected at build time via a ``{name: callable}`` mapping. Tests
pass mock callables; production wiring (QNT-60) passes real HTTP tools.
Keeping tools outside the module makes the graph unit-testable offline.

Pipeline:

1. ``classify`` — pick a response shape from the user's question. Four
   shapes are supported: ``thesis`` (Setup / Bull / Bear / Verdict),
   ``quick_fact`` (short prose + single cited value), ``comparison``
   (per-ticker sections + differences paragraph), and ``conversational``
   (greetings / capability asks / off-domain redirect). Defaults to
   ``thesis`` on any classifier failure so existing eval contracts
   (QNT-67, QNT-128) cannot regress.
2. ``plan`` — pick which report tools to fetch. Bias depends on intent:
   thesis over-fetches, quick_fact narrows, comparison reuses the thesis
   bias for both tickers, conversational skips entirely (no tools needed).
3. ``gather`` — drive the planned tools, retry transient failures, drop
   optional-tool failures silently. For comparison, gathers reports for
   each of the (capped) two tickers.
4. ``synthesize`` — branch on intent. Each path produces its structured
   answer; ANY synthesize-path failure (empty payload, no reports gathered,
   structured-output crash) falls back to a deterministic conversational
   redirect via :func:`agent.conversational.domain_redirect` so the panel
   never sees a stack trace or a blank state.

Exactly one of ``state['thesis']`` / ``state['quick_fact']`` /
``state['comparison']`` / ``state['conversational']`` is populated per run.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict

from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError, field_validator
from shared.tickers import TICKERS

from agent.comparison import ComparisonAnswer, LeanComparisonAnswer, LeanComparisonRow
from agent.conversational import ConversationalAnswer, coerce_suggestions, domain_redirect
from agent.disclaimer import DISCLAIMER
from agent.evals.hallucination import HallucinationResult
from agent.evals.hallucination import check as check_grounding
from agent.exploration import ExplorationAnswer
from agent.focused import FocusedAnalysis
from agent.intent import (
    _EXPLORATION_TRIGGERS,
    ClassifierSource,
    Intent,
    classify_intent_with_source,
    extract_tickers,
    has_comparison_phrase,
    underspecified_gesture,
)
from agent.llm import SMALL_NODE_ALIAS, get_llm
from agent.prompts import (
    HISTORY_TURN_LIMIT,
    REPORT_TOOLS,
    RETRIEVED_EARNINGS_HEADING,
    RETRIEVED_NEWS_HEADING,
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
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)

ToolFn = Callable[[str], str]
# QNT-222: search_news has a two-arg (ticker, query) signature that does not
# fit the single-arg ``ToolFn`` plan-surface dispatch, so it travels as its own
# typed callable rather than as a ``REPORT_TOOLS`` entry.
SearchToolFn = Callable[[str, str], str]
# QNT-224: lean comparison-metrics fetch takes the full ticker list (2-4) in one
# call and returns the JSON metrics payload; like search_news it travels outside
# the single-arg ``ToolFn`` plan-surface map.
ComparisonMetricsToolFn = Callable[[list[str]], str]
ReportToolName = Literal["company", "technical", "fundamental", "news"]

# QNT-224: N-way comparison band. 2 tickers keep the rich four-aspect bundle
# (unchanged); 3-4 take the lean metrics-table path; 5+ redirect.
_MIN_COMPARISON_TICKERS = 2
_MAX_COMPARISON_TICKERS = 4


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
    prompt: list[Any],
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


# QNT-159: optional event emitter for streaming intermediate state out of the
# graph as it runs, NOT after it completes. The classify node fires this with
# ``("intent", {"intent": <intent>})`` as soon as the classifier resolves so
# the SSE wrapper can surface the routing decision before the first tool_call
# event lands. Mirrors the tool-instrumentation pattern in
# ``api.routers.agent_chat._instrument_tools`` (worker thread → asyncio queue
# via ``loop.call_soon_threadsafe``); keeping it as a typed alias here makes
# the SSE adapter's responsibility explicit. None means "no streaming" — the
# CLI and unit tests don't pass one, the SSE endpoint always does.
EventEmitter = Callable[[str, dict[str, object]], None]

# Tool registry is canonical in ``agent.prompts.system`` so the citation list
# in SYSTEM_PROMPT and the dispatch list here can't drift. ``summary`` is
# intentionally omitted from the trio: plan/gather select from what the thesis
# composes over; adding summary is a QNT-60 call if the eval shows it improves
# grounding.

# News is optional — Qdrant or the news ingest can be down without invalidating
# the thesis. Technical & fundamental are load-bearing.
OPTIONAL_TOOLS: frozenset[str] = frozenset({"news"})

# QNT-176: focused-analysis intent → matching report family. The plan node
# narrows to ``["company", <report>]`` for these intents (company grounds
# qualitative business context per QNT-175; the matching report carries
# the numbers). News is the report family for the news focus
# even though it is in OPTIONAL_TOOLS — if news is down the synthesize
# node falls back to a domain redirect, same as any other empty-reports
# failure.
_FOCUSED_REPORT: dict[Intent, str] = {
    "fundamental": "fundamental",
    "technical": "technical",
    "news": "news",
}

# QNT-222 follow-up: intents whose synthesis consumes the news report, so a
# semantic-news-search hit (folded into ``reports["news"]``) actually reaches
# the prompt. The classifier's ``needs_news_search`` flag is intent-independent,
# but fundamental/technical focused reads are forbidden from citing news
# (FOCUSED_SYSTEM_PROMPT rule 3), so firing search there would be a wasted
# Qdrant call. Gate the RAG fetch to the intents that can use it.
_NEWS_SEARCH_INTENTS: frozenset[Intent] = frozenset({"news", "quick_fact", "thesis"})

# QNT-263: intents whose synthesis consumes the fundamental report, so an
# earnings-release hit (folded into ``reports["fundamental"]``) actually reaches
# the prompt. The earnings narrative is management framing + guidance -- a
# fundamental-flavoured read -- so it belongs with the fundamental slot. This is
# the earnings analogue of _NEWS_SEARCH_INTENTS: fire the RAG fetch only where
# the synthesis can use it. ``quick_fact`` is included (QNT-263 follow-up) for
# the same reason it sits in _NEWS_SEARCH_INTENTS -- ``build_quick_fact_prompt``
# renders every ``reports`` key and the quick-fact citation vocabulary already
# allows ``(source: fundamental)``, so a natural single-fact earnings ask ("what
# did management say about guidance?", which classifies as quick_fact) reaches
# the 8-K corpus instead of only the news headlines. ``news`` stays EXCLUDED: a
# focused news read is forbidden from citing the fundamental report
# (FOCUSED_SYSTEM_PROMPT rule 3), so firing there would be a wasted Qdrant call;
# ``technical`` likewise never gathers fundamental.
_EARNINGS_SEARCH_INTENTS: frozenset[Intent] = frozenset({"fundamental", "thesis", "quick_fact"})

_MAX_TOOL_ATTEMPTS = 2  # first try + one retry
_EXPLORATION_EXCLUSIONS: tuple[str, ...] = (
    # These are named lens or warm-follow-up requests. Let the existing
    # focused/followup paths handle them so exploration only owns broad scans.
    "news angle",
    "fundamental angle",
    "technical angle",
    "valuation angle",
    "chart angle",
    "headline",
    "catalyst",
    "drill into",
    "dig into",
    "go deeper",
)
_EXPLORATION_NAMED_LENS_TERMS: tuple[str, ...] = (
    "technically",
    "technical",
    "fundamentally",
    "fundamental",
    "valuation",
    "chart",
    "news angle",
    "headline",
    "headlines",
    "catalyst",
    "catalysts",
)

# QNT-212: intents that need a named ticker (or hydrated prior turn) to
# answer non-fabricated. With no ticker AND no prior turn we route to
# clarify rather than ship a thesis built on a placeholder.
_TICKER_REQUIRING_INTENTS: frozenset[Intent] = frozenset(
    {"thesis", "quick_fact", "fundamental", "technical", "news"}
)

# QNT-212: short-circuit intents skip plan + gather and route classify
# directly to synthesize. Conversational has no tools to gather; followup
# reuses the checkpointer-hydrated reports verbatim.
_SHORT_CIRCUIT_INTENTS: frozenset[Intent] = frozenset({"conversational", "followup"})

# QNT-220 (#8): intents that force-include the company report (QNT-175) and so
# get the compact company variant when one is supplied. Focused
# fundamental/technical/news asks keep the full report. Exploration (QNT-220
# follow-up) is a hot path whose non-news-led plan includes company, so it
# inherits the compact variant to preserve the lever #8 token savings.
_COMPACT_COMPANY_INTENTS: frozenset[Intent] = frozenset({"thesis", "comparison", "exploration"})

AmbiguityKind = Literal["needs_second_ticker", "needs_ticker", "needs_prior_turn"]

# QNT-212: deterministic fallback prose for when the clarify LLM call fails.
# Reasons must stay digit-free -- ``domain_redirect`` rejects digits at the
# boundary so a future caller cannot trip the hallucination guardrail.
_CLARIFY_FALLBACK_REASON: dict[str, str] = {
    "needs_ticker": "Which ticker did you have in mind?",
    "needs_second_ticker": "Which second ticker should I compare against?",
    "needs_prior_turn": "I don't have an earlier turn on this thread to follow up on.",
}

# QNT-220 follow-up: deterministic clarify lead-in bubbles. A clarify turn
# gathered ZERO reports, so an LLM-narrated bubble invents a stance with nothing
# to ground it (observed in prod: "On balance, the read is constructive for
# NVDA..." on a turn that fetched no data). The lead-in carries no analysis, so
# per ADR-003 (no reasoning => no LLM) it is emitted deterministically: always a
# content-free, engaging, digit-free readiness line that never restates the
# clarify card's question.
_CLARIFY_LEAD_IN: dict[str, str] = {
    "needs_ticker": "Happy to dig into any of the names I track.",
    "needs_second_ticker": "Happy to run that comparison for you.",
    "needs_prior_turn": "Happy to pick this up whenever you're ready.",
}
_CLARIFY_LEAD_IN_DEFAULT = "Happy to dig in."


class AgentState(TypedDict):
    """State carried through the graph.

    ``ticker`` is required at invocation; everything else is filled in by
    nodes as the graph runs. ``intent`` is set by the classify node and
    decides which synthesis branch fires. ``reports`` holds raw report
    strings keyed by tool name (for the primary ticker); ``errors`` records
    tool-name -> error message for any tool that failed after retries.

    Comparison runs add ``comparison_tickers`` (the 2 tickers the user
    asked to contrast, in order) and ``reports_by_ticker`` (per-ticker
    report bundle). The single-ticker ``reports`` dict is still populated
    with the primary ticker's reports so existing consumers (CLI confidence
    line, eval hallucination scorer) keep working.

    Exactly one of ``thesis`` / ``quick_fact`` / ``comparison`` /
    ``conversational`` is populated per run, matching ``intent``.
    """

    ticker: str
    analysis_ticker: NotRequired[str]
    question: NotRequired[str]
    intent: NotRequired[Intent]
    classifier_source: NotRequired[ClassifierSource]
    plan: NotRequired[list[str]]
    plan_rationale: NotRequired[str | None]
    reports: NotRequired[dict[str, str]]
    comparison_tickers: NotRequired[list[str]]
    reports_by_ticker: NotRequired[dict[str, dict[str, str]]]
    errors: NotRequired[dict[str, str]]
    thesis: NotRequired[Thesis | None]
    quick_fact: NotRequired[QuickFactAnswer | None]
    comparison: NotRequired[ComparisonAnswer | None]
    comparison_lean: NotRequired[LeanComparisonAnswer | None]
    conversational: NotRequired[ConversationalAnswer | None]
    focused: NotRequired[FocusedAnalysis | None]
    exploration: NotRequired[ExplorationAnswer | None]
    confidence: NotRequired[float]
    grounding_rate: NotRequired[float]
    grounding_unsupported: NotRequired[list[str]]
    supervisor_iterations: NotRequired[int]
    # QNT-211: streaming analyst-voice paragraph produced by the narrate node
    # AFTER synthesize. Persisted into the checkpointer so a follow-up turn
    # can reference the prior narrative if it wants to. None when narrate
    # was skipped (conversational intent) or when the LLM call failed --
    # the structured card still renders, the bubble degrades.
    narrative: NotRequired[str | None]
    # QNT-216: compact, append-only transcript persisted by the checkpointer.
    # Stores user turns and assistant surface text only; structured payloads
    # are referenced compactly so full card JSON does not bloat prompt prefix.
    messages: NotRequired[list[ConversationMessage]]
    # QNT-212: ambiguity classification produced by classify_node. When set,
    # the conditional edge from classify routes to clarify rather than the
    # normal plan/synthesize path. None ⇒ question was unambiguous.
    ambiguity_kind: NotRequired[AmbiguityKind | None]
    # QNT-222 follow-up: classifier signal for whether to fire the semantic news
    # search (search_news / RAG). Set by classify_node from
    # classify_intent_with_source; read by gather_node. Intent-independent -- a
    # targeted-event ask ("what did the CEO say about the buyback?") gets this
    # even when its intent is quick_fact or thesis rather than news.
    needs_news_search: NotRequired[bool]
    # QNT-226: provenance for the semantic news search. When gather fires
    # search_news (targeted news ask), it stores the retrieved hits here as
    # ``{headline, source, date, url}`` dicts so the SSE wrapper can surface a
    # clickable "Retrieved sources" list to the frontend. Absent when no search
    # ran or it returned nothing. Set by gather_node, read by the SSE endpoint
    # (gated on gather having run so a followup turn doesn't re-emit prior hits).
    retrieved_sources: NotRequired[list[dict[str, str]]]
    # QNT-263 / QNT-280: earnings-corpus routing signal. Set by classify_node
    # from the classify LLM's semantic ``needs_earnings_search`` flag (mirrors
    # needs_news_search; _is_earnings_search keyword decider is the recall
    # floor). When set
    # and the intent reads the fundamental report (_EARNINGS_SEARCH_INTENTS),
    # gather fires search_earnings over the equity_earnings corpus and folds the
    # hits into reports["fundamental"], tagging each retrieved source corpus=
    # "earnings" so provenance distinguishes the corpus a hit came from.
    needs_earnings_search: NotRequired[bool]
    # QNT-289: self-contained retrieval query produced by the classify LLM
    # alongside needs_news_search/needs_earnings_search -- resolves pronouns/
    # ellipses from history ("what about the buyback?" -> "NVDA buyback") so a
    # warm-thread targeted ask doesn't reach Qdrant as a bare, topic-less
    # string. Guardrailed by intent.sanitize_search_query (length cap +
    # hallucinated-entity rejection) before it lands in state. "" means no
    # rewrite was produced/survived the guardrail; gather falls back to the
    # raw question, which is today's behaviour, so this can only add recall.
    search_query: NotRequired[str]
    # QNT-212: ordered list of node names actually visited this turn.
    # Each node writes the FULL accumulated list (not a single element with
    # a reducer) -- a reducer would also accumulate across turns out of the
    # checkpointer, so turn 2 of a followup chain would see turn 1's path
    # prepended. ``_wrap_path`` does the read+append; classify resets at
    # the turn boundary. Surfaces on the SSE done event for latency-path
    # debugging + AC validation.
    intent_path: NotRequired[list[str]]


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


def _is_exploratory_question(question: str) -> bool:
    """Return True for the narrow QNT-215 exploration trigger set."""
    lowered = question.lower()
    return any(trigger in lowered for trigger in _EXPLORATION_TRIGGERS) and not any(
        trigger in lowered for trigger in _EXPLORATION_EXCLUSIONS
    )


def _has_exploration_anchor(
    question: str,
    *,
    has_prior_turn: bool,
) -> bool:
    """Exploration needs an explicit ticker in the question or prior context."""
    return bool(extract_tickers(question) or has_prior_turn)


def _has_named_exploration_lens(question: str) -> bool:
    """Return True when an exploratory phrase also names a specific lens."""
    lowered = question.lower()
    return any(term in lowered for term in _EXPLORATION_NAMED_LENS_TERMS)


def _should_route_exploration(
    intent: Intent,
    question: str,
    *,
    has_prior_turn: bool,
) -> bool:
    """Return True when classify should commit to the exploration shape."""
    if intent in _SHORT_CIRCUIT_INTENTS or intent in {"quick_fact", "comparison"}:
        return False
    return (
        _is_exploratory_question(question)
        and not _has_named_exploration_lens(question)
        and _has_exploration_anchor(question, has_prior_turn=has_prior_turn)
    )


def _minimum_exploration_tools(question: str, available: list[str]) -> int:
    """Broad exploratory asks need a second lens before synthesis."""
    if not available:
        return 0
    return min(2, len(available))


def _is_news_led_exploration(question: str) -> bool:
    """Timely broad scans should start from recent developments."""
    lowered = question.lower()
    return any(
        phrase in lowered
        for phrase in (
            "what's interesting",
            "what is interesting",
            "interesting about",
            "what should i watch",
            "this week",
            "next week",
            "watch",
        )
    )


def _deterministic_exploration_plan(question: str, available: list[str]) -> list[str]:
    """QNT-220 (#4): deterministic broad-exploration tool plan (0 LLM calls).

    The QNT-215 supervisor looped the LLM for one tool decision at a time but was
    content-blind -- ``_build_exploration_prompt`` only ever passed the tool
    *names* gathered so far, never the report *bodies* -- so the surrounding
    deterministic guardrail (min-two-lenses, news-first-when-timely, dedup) is
    what actually shaped the plan. This encodes that guardrail directly: a broad
    scan pulls the minimum complementary lenses, news-first when the ask is
    timely. It reproduces the loop's plans on the exploration goldens while
    cutting up to three LLM calls off the most expensive turn type.
    """
    if not available:
        return []
    if _is_news_led_exploration(question):
        preferred = ("news", "technical", "fundamental", "company")
    else:
        preferred = ("company", "news", "technical", "fundamental")
    ordered = [name for name in preferred if name in available]
    ordered += [name for name in available if name not in ordered]
    return ordered[: _minimum_exploration_tools(question, available)]


def _exploration_rationale(question: str, plan: list[str]) -> str | None:
    """One analyst-voice sentence describing a deterministic exploration scan."""
    if not plan:
        return None
    lenses = ", ".join(plan)
    if _is_news_led_exploration(question):
        return f"Timely broad scan, news-first across {lenses}."
    return f"Broad exploratory scan across {lenses}."


def _coerce_thesis_plan(response: object) -> ThesisPlan | None:
    """Normalise structured-output responses into a ``ThesisPlan``."""
    if isinstance(response, ThesisPlan):
        return response
    if isinstance(response, dict):
        parsed = response.get("parsed")
        if isinstance(parsed, ThesisPlan):
            return parsed
    return None


def _tools_from_thesis_plan(thesis_plan: ThesisPlan, available: list[str]) -> list[str]:
    """Filter a structured thesis plan to registered tools, preserving registry order."""
    chosen = set(thesis_plan.tools)
    plan = [tool for tool in available if tool in chosen]
    if "company" in available and "company" not in plan:
        plan.insert(0, "company")
    return plan if len(plan) >= 2 else list(available)


def _parse_plan(raw: str, available: list[str], intent: Intent = "thesis") -> list[str]:
    """Return the subset of ``available`` named in ``raw``, preserving the
    order in ``available``. Falls back to the full list if parsing yields
    nothing — we'd rather over-fetch than strand the synthesize node.

    QNT-175: enforces the ``company`` rule from the plan prompt as code, not
    just as a textual bias the LLM can ignore. ``thesis`` and ``comparison``
    paths always pull ``company`` when it's available (the static profile
    grounds qualitative claims); ``quick_fact`` always drops it (a one-metric
    answer never reaches for the description / competitor list).
    """
    tokens = {t.strip().lower() for t in raw.replace("\n", ",").split(",") if t.strip()}
    chosen = [t for t in available if t in tokens]
    if not chosen:
        chosen = list(available)
    if "company" in available:
        if intent in ("thesis", "comparison") and "company" not in chosen:
            chosen = [t for t in available if t == "company" or t in chosen]
        elif intent == "quick_fact":
            chosen = [t for t in chosen if t != "company"]
    return chosen


def _confidence_from_reports(reports: dict[str, str], plan: list[str]) -> float:
    """Coverage factor = fraction of planned reports actually gathered."""
    if not plan:
        return 0.0
    present = sum(1 for name in plan if name in reports and not _is_tool_error(reports[name]))
    return round(present / len(plan), 2)


def _grounding_rate(result: HallucinationResult) -> float:
    """Fraction of answer numbers traceable to a fetched report."""
    total = len(result.thesis_numbers)
    if total == 0:
        return 1.0
    supported = total - len(result.unsupported)
    return round(supported / total, 2)


def _composite_confidence(
    coverage: float,
    grounding_rate: float = 1.0,
    freshness: float = 1.0,
) -> float:
    """Answer-groundedness score = coverage x grounding x freshness.

    ``coverage`` is report availability, ``grounding_rate`` is numeric support,
    and ``freshness`` is reserved for report as-of dates. Until report templates
    expose a uniform as-of date, freshness is neutral at 1.0.
    """
    return round(max(0.0, min(1.0, coverage * grounding_rate * freshness)), 2)


def _runtime_report_texts(state: AgentState) -> list[str]:
    """Return report bodies gathered for this run, including comparison reports."""
    reports_by_ticker = state.get("reports_by_ticker") or {}
    if reports_by_ticker:
        flat: list[str] = []
        for ticker_reports in reports_by_ticker.values():
            flat.extend(str(report) for report in ticker_reports.values())
        return flat
    reports = state.get("reports") or {}
    return [str(report) for report in reports.values()]


def _runtime_grounding_check(answer: str, reports: list[str]) -> tuple[HallucinationResult, float]:
    """Advisory runtime numeric grounding check for completed answer text."""
    result = check_grounding(answer, reports)
    return result, _grounding_rate(result)


def _is_tool_error(result: str) -> bool:
    """Stable agent.tools error prefix."""
    return result.startswith("[error]")


def _call_with_retry(tool: ToolFn, ticker: str, name: str) -> tuple[str | None, str | None]:
    """Return (result, error). Retries up to ``_MAX_TOOL_ATTEMPTS`` on exception."""
    last_error: str | None = None
    for attempt in range(1, _MAX_TOOL_ATTEMPTS + 1):
        try:
            return tool(ticker), None
        except Exception as exc:  # noqa: BLE001 — tool errors must not crash the graph
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "gather %s: tool=%s attempt=%d/%d failed: %s",
                ticker,
                name,
                attempt,
                _MAX_TOOL_ATTEMPTS,
                last_error,
            )
    return None, last_error


def _gather_reports(
    ticker: str, plan: list[str], tools: dict[str, ToolFn]
) -> tuple[dict[str, str], dict[str, str]]:
    """Drive the planned tools and return ``(reports, errors)``.

    Optional tools (``OPTIONAL_TOOLS``) are dropped silently on both the
    missing-from-map and retry-exhaustion paths so a routine news outage
    doesn't make the synthesize prompt apologise. Required tools surface in
    ``errors`` either way. Factored out of the gather node closure so the
    branching can be unit-tested without compiling a graph.
    """
    reports: dict[str, str] = {}
    errors: dict[str, str] = {}
    for name in plan:
        optional = name in OPTIONAL_TOOLS
        tool = tools.get(name)
        if tool is None:
            if not optional:
                errors[name] = "tool-not-registered"
            continue
        result, error = _call_with_retry(tool, ticker, name)
        if result is None:
            if not optional:
                errors[name] = error or "failed-after-retries"
            continue
        if _is_tool_error(result):
            if not optional:
                errors[name] = result
            continue
        reports[name] = result
    return reports, errors


# QNT-225/276: per-corpus body budget for a folded retrieved hit. A news body is
# a short Finnhub summary -- 280 chars disambiguates an event from a name-drop
# while bounding the added prompt cost (~5 hits/turn). An earnings chunk is up to
# 900 chars (edgar_feeds._CHUNK_MAX_CHARS) of 8-K guidance prose, and the chunk
# itself is the whole reason the earnings corpus exists; the old single 280 cap
# discarded ~two thirds of every retrieved chunk before the LLM saw it, so
# earnings preserves the full chunk. Whole sentences aren't guaranteed; we cut on
# a word boundary and add an ellipsis.
_NEWS_BODY_MAX_CHARS = 280
_EARNINGS_BODY_MAX_CHARS = 900


def _truncate_body(body: str, max_chars: int = _NEWS_BODY_MAX_CHARS) -> str:
    """Trim a folded hit's body to ``max_chars`` on a word boundary."""
    body = body.strip()
    if len(body) <= max_chars:
        return body
    cut = body[:max_chars].rsplit(" ", 1)[0].rstrip()
    return f"{cut}..."


def _format_search_hits(raw: str) -> str:
    """QNT-222/225: render ``search_news`` JSON rows into a news-report-shaped block.

    ``search_news`` returns ``json.dumps([{headline, source, date, score, url,
    body}, ...])`` on a hit and ``"[]"`` on every degraded path (Qdrant outage,
    HTTP error, empty match set, invalid ticker/query). We render headline +
    date + source as ``"- "`` bullets so the block reads like the canned news
    report the focused-news prompt already consumes (the SSE tool_result
    summary also counts ``"- "`` headline lines). QNT-225: when a row carries ``body`` (the Finnhub
    summary), a truncated copy is indented under the headline so the synthesis
    reads the story, not just the title -- empty for points embedded before
    QNT-225 until they roll out of the 7-day window. Returns ``""`` when there
    is nothing usable so the caller can skip the merge entirely.
    """
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(rows, list) or not rows:
        return ""
    lines: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        headline = str(row.get("headline", "")).strip()
        if not headline:
            continue
        date = str(row.get("date", "")).strip()
        source = str(row.get("source", "")).strip()
        meta = ", ".join(part for part in (source, date) if part)
        lines.append(f"- {headline}" + (f" ({meta})" if meta else ""))
        body = _truncate_body(str(row.get("body", "")))
        if body:
            lines.append(f"  {body}")
    if not lines:
        return ""
    return f"## {RETRIEVED_NEWS_HEADING}\n" + "\n".join(lines)


def _parse_search_sources(raw: str) -> list[dict[str, str]]:
    """QNT-226: extract ``{headline, source, date, url}`` rows from ``search_news`` JSON.

    Mirrors :func:`_format_search_hits` parsing but keeps the structured fields
    (not a markdown block) so the SSE wrapper can surface them as a clickable
    provenance list. ``search_news`` returns ``"[]"`` on every degraded path, so
    a bad/empty payload yields ``[]`` and the caller surfaces no sources. Rows
    with no headline are skipped (nothing to render).
    """
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    sources: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        headline = str(row.get("headline") or "").strip()
        if not headline:
            continue
        sources.append(
            {
                "headline": headline,
                "source": str(row.get("source") or "").strip(),
                "date": str(row.get("date") or "").strip(),
                "url": str(row.get("url") or "").strip(),
                # QNT-263: stamp the corpus so the provenance list distinguishes a
                # news hit from an earnings-release hit (AC2).
                "corpus": "news",
            }
        )
    return sources


def _format_earnings_hits(raw: str) -> str:
    """QNT-263: render ``search_earnings`` JSON rows into a report-shaped block.

    ``search_earnings`` returns ``json.dumps([{title, section, date, score, url,
    text}, ...])`` on a hit and ``"[]"`` on every degraded path. We render each
    chunk as a ``"- "`` bullet (title + section + date) with the truncated chunk
    text indented under it, mirroring :func:`_format_search_hits` so the block
    folds cleanly into the fundamental report the synthesis already consumes.
    Returns ``""`` when there is nothing usable so the caller can skip the merge.
    """
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(rows, list) or not rows:
        return ""
    lines: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title", "")).strip()
        section = str(row.get("section", "")).strip()
        if not title and not section:
            continue
        date = str(row.get("date", "")).strip()
        head = title or section
        meta = ", ".join(part for part in (section if title else "", date) if part)
        lines.append(f"- {head}" + (f" ({meta})" if meta else ""))
        # QNT-276: earnings preserves close to the full ~900-char chunk (vs the
        # 280-char news budget) so the 8-K guidance paragraph reaches the LLM.
        text = _truncate_body(str(row.get("text", "")), _EARNINGS_BODY_MAX_CHARS)
        if text:
            lines.append(f"  {text}")
    if not lines:
        return ""
    return f"## {RETRIEVED_EARNINGS_HEADING}\n" + "\n".join(lines)


def _parse_earnings_sources(raw: str) -> list[dict[str, str]]:
    """QNT-263: extract corpus-tagged provenance rows from ``search_earnings`` JSON.

    Mirrors :func:`_parse_search_sources` but maps the earnings-chunk shape onto
    the same ``{headline, source, date, url, corpus}`` provenance dict the SSE
    wrapper already surfaces — ``title`` -> headline, section -> source — and
    tags ``corpus="earnings"`` so the frontend can label which corpus a cited
    hit came from (AC2). ``search_earnings`` degrades to ``"[]"``, so a bad/empty
    payload yields ``[]``.
    """
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    sources: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        section = str(row.get("section") or "").strip()
        headline = title or section
        if not headline:
            continue
        sources.append(
            {
                "headline": headline,
                "source": section if title else "8-K earnings release",
                "date": str(row.get("date") or "").strip(),
                "url": str(row.get("url") or "").strip(),
                "corpus": "earnings",
            }
        )
    return sources


def _coerce_thesis(response: object) -> Thesis | None:
    """Normalise whatever ``llm.invoke`` hands back into a ``Thesis``.

    Structured-output runnables can return a ``Thesis`` directly, an
    ``include_raw=True`` dict, or — on a parsing failure with some providers
    — an AIMessage whose ``.content`` is JSON. We accept all three so a
    LiteLLM provider quirk doesn't leak into the synthesize node.
    """
    if isinstance(response, Thesis):
        return response
    if isinstance(response, dict):
        # ``with_structured_output(..., include_raw=True)`` shape.
        parsed = response.get("parsed")
        if isinstance(parsed, Thesis):
            return parsed
    return None


def _coerce_quick_fact(response: object) -> QuickFactAnswer | None:
    """Normalise whatever ``llm.invoke`` hands back into a ``QuickFactAnswer``.

    Mirror of :func:`_coerce_thesis` for the quick-fact path — same provider
    quirks apply.
    """
    if isinstance(response, QuickFactAnswer):
        return response
    if isinstance(response, dict):
        parsed = response.get("parsed")
        if isinstance(parsed, QuickFactAnswer):
            return parsed
    return None


def _coerce_comparison(response: object) -> ComparisonAnswer | None:
    """Normalise whatever ``llm.invoke`` hands back into a ``ComparisonAnswer``."""
    if isinstance(response, ComparisonAnswer):
        return response
    if isinstance(response, dict):
        parsed = response.get("parsed")
        if isinstance(parsed, ComparisonAnswer):
            return parsed
    return None


def _build_lean_comparison(
    metrics_json: str | None, tickers: list[str]
) -> LeanComparisonAnswer | None:
    """QNT-224: parse the lean comparison-metrics JSON into a structured answer.

    ``metrics_json`` is the ``{"rows": [...]}`` text gather stashed from the API
    (already in requested-ticker order). Returns None on a missing / malformed /
    empty payload so synthesize can redirect. No arithmetic, no LLM — each row
    is a pre-formatted metrics row copied straight from the API (ADR-003).
    """
    if not metrics_json:
        return None
    try:
        payload = json.loads(metrics_json)
    except (ValueError, TypeError):
        logger.warning("lean comparison: metrics JSON not parseable")
        return None
    raw_rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(raw_rows, list) or not raw_rows:
        return None
    rows: list[LeanComparisonRow] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        try:
            rows.append(LeanComparisonRow.model_validate(raw))
        except ValidationError:
            logger.warning("lean comparison: row failed validation: %r", raw)
            return None
    if len(rows) < _MIN_COMPARISON_TICKERS:
        return None
    return LeanComparisonAnswer(rows=rows)


def _coerce_conversational(response: object) -> ConversationalAnswer | None:
    """Normalise whatever ``llm.invoke`` hands back into a ``ConversationalAnswer``."""
    if isinstance(response, ConversationalAnswer):
        return response
    if isinstance(response, dict):
        parsed = response.get("parsed")
        if isinstance(parsed, ConversationalAnswer):
            return parsed
    return None


# QNT-244: map each clarify ambiguity kind to the suggestion-bank hint used
# when the LLM's suggestions are rejected. needs_second_ticker wants concrete
# covered pairs; the others take a balanced mix (needs_prior_turn typically
# carries no suggestions at all, which coerce_suggestions leaves empty).
_CLARIFY_SUGGESTION_HINT: dict[str, str | None] = {
    "needs_second_ticker": "comparison",
    "needs_ticker": None,
    "needs_prior_turn": None,
}


def _with_coerced_suggestions(
    answer: ConversationalAnswer, *, hint: str | None
) -> ConversationalAnswer:
    """Return ``answer`` with its suggestions normalised to the QNT-244 contract.

    Keeps the LLM-generated prose untouched; only the clickable suggestions are
    validated/replaced. Returns the same object when nothing changed so the
    common (already-valid) path avoids a needless copy.
    """
    coerced = coerce_suggestions(answer.suggestions, hint=hint)
    if coerced == answer.suggestions:
        return answer
    return answer.model_copy(update={"suggestions": coerced})


def _coerce_focused(response: object) -> FocusedAnalysis | None:
    """Normalise whatever ``llm.invoke`` hands back into a ``FocusedAnalysis``."""
    if isinstance(response, FocusedAnalysis):
        return response
    if isinstance(response, dict):
        parsed = response.get("parsed")
        if isinstance(parsed, FocusedAnalysis):
            return parsed
    return None


def _coerce_exploration(response: object) -> ExplorationAnswer | None:
    """Normalise whatever ``llm.invoke`` hands back into an ``ExplorationAnswer``."""
    if isinstance(response, ExplorationAnswer):
        return response
    if isinstance(response, dict):
        parsed = response.get("parsed")
        if isinstance(parsed, ExplorationAnswer):
            return parsed
    return None


def _detect_ambiguity(
    intent: Intent,
    question: str,
    *,
    has_prior_turn: bool,
    has_context_ticker: bool = False,
    context_ticker: str | None = None,
) -> AmbiguityKind | None:
    """Return the kind of ambiguity in ``question`` for ``intent``, or None.

    QNT-212: heuristic-only check fired by classify_node right after the
    intent resolves. The three triggers map directly to the AC1 scenarios:

    * comparison + no named ticker ⇒ ``needs_second_ticker``. One named
      ticker is enough when a URL-context ticker or prior turn can supply
      the other side, e.g. /ticker/NVDA + "compare to AAPL". This reverses
      the earlier QNT-212 pin by product decision in QNT-233.
    * thesis / focused / quick_fact + no ticker named + no prior turn ⇒
      ``needs_ticker``. Today this would route to a thesis built around
      whatever placeholder state.ticker the request carries and fabricate
      an answer; the new clarify path asks the user to anchor instead.
    * followup + no prior turn ⇒ ``needs_prior_turn``. The followup
      heuristic already requires has_prior_turn=True so this only fires
      when the LLM classifier returns followup on a cold thread — a
      defensive belt-and-braces against a misbehaving classifier.

    Returns None when the question is unambiguous. The conditional edge in
    build_graph routes None ⇒ plan/synthesize (existing behavior), non-None
    ⇒ clarify.
    """
    question_tickers = extract_tickers(question)
    # QNT-214 follow-up: a bare analysis/compare gesture that names no ticker
    # and has no prior turn is ambiguous regardless of the intent label the
    # classifier returned. The LLM frequently mislabels "what do you think?" /
    # "compare them" as conversational, which would skip the clarify path
    # QNT-212 built for exactly this. Mirror QNT-212: ask back rather than
    # answer on the placeholder ``state.ticker``. A named ticker (handled by
    # the branches below) still gets answered; warm threads keep their prior
    # turn via the ``has_prior_turn`` guard.
    if not question_tickers and not has_prior_turn:
        gesture = underspecified_gesture(question)
        if gesture == "compare":
            return "needs_second_ticker"
        if gesture == "view":
            return "needs_ticker"
    if intent == "comparison" and not question_tickers:
        return "needs_second_ticker"
    context_upper = (context_ticker or "").upper()
    context_adds_second_ticker = (
        has_context_ticker and context_upper in TICKERS and (context_upper not in question_tickers)
    )
    if (
        intent == "comparison"
        and len(question_tickers) < _MIN_COMPARISON_TICKERS
        and not (context_adds_second_ticker or has_prior_turn)
    ):
        return "needs_second_ticker"
    if intent in _TICKER_REQUIRING_INTENTS and not question_tickers and not has_prior_turn:
        return "needs_ticker"
    if intent == "followup" and not has_prior_turn:
        return "needs_prior_turn"
    return None


def _resolve_comparison_tickers(primary: str, question: str) -> list[str]:
    """Return up to 4 tickers to compare, in user-named order (QNT-224).

    Ticker symbols mentioned in ``question`` come first (in the order the
    user wrote them). ``primary`` (the URL-derived ticker the chat panel
    sends) is appended ONLY to reach the two-ticker minimum -- so a question
    like "compare to AAPL" fired from /ticker/NVDA still works -- and never to
    inflate a request the user already filled. Without the ``< _MIN`` guard,
    a 2-named compare from /ticker/NVDA ("compare AAPL and MSFT") would gain a
    third (NVDA) and silently flip from the rich 2-ticker card to a lean 3-way
    that includes a ticker the user never named. The list is capped at
    ``_MAX_COMPARISON_TICKERS`` (4): 2 takes the rich four-aspect bundle,
    3-4 the lean metrics table. Five or more named tickers are handled
    upstream (plan_node) as a conversational redirect, so they never reach
    this cap.
    """
    chosen: list[str] = list(extract_tickers(question))
    primary_upper = primary.upper()
    if (
        primary_upper in TICKERS
        and primary_upper not in chosen
        and len(chosen) < _MIN_COMPARISON_TICKERS
    ):
        chosen.append(primary_upper)
    return chosen[:_MAX_COMPARISON_TICKERS]


def _followup_is_metric_ask(question: str) -> bool:
    """Return True if a followup question targets a specific metric.

    Reuses the same quick-fact token list the classifier heuristic uses
    (RSI, P/E, EPS, volume, etc.). A hit means the followup should still
    produce a QuickFactAnswer card; a miss routes to the narrative-only
    path so narrate owns the response and no quick_fact event fires.
    """
    from agent.intent import _QUICK_FACT_TOKENS, _matches_any

    return _matches_any(question.lower(), _QUICK_FACT_TOKENS) is not None


# QNT-232 #13: per-intent history budget. v4's post-ship analysis (QNT-216)
# named conversation-history injection the primary driver of synthesize
# input-token growth (+1,132 tokens/thesis turn). HISTORY_TURN_LIMIT applies
# identically to every intent, but a fresh analytical ask (thesis / quick_fact /
# focused / comparison / exploration) stands on the reports it just gathered and
# rarely needs ten prior turns; only continuations (followup / conversational /
# clarify) genuinely lean on depth. Fresh asks get the trimmed budget; the
# continuation intents keep the full HISTORY_TURN_LIMIT.
_FRESH_ANALYTICAL_HISTORY_TURNS = 3
_DEEP_HISTORY_INTENTS: frozenset[str] = frozenset({"followup", "conversational", "clarify"})


def _history_budget(intent: str) -> int:
    """Max prior turns to inject into a node's prompt prefix for ``intent``."""
    if intent in _DEEP_HISTORY_INTENTS:
        return HISTORY_TURN_LIMIT
    return _FRESH_ANALYTICAL_HISTORY_TURNS


def _history_before_current(
    messages: list[ConversationMessage] | None,
    question: str,
    *,
    max_turns: int = HISTORY_TURN_LIMIT,
) -> list[ConversationMessage]:
    """Return prior transcript, excluding the current user turn if appended.

    ``max_turns`` bounds how many prior user/assistant turns reach the prompt
    prefix; callers pass an intent-aware value via :func:`_history_budget`
    (QNT-232 #13). The routing-only callsite keeps the full default so prior-turn
    detection stays accurate.
    """
    history = trim_message_history(messages, max_turns=max_turns)
    if (
        history
        and history[-1].get("role") == "user"
        and history[-1].get("content") == question.strip()
    ):
        return history[:-1]
    return history


def _prior_turn_context(state: AgentState, question: str) -> tuple[list[ConversationMessage], bool]:
    """Return transcript context plus the canonical prior-turn boolean."""
    history = _history_before_current(state.get("messages"), question)
    return history, bool(history or state.get("reports") or state.get("thesis"))


def _append_user_message(
    messages: list[ConversationMessage] | None,
    question: str,
) -> list[ConversationMessage]:
    """Append the current user turn to the compact transcript."""
    content = question.strip()
    if not content:
        return trim_message_history(messages)
    return trim_message_history(
        [*trim_message_history(messages), {"role": "user", "content": content}]
    )


def _resolve_single_ticker_context(
    *,
    current_ticker: str,
    question: str,
    intent: Intent,
    prior_ticker: str | None,
) -> str:
    """Return the ticker single-name analytical paths should use.

    A question-named ticker beats the URL-context ticker for single-ticker
    intents. Comparison keeps its separate resolver because two-or-more names
    have distinct semantics there. Bare followups inherit the last analytical
    ticker stored in the checkpoint so a rebased turn stays coherent.

    QNT-245 boundary (older-turn re-gather): a bare followup inherits ONLY the
    MOST-RECENT analysis_ticker. Within one ticker-agnostic conversation thread,
    a followup that gestures at an EARLIER turn's ticker ("go back to NVDA"
    after the subject moved to AMZN) names NVDA, so it routes as a fresh NVDA
    ask and RE-GATHERS — it does not reuse NVDA's prior reports. ``reports`` /
    ``reports_by_ticker`` / ``thesis`` are last-write-wins in the checkpoint
    (gather overwrites them on each non-followup turn; only the followup branch
    in plan_node deliberately preserves them), so the older ticker's reports are
    gone once a newer single-ticker turn lands.
    This is accepted by design: we re-gather rather than maintain a per-ticker
    report cache. Cross-ticker continuity is provided by the shared thread +
    transcript, not by cached per-ticker reports.
    """
    current = current_ticker.upper()
    named = extract_tickers(question)
    if intent != "comparison" and len(named) == 1:
        return named[0]
    prior = (prior_ticker or "").upper()
    if intent == "followup" and not named and prior in TICKERS:
        return prior
    return current


def _strip_disclaimer(markdown: str) -> str:
    """Remove the rendered footer before narrate treats markdown as substrate."""
    return markdown.replace(DISCLAIMER, "").strip()


def _assistant_surface(state: AgentState, narrative: str | None) -> str | None:
    """Compact assistant transcript entry for the completed turn."""
    if narrative:
        prefix = narrative.strip()
    else:
        prefix = ""

    conversational = state.get("conversational")
    if conversational is not None:
        answer = getattr(conversational, "answer", "")
        return (prefix or str(answer)).strip() or None

    quick_fact = state.get("quick_fact")
    if quick_fact is not None:
        answer = getattr(quick_fact, "answer", "")
        ref = "Structured payload: quick_fact"
        return "\n".join(part for part in (prefix or str(answer), ref) if part).strip()

    focused = state.get("focused")
    if focused is not None:
        focus = getattr(focused, "focus", "focused")
        summary = getattr(focused, "summary", "")
        ref = f"Structured payload: focused {focus}"
        return "\n".join(part for part in (prefix or str(summary), ref) if part).strip()

    exploration = state.get("exploration")
    if exploration is not None:
        headline = getattr(exploration, "headline", "")
        ref = "Structured payload: exploration"
        return "\n".join(part for part in (prefix or str(headline), ref) if part).strip()

    comparison = state.get("comparison")
    if comparison is not None:
        differences = getattr(comparison, "differences", "")
        return "\n".join(
            part for part in (prefix or str(differences), "Structured payload: comparison") if part
        ).strip()

    comparison_lean = state.get("comparison_lean")
    if comparison_lean is not None:
        # QNT-224: the lean shape has no differences field — the spoken
        # contrast is the narrative. Carry it (or a payload marker) so a
        # followup turn has a transcript anchor.
        ref = "Structured payload: comparison_lean"
        return "\n".join(part for part in (prefix, ref) if part).strip()

    thesis = state.get("thesis")
    if thesis is not None:
        verdict = getattr(thesis, "verdict", "thesis")
        rationale = getattr(thesis, "verdict_rationale", "")
        ref = f"Structured payload: thesis verdict={verdict}"
        return "\n".join(part for part in (prefix or str(rationale), ref) if part).strip()

    return prefix or None


def _append_assistant_message(
    state: AgentState,
    narrative: str | None,
) -> list[ConversationMessage]:
    """Append the assistant surface for this turn and trim to the history limit."""
    surface = _assistant_surface(state, narrative)
    if not surface:
        return trim_message_history(state.get("messages"))
    return trim_message_history(
        [*trim_message_history(state.get("messages")), {"role": "assistant", "content": surface}]
    )


def _hint_from_intent(intent: Intent) -> str | None:
    """Bucket the intent into a hint label for ``domain_redirect``.

    The redirect's suggestion picker uses the hint to bias toward questions
    matching the user's evident shape. Hints must match a label in
    :data:`agent.conversational._SUGGESTION_BANK` — the bank is keyed by
    report-type / shape (``technical``, ``fundamental``, ``news``,
    ``thesis``, ``comparison``), not by intent name. A bare ``"quick_fact"``
    hint silently degrades because the bank has no such label, so we map
    quick_fact -> ``"technical"`` (where most single-metric asks live —
    RSI, MACD, current price). The conversational intent never invokes
    the fallback (it IS the redirect path), so a None return for it is
    unreachable rather than just harmless.
    """
    if intent == "thesis":
        return "thesis"
    if intent == "quick_fact":
        return "technical"
    if intent == "comparison":
        return "comparison"
    # QNT-176: focused intents map to the matching suggestion-bank label
    # so a synthesize-failure redirect biases toward the same domain the
    # user originally asked about (e.g. failed news → news
    # suggestions, not a random thesis pitch).
    if intent == "fundamental":
        return "fundamental"
    if intent == "technical":
        return "technical"
    if intent == "news":
        return "news"
    # QNT-220 follow-up: a failed exploration scan is broad by nature, so bias
    # the fallback redirect toward the thesis suggestion bank.
    if intent == "exploration":
        return "thesis"
    return None


def build_graph(
    tools: dict[str, ToolFn],
    *,
    event_emitter: EventEmitter | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    compact_company_tool: ToolFn | None = None,
    search_news_tool: SearchToolFn | None = None,
    search_earnings_tool: SearchToolFn | None = None,
    comparison_metrics_tool: ComparisonMetricsToolFn | None = None,
) -> CompiledStateGraph:
    """Compile the classify -> plan -> gather -> synthesize graph (QNT-149, QNT-159).

    Tools are a plain ``{name: callable}`` mapping. Callables take a ticker
    string and return a report string. Exceptions are caught and retried;
    optional tools (see ``OPTIONAL_TOOLS``) are silently dropped after retry
    exhaustion, required tools surface in ``state['errors']``.

    ``event_emitter`` is an optional callable for streaming intermediate
    state to a consumer while the graph runs. Today it's used by the SSE
    endpoint to surface the ``intent`` event from the classify node BEFORE
    the first tool_call lands — without it the panel's streaming label
    would say "streaming thesis…" for the entire tool-gathering phase
    regardless of which intent the classifier picked (the post-graph
    intent emission was too late). None is a no-op, used by the CLI and
    unit tests that read final state directly.

    QNT-220 (#8): ``compact_company_tool`` is an optional callable used for the
    ``company`` slot on the thesis/comparison/exploration hot path (where the
    company report is force-injected per QNT-175). When None, the full
    ``tools['company']`` is used everywhere (existing CLI / eval / test
    behavior). Focused fundamental/company asks always keep the full report.

    QNT-222: ``search_news_tool`` is an optional ``(ticker, query) -> str``
    callable (semantic vector search over the Qdrant equity_news collection).
    It travels outside the ``tools`` map because its two-arg signature does not
    fit the single-arg plan-surface dispatch (``_call_with_retry`` /
    ``_instrument_tools``) and ``default_report_tools`` keeps it off the plan
    surface by design. When the classifier sets ``needs_news_search`` (a
    targeted-event ask -- litigation, CEO, buyback, lawsuit, recall,
    partnership, ... -- judged semantically, independent of intent), gather
    calls it with the user's question as the query and folds the retrieved
    headlines into ``reports["news"]`` for the synthesis to cite. Scoped to
    ``_NEWS_SEARCH_INTENTS`` (news / quick_fact / thesis). Generic news asks
    leave the flag False and keep the canned digest. None ⇒ canned-digest-only
    (CLI / eval / tests).

    QNT-263: ``search_earnings_tool`` is the sibling ``(ticker, query) -> str``
    callable for the second RAG corpus (semantic search over the Qdrant
    equity_earnings collection). Fired by gather only on an earnings-narrative
    ask (``needs_earnings_search`` + ``_EARNINGS_SEARCH_INTENTS``); the retrieved
    release excerpts fold into ``reports["fundamental"]`` and their provenance is
    tagged ``corpus="earnings"``. None ⇒ news-corpus-only (CLI / eval / tests).

    QNT-224: ``comparison_metrics_tool`` is an optional ``(list[str]) -> str``
    callable hitting the lean comparison-metrics endpoint. Fired only on a 3-4
    ticker comparison; the rich two-ticker path never touches it. None ⇒ a 3-4
    way compare degrades to a conversational redirect (CLI / eval / non-wired
    tests); the rich two-ticker path is unaffected.
    """

    def _effective_tools(intent: Intent) -> dict[str, ToolFn]:
        """Swap the compact company tool into the ``company`` slot for the
        thesis/comparison hot path; keep the full report for every other intent.
        Returns the original map untouched when no compact tool was supplied."""
        if (
            compact_company_tool is not None
            and "company" in tools
            and intent in _COMPACT_COMPANY_INTENTS
        ):
            return {**tools, "company": compact_company_tool}
        return tools

    def classify_node(state: AgentState, config: RunnableConfig) -> dict[str, object]:
        # QNT-181: nodes accept ``config`` so the LangGraph CallbackHandler
        # propagates to inner ``llm.invoke(prompt, config=config)`` calls.
        # Without it, generation observations would not nest under the
        # parent agent-chat trace.
        ticker = state["ticker"]
        question = state.get("question", "")
        # ``classify_intent`` already biases to "thesis" on internal LLM
        # failures, but a failure in the surrounding observability stack
        # would propagate and kill the run — same shape as plan_node /
        # synthesize_node which wrap their LLM call in BLE001. Mirror that
        # contract here so the bias-to-thesis invariant the rest of the
        # graph relies on cannot be defeated by an unrelated dependency.
        # QNT-216: prior turn can be detected from the transcript first, with
        # the old QNT-209 reports/thesis hydration kept as backwards-compatible
        # signal for checkpoints created before ``messages`` existed.
        history, has_prior_turn = _prior_turn_context(state, question)
        try:
            (
                intent,
                classifier_source,
                needs_news_search,
                needs_earnings_search,
                search_query,
            ) = classify_intent_with_source(
                question,
                config=config,
                has_prior_turn=has_prior_turn,
                history=history,
            )
        except Exception as exc:  # noqa: BLE001 — preserve the safe default
            logger.warning("classify %s: defaulting to thesis: %s", ticker, exc)
            intent = "thesis"
            classifier_source = "fallback"
            needs_news_search = False
            needs_earnings_search = False
            search_query = ""
        question_tickers = extract_tickers(question)
        if (
            has_comparison_phrase(question)
            and len(question_tickers) == 1
            and ticker.upper() in TICKERS
            and ticker.upper() not in question_tickers
        ):
            intent = "comparison"
        if _should_route_exploration(intent, question, has_prior_turn=has_prior_turn):
            intent = "exploration"
        logger.info(
            "classify %s: intent=%s source=%s needs_news_search=%s needs_earnings_search=%s "
            "search_query=%r",
            ticker,
            intent,
            classifier_source,
            needs_news_search,
            needs_earnings_search,
            search_query,
        )
        # QNT-212: heuristic ambiguity check on the resolved intent. Drives
        # the conditional edge below: a non-None ambiguity_kind routes to
        # clarify; None falls through to the existing plan/synthesize path
        # (or the new conversational/followup short-circuits).
        ambiguity_kind = _detect_ambiguity(
            intent,
            question,
            has_prior_turn=has_prior_turn,
            has_context_ticker=ticker.upper() in TICKERS,
            context_ticker=ticker,
        )
        if ambiguity_kind is not None:
            logger.info(
                "classify %s: ambiguity_kind=%s (intent=%s)",
                ticker,
                ambiguity_kind,
                intent,
            )
        effective_ticker = _resolve_single_ticker_context(
            current_ticker=ticker,
            question=question,
            intent=intent,
            prior_ticker=state.get("analysis_ticker"),
        )
        if effective_ticker != ticker.upper():
            logger.info(
                "classify %s: rebased run to question/context ticker %s (intent=%s)",
                ticker,
                effective_ticker,
                intent,
            )
        # QNT-159: surface the routing decision BEFORE plan/gather/synthesize
        # run. The SSE wrapper provides an emitter that posts to its asyncio
        # queue so the chat panel sees ``intent`` as soon as it's known
        # (rather than after the whole graph completes — see ``_stream`` in
        # api.routers.agent_chat for the post-graph fallback emission, kept
        # as an idempotent safety net for stubbed test graphs that bypass
        # this node).
        if event_emitter is not None:
            try:
                event_emitter("intent", {"intent": intent})
            except Exception as exc:  # noqa: BLE001 — never let SSE plumbing crash the graph
                logger.warning("classify %s: event_emitter failed: %s (continuing)", ticker, exc)
        return {
            "ticker": effective_ticker,
            "analysis_ticker": effective_ticker,
            "intent": intent,
            "classifier_source": classifier_source,
            "ambiguity_kind": ambiguity_kind,
            "needs_news_search": needs_news_search,
            # QNT-263 / QNT-280: earnings-corpus routing. The trigger is now the
            # classify LLM's semantic ``needs_earnings_search`` flag (with the
            # _is_earnings_search keyword decider demoted to a recall floor),
            # resolved alongside the intent in classify_intent_with_source.
            "needs_earnings_search": needs_earnings_search,
            # QNT-289: guardrailed self-contained retrieval query; "" ⇒ gather
            # falls back to the raw question.
            "search_query": search_query,
            "messages": _append_user_message(history, question),
        }

    def plan_node(state: AgentState, config: RunnableConfig) -> dict[str, object]:
        ticker = state["ticker"]
        question = state.get("question", "")
        intent = state.get("intent", "thesis")

        # QNT-209: followup reuses the prior turn's hydrated reports — set
        # plan empty so gather no-ops. Critically we return ONLY ``plan``
        # here; including ``reports`` / ``reports_by_ticker`` in the return
        # dict would clobber the checkpointer-hydrated state with empty
        # values and defeat the whole point of the followup path.
        if intent == "followup":
            logger.info("plan %s: skipped (followup)", ticker)
            return {"plan": []}

        # Conversational path skips tool gathering entirely — the answer
        # comes from the LLM with no report context. We still pass through
        # plan_node so the graph topology stays linear; the gather node
        # then no-ops when ``plan`` is empty.
        if intent == "conversational":
            logger.info("plan %s: skipped (conversational)", ticker)
            return {
                "plan": [],
                "plan_rationale": None,
                "reports": {},
                "errors": {},
                "comparison_tickers": [],
                "reports_by_ticker": {},
            }

        available = [t for t in REPORT_TOOLS if t in tools]
        if not available:
            logger.warning("plan %s: no tools registered", ticker)
            return {
                "plan": [],
                "plan_rationale": None,
                "reports": {},
                "errors": {},
                "comparison_tickers": [],
                "reports_by_ticker": {},
            }

        # Comparison path resolves which two tickers to fetch upfront so the
        # gather node knows the scope. If we can't find two, we still emit a
        # plan (so the synthesize node sees the failure and can route to a
        # conversational redirect with the right hint).
        comparison_tickers: list[str] = []
        if intent == "comparison":
            # QNT-224: 5+ named tickers exceed the lean cap -> redirect. Gate
            # here (not synthesize) so gather never fetches metrics for a set
            # we will refuse. Leaving comparison_tickers empty routes through
            # the existing <2 guard; synthesize re-reads the named count to
            # pick the "too many" vs "couldn't find two" message.
            named = extract_tickers(question)
            if len(named) > _MAX_COMPARISON_TICKERS:
                logger.info(
                    "plan %s: comparison named %d tickers (>%d) — synthesize will redirect",
                    ticker,
                    len(named),
                    _MAX_COMPARISON_TICKERS,
                )
            else:
                comparison_tickers = _resolve_comparison_tickers(ticker, question)
                if len(comparison_tickers) < _MIN_COMPARISON_TICKERS:
                    logger.info(
                        "plan %s: comparison needs 2 tickers, found %s — synthesize will redirect",
                        ticker,
                        comparison_tickers,
                    )

        # Thesis uses a structured-output planner so focused questions avoid
        # irrelevant report calls while still carrying a rationale narrate can
        # optionally surface. Comparison still fetches every available tool:
        # the same plan is run against two tickers, so narrowing can starve
        # one side of the contrast. Quick_fact keeps its older comma-list
        # planner because it only needs a tiny single-metric selection.
        #
        # QNT-176: focused-analysis intents narrow deterministically to
        # ``["company", <matching_report>]``. The user named the domain
        # explicitly; the plan-LLM has nothing to disambiguate.
        if intent in _FOCUSED_REPORT:
            wanted = ("company", _FOCUSED_REPORT[intent])
            plan = [t for t in available if t in wanted]
            plan_rationale = None
        elif intent == "quick_fact":
            prompt = _build_plan_prompt(ticker, question, available, intent)
            # QNT-220 (#7): plan is a small structured/list call -> small alias.
            response = get_llm(temperature=0.0, model_alias=SMALL_NODE_ALIAS).invoke(
                prompt, config=config
            )
            content = response.content if hasattr(response, "content") else str(response)
            plan = _parse_plan(str(content), available, intent)
            plan_rationale = None
        elif intent == "thesis":
            prompt = _build_thesis_plan_prompt(ticker, question, available)
            # QNT-220 (#7): thesis-plan selection is a small structured call -> small alias.
            structured_llm = (
                get_llm(temperature=0.0, model_alias=SMALL_NODE_ALIAS)
                .with_structured_output(ThesisPlan)
                .with_retry(
                    stop_after_attempt=2,
                    retry_if_exception_type=(ValidationError, OutputParserException),
                )
            )
            try:
                response = structured_llm.invoke(prompt, config=config)
            except Exception as exc:  # noqa: BLE001 — fall back to existing deterministic plan
                logger.warning(
                    "plan %s: thesis plan LLM failed for question %r: %s: %s; "
                    "falling back to all tools",
                    ticker,
                    question,
                    type(exc).__name__,
                    exc,
                )
                thesis_plan = None
            else:
                thesis_plan = _coerce_thesis_plan(response)
                if thesis_plan is None:
                    logger.warning(
                        "plan %s: thesis plan LLM returned invalid plan for question %r; "
                        "falling back to all tools",
                        ticker,
                        question,
                    )
            if thesis_plan is None:
                plan = list(available)
                plan_rationale = None
            else:
                plan = _tools_from_thesis_plan(thesis_plan, available)
                plan_rationale = thesis_plan.rationale.strip() or None
        else:
            plan = list(available)
            plan_rationale = None
        logger.info(
            "plan %s: %s (intent=%s, comparison_tickers=%s)",
            ticker,
            plan,
            intent,
            comparison_tickers,
        )
        return {
            "plan": plan,
            "plan_rationale": plan_rationale,
            "reports": {},
            "errors": {},
            "comparison_tickers": comparison_tickers,
            "reports_by_ticker": {},
        }

    def gather_node(state: AgentState, config: RunnableConfig) -> dict[str, object]:  # noqa: ARG001 — config received for LangGraph contract; tools are HTTP, no LLM call
        ticker = state["ticker"]
        intent = state.get("intent", "thesis")
        plan = state.get("plan", [])

        # QNT-209: followup keeps the hydrated reports verbatim. Return an
        # empty dict so TypedDict merge leaves ``reports`` and
        # ``reports_by_ticker`` untouched (AC4: zero tool calls).
        if intent == "followup":
            logger.info("gather %s: skipped (followup)", ticker)
            return {}

        # Conversational path: nothing to gather — keep state intact and
        # let synthesize emit the prose answer.
        if intent == "conversational":
            logger.info("gather %s: skipped (conversational)", ticker)
            return {"reports": {}, "errors": {}, "reports_by_ticker": {}}

        if intent == "comparison":
            comparison_tickers = state.get("comparison_tickers", [])
            if len(comparison_tickers) < _MIN_COMPARISON_TICKERS:
                # Fall through with empty bundle — synthesize will redirect.
                logger.info(
                    "gather %s: comparison needs 2 tickers, got %s",
                    ticker,
                    comparison_tickers,
                )
                return {"reports": {}, "errors": {}, "reports_by_ticker": {}}

            # QNT-224: 3-4 tickers take the lean metrics path — ONE fetch of a
            # compact metrics row per ticker, not a full bundle each. The JSON
            # text is stashed into ``reports`` so _runtime_report_texts (and
            # thus the narrate grounding check) sees the numbers the lean card
            # and narration quote. reports_by_ticker stays empty (no rich
            # bundle), so synthesize routes to the lean branch.
            if len(comparison_tickers) > _MIN_COMPARISON_TICKERS:
                if comparison_metrics_tool is None:
                    logger.info(
                        "gather %s: 3-4 way comparison but no metrics tool wired — redirect",
                        ticker,
                    )
                    return {"reports": {}, "errors": {}, "reports_by_ticker": {}}
                metrics_json = comparison_metrics_tool(comparison_tickers)
                if _is_tool_error(metrics_json):
                    logger.warning("gather %s: comparison-metrics failed: %s", ticker, metrics_json)
                    return {
                        "reports": {},
                        "errors": {"comparison_metrics": metrics_json},
                        "reports_by_ticker": {},
                    }
                logger.info("gather %s: lean comparison metrics for %s", ticker, comparison_tickers)
                return {
                    "reports": {"comparison_metrics": metrics_json},
                    "errors": {},
                    "reports_by_ticker": {},
                }

            reports_by_ticker: dict[str, dict[str, str]] = {}
            errors: dict[str, str] = {}
            effective_tools = _effective_tools(intent)  # compact company on comparison
            for cmp_ticker in comparison_tickers:
                ticker_reports, ticker_errors = _gather_reports(cmp_ticker, plan, effective_tools)
                reports_by_ticker[cmp_ticker] = ticker_reports
                # Tag errors with the ticker prefix so a single tool failing
                # for one ticker doesn't get confused with the same tool
                # failing for the other in the surfaced error map.
                for tool_name, err in ticker_errors.items():
                    errors[f"{cmp_ticker}.{tool_name}"] = err

            primary_reports = reports_by_ticker.get(comparison_tickers[0], {})
            logger.info(
                "gather %s: comparison gathered=%s errors=%s",
                ticker,
                {t: sorted(reports_by_ticker.get(t, {})) for t in comparison_tickers},
                sorted(errors),
            )
            return {
                "reports": primary_reports,
                "errors": errors,
                "reports_by_ticker": reports_by_ticker,
            }

        # QNT-220 (#8): thesis gets the compact company variant when supplied.
        reports, errors = _gather_reports(ticker, plan, _effective_tools(intent))

        # QNT-222: targeted news asks (litigation, CEO, buyback, lawsuit,
        # recall, partnership, ...) additionally pull semantically-relevant
        # headlines via search_news and fold them into the news report so the
        # synthesis can cite them. The trigger is the classifier's
        # intent-independent ``needs_news_search`` flag (QNT-222 follow-up) --
        # so "what did the CEO say about the buyback?" fires it even as a
        # quick_fact, not just on a literal news intent. Scoped to the intents
        # whose synthesis actually reads the news report (_NEWS_SEARCH_INTENTS);
        # generic "news on AAPL" leaves the flag False and keeps the canned
        # digest. search_news never raises (it degrades to "[]"), but we guard
        # defensively so a wrapper bug can't crash gather.
        question = state.get("question", "")
        # QNT-289: the classifier's self-contained rewrite (ticker/entity +
        # topic, pronouns/ellipses resolved from history) is the query when it
        # survived sanitize_search_query; "" falls back to the raw question --
        # today's behaviour, so a warm-thread ellipsis can only gain recall,
        # never lose it.
        retrieval_query = state.get("search_query") or question
        retrieved_sources: list[dict[str, str]] = []
        if (
            state.get("needs_news_search")
            and intent in _NEWS_SEARCH_INTENTS
            and search_news_tool is not None
        ):
            try:
                raw = search_news_tool(ticker, retrieval_query)
            except Exception as exc:  # noqa: BLE001 — search is additive; never crash gather
                logger.warning("gather %s: search_news failed: %s (continuing)", ticker, exc)
                raw = "[]"
            # QNT-226: parse once into both the prompt block (folded into the
            # news report) and the structured provenance list (surfaced to the
            # frontend). Same rows, two renderings.
            retrieved_sources = _parse_search_sources(raw)
            hits = _format_search_hits(raw)
            if hits:
                existing = reports.get("news")
                # QNT-276: retrieved hits LEAD, canned digest follows. The block
                # that specifically matched the question must sit ahead of the
                # generic digest, not below it where the synthesis prompt's
                # "omission is fine" license used to demote it.
                reports["news"] = f"{hits}\n\n{existing}" if existing else hits
                logger.info(
                    "gather %s: folded %d targeted-news hits into news report",
                    ticker,
                    len(retrieved_sources),
                )

        # QNT-263: multi-corpus routing. An earnings-narrative ask (guidance,
        # management framing, outlook) additionally searches the equity_earnings
        # corpus and folds the release excerpts into reports["fundamental"] (the
        # earnings narrative is a fundamental-flavoured read). Gated, like news,
        # on the deterministic flag AND the intents whose synthesis reads the
        # fundamental report (_EARNINGS_SEARCH_INTENTS). Each hit's provenance is
        # tagged corpus="earnings" and appended to the same retrieved_sources
        # list, so the frontend distinguishes which corpus a citation came from.
        if (
            state.get("needs_earnings_search")
            and intent in _EARNINGS_SEARCH_INTENTS
            and search_earnings_tool is not None
        ):
            try:
                raw = search_earnings_tool(ticker, retrieval_query)
            except Exception as exc:  # noqa: BLE001 — search is additive; never crash gather
                logger.warning("gather %s: search_earnings failed: %s (continuing)", ticker, exc)
                raw = "[]"
            earnings_sources = _parse_earnings_sources(raw)
            hits = _format_earnings_hits(raw)
            if hits:
                existing = reports.get("fundamental")
                # QNT-276: retrieved earnings excerpts LEAD, canned fundamental
                # digest follows -- same foregrounding as the news fold above.
                reports["fundamental"] = f"{hits}\n\n{existing}" if existing else hits
                retrieved_sources = retrieved_sources + earnings_sources
                logger.info(
                    "gather %s: folded %d earnings-release hits into fundamental report",
                    ticker,
                    len(earnings_sources),
                )

        logger.info(
            "gather %s: gathered=%s errors=%s",
            ticker,
            sorted(reports),
            sorted(errors),
        )
        return {
            "reports": reports,
            "errors": errors,
            "reports_by_ticker": {},
            "retrieved_sources": retrieved_sources,
        }

    def explore_supervisor_node(state: AgentState, config: RunnableConfig) -> dict[str, object]:  # noqa: ARG001 — config kept for LangGraph node contract; deterministic policy makes no LLM call
        """QNT-215: bounded exploratory tool selection before synthesis.

        This is deliberately an internal route, not a replacement topology:
        classify only sends unambiguous, anchored exploratory turns here. The
        node gathers at most three existing report tools, then hands the
        accumulated reports to the normal synthesize/narrate tail.
        """
        ticker = state["ticker"]
        question = state.get("question", "")
        available = [t for t in REPORT_TOOLS if t in tools]
        reports: dict[str, str] = dict(state.get("reports") or {})

        if not available:
            logger.warning("explore_supervisor %s: no tools registered", ticker)
            return {
                "intent": "thesis",
                "plan": [],
                "plan_rationale": None,
                "reports": {},
                "errors": {},
                "reports_by_ticker": {},
                "supervisor_iterations": 0,
            }

        # QNT-220 (#4): deterministic broad-exploration policy -- 0 LLM calls.
        # The old QNT-215 loop asked the LLM for one tool at a time but never
        # showed it the report bodies, so the deterministic guardrail drove the
        # plan anyway (see _deterministic_exploration_plan). Gather the planned
        # lenses in one shot and hand them to the normal synthesize/narrate tail.
        plan = _deterministic_exploration_plan(question, available)
        # QNT-220 follow-up: a broad anchored scan always renders as the
        # dedicated exploration card -- a verdict-free, multi-lens shape -- so
        # the output intent is constant. "exploration" is in
        # _COMPACT_COMPANY_INTENTS, so the non-news-led [company, news] plan
        # still gets the compact company report (lever #8 savings preserved).
        output_intent: Intent = "exploration"
        tool_reports, errors = _gather_reports(ticker, plan, _effective_tools(output_intent))
        reports.update(tool_reports)
        logger.info(
            "explore_supervisor %s: deterministic plan=%s output_intent=%s gathered=%s errors=%s",
            ticker,
            plan,
            output_intent,
            sorted(tool_reports),
            sorted(errors),
        )

        return {
            "intent": output_intent,
            "plan": plan,
            "plan_rationale": _exploration_rationale(question, plan),
            "reports": reports,
            "errors": errors,
            "reports_by_ticker": {},
            "comparison_tickers": [],
            "supervisor_iterations": len(plan),
            "confidence": _confidence_from_reports(reports, plan),
        }

    def _synthesize_payload(state: AgentState, config: RunnableConfig) -> dict[str, object]:
        ticker = state["ticker"]
        question = state.get("question", "")
        reports = state.get("reports", {})
        plan = state.get("plan", [])
        intent = state.get("intent", "thesis")
        confidence = _confidence_from_reports(reports, plan)
        # QNT-232 #13: intent-aware history budget for every prompt this node
        # assembles (fresh analytical asks trim to a few turns; continuations
        # keep the full HISTORY_TURN_LIMIT).
        history_budget = _history_budget(str(intent))

        # Helper: build the all-None payload skeleton so each branch only has
        # to set its own slot. Keeps consumers free to switch on intent
        # without worrying about stale keys from a previous shape.
        def _empty_payload() -> dict[str, object]:
            return {
                "thesis": None,
                "quick_fact": None,
                "comparison": None,
                "comparison_lean": None,
                "conversational": None,
                "focused": None,
                "exploration": None,
                "confidence": confidence,
            }

        # Helper: deterministic fallback when a path can't produce its
        # primary payload. Used by every branch below — the panel never
        # sees a blank state.
        def _fallback(reason: str) -> dict[str, object]:
            payload = _empty_payload()
            payload["conversational"] = domain_redirect(
                reason=reason,
                tickers=TICKERS,
                hint=_hint_from_intent(intent),
            )
            logger.info(
                "synthesize %s: fallback to conversational redirect (%s)",
                ticker,
                reason,
            )
            return payload

        # QNT-209: followup reasons over the prior turn's hydrated reports
        # via a single LLM call into QuickFactAnswer. We deliberately reuse
        # an existing structured shape rather than mint a new one — the
        # frontend already renders QuickFactAnswer and the AC explicitly
        # forbids introducing a new schema for followup.
        if intent == "followup":
            prior_thesis = state.get("thesis")
            if not reports and not prior_thesis:
                # No prior context to reason over — degrade to the redirect
                # so the user sees an in-domain reply rather than blank.
                return _fallback("I don't have a prior turn on this thread to follow up on.")
            # QNT-211: narrative-only followup path. If the question is pure
            # conversational continuation ("what does that mean for retail?")
            # rather than a metric ask ("what's the P/E?"), skip the
            # QuickFactAnswer LLM call entirely -- narrate owns the response
            # and no quick_fact event fires downstream. The frontend renders
            # the bubble alone, no card.
            if not _followup_is_metric_ask(question):
                logger.info(
                    "synthesize %s: followup narrative-only (no metric ask)",
                    ticker,
                )
                followup_confidence = 1.0 if reports else confidence
                return {"quick_fact": None, "confidence": followup_confidence}
            prompt = build_followup_prompt(
                ticker,
                question,
                reports,
                prior_thesis,
                history=_history_before_current(
                    state.get("messages"), question, max_turns=history_budget
                ),
            )
            structured_llm = (
                get_llm()
                .with_structured_output(QuickFactAnswer)
                .with_retry(
                    stop_after_attempt=2,
                    retry_if_exception_type=(ValidationError, OutputParserException),
                )
            )
            try:
                response = _linked_invoke(structured_llm, prompt, config, "followup-prompt")
            except Exception as exc:  # noqa: BLE001 — fall back to redirect
                logger.warning(
                    "synthesize %s: followup structured output failed: %s: %s",
                    ticker,
                    type(exc).__name__,
                    exc,
                )
                response = None
            followup = _coerce_quick_fact(response)
            if followup is None:
                return _fallback("I had trouble building a follow-up answer for that.")
            # QNT-209: return ONLY the keys this branch owns. Using
            # ``_empty_payload()`` here would write ``thesis=None`` /
            # ``focused=None`` / etc. back to the checkpoint, clobbering
            # the prior turn's hydrated payload — turn 3 of a followup
            # chain would then see ``state['thesis'] is None`` and lose
            # the v2 framing the FOLLOWUP_SYSTEM_PROMPT wants to reference.
            # plan is empty on followup runs, so the report-coverage
            # heuristic would render 0% confidence -- a misleading chip
            # given we reused EVERY hydrated report. Treat reuse as full
            # coverage.
            followup_confidence = 1.0 if reports else confidence
            logger.info("synthesize %s: confidence=%s followup=ok", ticker, followup_confidence)
            return {"quick_fact": followup, "confidence": followup_confidence}

        if intent == "conversational":
            # QNT-217: thread prior conversation into the conversational
            # prompt. When history exists, build_conversational_prompt selects
            # the warm-thread system prompt -- it stays in the latest analysis
            # context and suppresses the cold-start capability card. A fresh
            # thread (no history) keeps the cold capability response.
            prompt = build_conversational_prompt(
                question,
                history=_history_before_current(
                    state.get("messages"), question, max_turns=history_budget
                ),
            )
            structured_llm = (
                get_llm()
                .with_structured_output(ConversationalAnswer)
                .with_retry(
                    stop_after_attempt=2,
                    retry_if_exception_type=(ValidationError, OutputParserException),
                )
            )
            try:
                response = _linked_invoke(structured_llm, prompt, config, "conversational-prompt")
            except Exception as exc:  # noqa: BLE001 — fall back to deterministic redirect
                logger.warning(
                    "synthesize %s: conversational structured output failed: %s: %s",
                    ticker,
                    type(exc).__name__,
                    exc,
                )
                response = None
            conversational = _coerce_conversational(response)
            if conversational is None:
                # Deterministic redirect when the LLM itself fails — the
                # whole point of this path is the user always gets prose.
                return _fallback("I had trouble answering that.")
            # QNT-244: the prose answer is LLM-generated, but the clickable
            # suggestions must be concrete answerable prompts. Replace generic
            # placeholder lists ("trend for a specific stock?") with
            # deterministic in-scope picks so a clicked starter never routes to
            # clarify.
            conversational = _with_coerced_suggestions(conversational, hint=None)
            payload = _empty_payload()
            payload["conversational"] = conversational
            logger.info("synthesize %s: confidence=%s conversational=ok", ticker, confidence)
            return payload

        if intent == "comparison":
            comparison_tickers = state.get("comparison_tickers", [])
            reports_by_ticker = state.get("reports_by_ticker", {})
            if len(comparison_tickers) < _MIN_COMPARISON_TICKERS:
                # QNT-224: distinguish "too many" (5+, plan emptied the list)
                # from "couldn't find two". Re-read the named count so the
                # redirect tells the user the actual constraint.
                if len(extract_tickers(question)) > _MAX_COMPARISON_TICKERS:
                    return _fallback(
                        "I can compare up to four tickers at a time — "
                        "pick the four you care about most."
                    )
                return _fallback(
                    "I can compare two tickers I cover, but I couldn't find two in your question."
                )

            # QNT-224: 3-4 tickers take the lean metrics path. The table is
            # pure pre-computed data (math in SQL, formatted in the API), so
            # per ADR-003 there is NO LLM synthesis call — we parse the metrics
            # JSON gather stashed and build the answer deterministically. The
            # narrate node speaks the qualitative contrast over to_markdown().
            if len(comparison_tickers) > _MIN_COMPARISON_TICKERS:
                metrics_json = (state.get("reports") or {}).get("comparison_metrics")
                lean = _build_lean_comparison(metrics_json, comparison_tickers)
                if lean is None:
                    return _fallback("I couldn't pull comparison metrics right now.")
                payload = _empty_payload()
                payload["comparison_lean"] = lean
                logger.info(
                    "synthesize %s: confidence=%s comparison_lean=%s",
                    ticker,
                    confidence,
                    [r.ticker for r in lean.rows],
                )
                return payload

            # Need at least one report for each ticker — comparing an empty
            # column to anything is just a half thesis.
            if not all(reports_by_ticker.get(t) for t in comparison_tickers):
                return _fallback("I couldn't pull reports for both of those tickers right now.")

            prompt = build_comparison_prompt(
                comparison_tickers,
                question,
                reports_by_ticker,
                history=_history_before_current(
                    state.get("messages"), question, max_turns=history_budget
                ),
            )
            structured_llm = (
                get_llm()
                .with_structured_output(ComparisonAnswer)
                .with_retry(
                    stop_after_attempt=2,
                    retry_if_exception_type=(ValidationError, OutputParserException),
                )
            )
            try:
                response = _linked_invoke(structured_llm, prompt, config, "comparison-prompt")
            except Exception as exc:  # noqa: BLE001 — fall back to redirect
                logger.warning(
                    "synthesize %s: comparison structured output failed: %s: %s",
                    ticker,
                    type(exc).__name__,
                    exc,
                )
                response = None
            comparison = _coerce_comparison(response)
            if comparison is None:
                return _fallback("I had trouble building that comparison.")
            payload = _empty_payload()
            payload["comparison"] = comparison
            logger.info(
                "synthesize %s: confidence=%s comparison=%s",
                ticker,
                confidence,
                [s.ticker for s in comparison.sections],
            )
            return payload

        if intent == "exploration":
            # QNT-220 follow-up: a broad anchored scan rendered as the
            # dedicated verdict-free, multi-lens exploration card. Mirrors the
            # focused path's structured-output + fallback contract.
            if not reports:
                return _fallback("I couldn't pull any reports to scan for that right now.")
            prompt = build_exploration_prompt(
                ticker,
                question,
                reports,
                history=_history_before_current(
                    state.get("messages"), question, max_turns=history_budget
                ),
            )
            structured_llm = (
                get_llm()
                .with_structured_output(ExplorationAnswer)
                .with_retry(
                    stop_after_attempt=2,
                    retry_if_exception_type=(ValidationError, OutputParserException),
                )
            )
            try:
                response = _linked_invoke(structured_llm, prompt, config, "exploration-prompt")
            except Exception as exc:  # noqa: BLE001 — surface as fallback redirect
                logger.warning(
                    "synthesize %s: exploration structured output failed: %s: %s",
                    ticker,
                    type(exc).__name__,
                    exc,
                )
                response = None
            exploration = _coerce_exploration(response)
            if exploration is None:
                return _fallback("I had trouble pulling that scan together.")
            payload = _empty_payload()
            payload["exploration"] = exploration
            logger.info("synthesize %s: confidence=%s exploration=ok", ticker, confidence)
            return payload

        if intent in _FOCUSED_REPORT:
            focus_report = _FOCUSED_REPORT[intent]
            if focus_report not in reports:
                return _fallback(
                    "I couldn't pull a report to answer that focused analysis right now."
                )
            # QNT-226/276: focused read that actually fired a RAG search AND got
            # hits -> lighter shape. Skip the focused-card LLM call and let
            # narrate own the spoken answer (mirrors the QNT-211 followup
            # narrative-only path: synthesize returns the payload slot as None,
            # narrate speaks). The retrieved-sources list renders below the voice
            # as the structured surface, so the user still sees the hits even if
            # narrate degrades. QNT-276 extends this from the news focus
            # (search_news) to the fundamental focus (search_earnings), so an
            # earnings-narrative ask foregrounds the retrieved 8-K excerpt the
            # same way instead of synthesizing a valuation card around it. Gated
            # on retrieved_sources being non-empty: a degraded search (Qdrant
            # down, zero hits) keeps the full focused card, since the canned
            # digest is then the only substrate and the card is the right shape.
            fired_search = (intent == "news" and state.get("needs_news_search")) or (
                intent == "fundamental" and state.get("needs_earnings_search")
            )
            if fired_search and state.get("retrieved_sources"):
                logger.info(
                    "synthesize %s: %s narrative-only (focused card dropped)",
                    ticker,
                    intent,
                )
                return {"focused": None, "confidence": confidence}
            prompt = build_focused_prompt(
                intent,
                ticker,
                question,
                reports,
                history=_history_before_current(
                    state.get("messages"), question, max_turns=history_budget
                ),
            )
            structured_llm = (
                get_llm()
                .with_structured_output(FocusedAnalysis)
                .with_retry(
                    stop_after_attempt=2,
                    retry_if_exception_type=(ValidationError, OutputParserException),
                )
            )
            try:
                response = _linked_invoke(structured_llm, prompt, config, "focused-prompt")
            except Exception as exc:  # noqa: BLE001 — surface as fallback redirect
                logger.warning(
                    "synthesize %s: focused (%s) structured output failed: %s: %s",
                    ticker,
                    intent,
                    type(exc).__name__,
                    exc,
                )
                response = None
            focused = _coerce_focused(response)
            if focused is None:
                return _fallback("I had trouble pulling that focused analysis together.")
            # Re-assert the focus discriminator from intent — defends against
            # a misbehaving provider that echoed the wrong literal back.
            if focused.focus != intent:
                focused = focused.model_copy(update={"focus": intent})
            payload = _empty_payload()
            payload["focused"] = focused
            logger.info(
                "synthesize %s: confidence=%s focused=%s",
                ticker,
                confidence,
                intent,
            )
            return payload

        if intent == "quick_fact":
            if not reports:
                return _fallback("I couldn't pull a report to answer that quick fact right now.")
            prompt = build_quick_fact_prompt(
                ticker,
                question,
                reports,
                history=_history_before_current(
                    state.get("messages"), question, max_turns=history_budget
                ),
            )
            structured_llm = (
                get_llm()
                .with_structured_output(QuickFactAnswer)
                .with_retry(
                    stop_after_attempt=2,
                    retry_if_exception_type=(ValidationError, OutputParserException),
                )
            )
            try:
                response = _linked_invoke(structured_llm, prompt, config, "quick-fact-prompt")
            except Exception as exc:  # noqa: BLE001 — surface as fallback redirect
                logger.warning(
                    "synthesize %s: quick-fact structured output failed: %s: %s",
                    ticker,
                    type(exc).__name__,
                    exc,
                )
                response = None
            quick_fact = _coerce_quick_fact(response)
            if quick_fact is None:
                return _fallback("I had trouble pulling a single answer to that.")
            payload = _empty_payload()
            payload["quick_fact"] = quick_fact
            logger.info(
                "synthesize %s: confidence=%s quick_fact=ok",
                ticker,
                confidence,
            )
            return payload

        # Default thesis path
        if not reports:
            return _fallback("I couldn't pull any reports for that ticker right now.")
        prompt = build_synthesis_prompt(
            ticker,
            question,
            reports,
            history=_history_before_current(
                state.get("messages"), question, max_turns=history_budget
            ),
        )
        # ``with_structured_output(Thesis)`` forces the LLM into the four-section
        # schema. Errors from a misbehaving provider (Gemini occasionally
        # returns malformed tool-call JSON) surface as a fallback redirect
        # rather than crashing the whole run. with_retry recovers transient
        # parse failures (measured at 5.5% on this branch — QNT-196).
        structured_llm = (
            get_llm()
            .with_structured_output(Thesis)
            .with_retry(
                stop_after_attempt=2,
                retry_if_exception_type=(ValidationError, OutputParserException),
            )
        )
        try:
            response = _linked_invoke(structured_llm, prompt, config, "system-prompt")
        except Exception as exc:  # noqa: BLE001 — surface as fallback redirect
            logger.warning(
                "synthesize %s: thesis structured output failed: %s: %s",
                ticker,
                type(exc).__name__,
                exc,
            )
            response = None
        thesis = _coerce_thesis(response)
        if thesis is None:
            return _fallback("I had trouble pulling a thesis together for that.")
        payload = _empty_payload()
        payload["thesis"] = thesis
        logger.info("synthesize %s: confidence=%s thesis=ok", ticker, confidence)
        return payload

    def synthesize_node(state: AgentState, config: RunnableConfig) -> dict[str, object]:
        """QNT-229 #2b: run synthesis, then emit the structured card through the
        ``event_emitter`` the moment it is ready -- BEFORE narrate streams the
        analyst-voice bubble above it.

        Moves the card forward by roughly one narrate duration: the SSE wrapper
        relays the emitted event verbatim, so the panel renders the card while
        the narrative is still streaming. The post-graph emission in
        agent_chat.py stays as the idempotent safety net (the panel's
        ``updateRun`` overwrites with the same payload). Conversational and
        fallback-redirect shapes carry no card slot, so nothing is emitted
        early for them -- their prose still streams via ``prose_chunk``.
        """
        result = _synthesize_payload(state, config)
        if event_emitter is not None and isinstance(result, dict):
            # State key == SSE event name for every card shape; conversational
            # is intentionally excluded (no card, streams as prose_chunk).
            for slot in (
                "thesis",
                "quick_fact",
                "comparison",
                "comparison_lean",
                "focused",
                "exploration",
            ):
                payload = result.get(slot)
                if isinstance(payload, BaseModel):
                    try:
                        event_emitter(slot, payload.model_dump())
                    except Exception as exc:  # noqa: BLE001 — never let SSE plumbing crash synthesize
                        logger.warning(
                            "synthesize %s: card emit (%s) failed: %s (continuing)",
                            state.get("ticker", "?"),
                            slot,
                            exc,
                        )
        return result

    def narrate_node(state: AgentState, config: RunnableConfig) -> dict[str, object]:
        """QNT-211: stream a 1-4 sentence analyst-voice paragraph that wraps
        whichever structured payload synthesize produced.

        Tokens stream out via ``event_emitter("narrative_chunk", {"delta": ...})``
        so the chat panel can render a prose bubble above the card before the
        card composes. On any LLM failure we log, set ``narrative=None``, and
        terminate normally — the structured card still renders.

        Conversational intent skips this node: that path's answer is already
        prose, so re-narrating would just echo it. QNT-232 #3: quick_fact skips
        it too -- its card answer is already analyst-voice prose, so the second
        70b call only paraphrased the card. Followup narrative-only path
        (synthesize set ``quick_fact=None``) routes through here and produces the
        only spoken response the user sees.
        """
        intent = state.get("intent", "thesis")
        ticker = state["ticker"]
        question = state.get("question", "")
        # QNT-232 #13: same intent-aware history budget the synthesize node uses.
        history_budget = _history_budget(str(intent))

        # Conversational already speaks in the right voice -- nothing to
        # narrate over. Same applies to ANY synthesize fallback-redirect
        # path: when synthesize hits _fallback() for a thesis/focused/
        # quick_fact/comparison failure it leaves the original ``intent``
        # intact AND populates ``state['conversational']`` with the
        # deterministic domain_redirect. Without this guard narrate would
        # re-narrate the redirect prose, producing a duplicate bubble above
        # the same conversational card. Gate on the payload, not just the
        # intent.
        #
        # QNT-212: the clarify path also populates ``state['conversational']``
        # (with the asked-back question) but is NOT a fallback -- AC3 wants
        # narrate to fire so the bubble streams above the clarify card.
        # ``ambiguity_kind`` is the distinguishing signal: only the clarify
        # route sets it.
        #
        # QNT-220 follow-up: gate ONLY on ``not is_clarify``. Previously the
        # leading ``intent == "conversational"`` clause meant a clarify turn the
        # classifier happened to label conversational skipped narrate (no
        # bubble), while the same ambiguous question labeled thesis got one --
        # the bubble flickered in/out with the classifier label. A clarify turn
        # always gets the (now non-restating, engaging) lead-in; genuine
        # conversational greetings and synthesize fallback-redirects still skip.
        is_clarify = state.get("ambiguity_kind") is not None
        if not is_clarify and (
            intent == "conversational" or state.get("conversational") is not None
        ):
            return {
                "narrative": None,
                "messages": _append_assistant_message(state, None),
            }

        # QNT-220 follow-up: clarify turns get a DETERMINISTIC lead-in, never an
        # LLM narration. No reports were gathered on a clarify turn, so letting
        # the narrator speak invents a stance (prod: "the read is constructive
        # for NVDA" with zero data). Emit a content-free readiness line keyed to
        # the ambiguity kind; the clarify card below owns the actual question.
        if is_clarify:
            lead_in = _CLARIFY_LEAD_IN.get(
                str(state.get("ambiguity_kind")), _CLARIFY_LEAD_IN_DEFAULT
            )
            if event_emitter is not None:
                try:
                    event_emitter("narrative_chunk", {"delta": lead_in})
                except Exception as exc:  # noqa: BLE001 — never let SSE plumbing crash narrate
                    logger.warning("narrate %s: clarify emit failed: %s (continuing)", ticker, exc)
            return {
                "narrative": lead_in,
                "messages": _append_assistant_message(state, lead_in),
            }

        # QNT-232 #3 (option a): quick_fact skips narrate. The QuickFactAnswer
        # card is already a one-or-two-sentence analyst-voice answer + cited
        # value, so a second 70b call would only paraphrase the card it sits
        # above (the v5 voice/card review measured near-total overlap; quick_fact
        # has no probe close). Dropping it makes a quick_fact turn exactly one
        # default-alias LLM call (synthesize) -- mirrors the conversational gate
        # above. Gated on the card actually landing: a quick_fact whose
        # synthesize failed sets ``conversational`` (caught by the gate above) or
        # leaves quick_fact None, in which case there is no surface to skip for.
        # The followup path reuses QuickFactAnswer but keeps intent="followup",
        # so it is unaffected and still narrates.
        if intent == "quick_fact" and state.get("quick_fact") is not None:
            return {
                "narrative": None,
                "messages": _append_assistant_message(state, None),
            }

        # Pick the structured payload to summarise. Exactly one of these is
        # populated on a successful synthesize; we render whichever it is to
        # markdown so the narrator reads the same thing the panel renders.
        payload_obj: object | None = (
            state.get("thesis")
            or state.get("quick_fact")
            or state.get("comparison")
            or state.get("comparison_lean")
            or state.get("focused")
            or state.get("exploration")
            or state.get("conversational")
        )
        payload_markdown = ""
        to_md: Any = getattr(payload_obj, "to_markdown", None)
        if callable(to_md):
            try:
                payload_markdown = _strip_disclaimer(str(to_md()))
            except Exception:  # noqa: BLE001 — never let formatting kill narrate
                payload_markdown = ""

        # Followup narrative-only path: quick_fact is None but a prior thesis
        # is hydrated. Feed the prior thesis as the substrate the narrator
        # reacts to. For other intents the payload above already carries
        # everything narrate needs.
        prior_thesis_markdown: str | None = None
        if intent == "followup" and not payload_markdown:
            prior_thesis = state.get("thesis")
            prior_to_md: Any = getattr(prior_thesis, "to_markdown", None)
            if callable(prior_to_md):
                try:
                    prior_thesis_markdown = _strip_disclaimer(str(prior_to_md()))
                except Exception:  # noqa: BLE001
                    prior_thesis_markdown = None

        # QNT-226/276: narrative-only substrate. When synthesize dropped the
        # focused card (search fired + hits, focused=None), no structured payload
        # exists, so feed the gathered report -- which now LEADS with the folded
        # RAG block -- as the substrate the narrator speaks from. Without this the
        # prompt would say "no structured payload -- speak from the prior turn"
        # and the narrator would invent an answer with nothing to ground it.
        # news -> reports["news"]; fundamental (earnings) -> reports["fundamental"].
        # The guards mirror the synthesize-side drop condition exactly so this
        # only fires on the path synthesize dropped the card for. The runtime
        # grounding check below runs against the same reports, so ADR-003 numeric
        # grounding still applies to the spoken answer.
        dropped_focus_report: str | None = None
        if not payload_markdown and state.get("retrieved_sources"):
            if intent == "news" and state.get("needs_news_search"):
                dropped_focus_report = "news"
            elif intent == "fundamental" and state.get("needs_earnings_search"):
                dropped_focus_report = "fundamental"
        if dropped_focus_report is not None:
            report_body = (state.get("reports") or {}).get(dropped_focus_report)
            if report_body:
                payload_markdown = str(report_body)

        prompt = build_narrate_prompt(
            intent=str(intent),
            ticker=ticker,
            question=question,
            payload_markdown=payload_markdown,
            prior_thesis_markdown=prior_thesis_markdown,
            plan_rationale=state.get("plan_rationale"),
            history=_history_before_current(
                state.get("messages"), question, max_turns=history_budget
            ),
            is_clarify=is_clarify,
        )

        try:
            chunks: list[str] = []
            stream = get_llm(temperature=0.3).stream(prompt, config=config)
            for chunk in stream:
                delta_obj = getattr(chunk, "content", None)
                if delta_obj is None:
                    continue
                delta = str(delta_obj)
                if not delta:
                    continue
                chunks.append(delta)
                if event_emitter is not None:
                    try:
                        event_emitter("narrative_chunk", {"delta": delta})
                    except Exception as exc:  # noqa: BLE001 — never let SSE plumbing crash narrate
                        logger.warning(
                            "narrate %s: event_emitter failed: %s (continuing)",
                            ticker,
                            exc,
                        )
            narrative = "".join(chunks).strip()
        except Exception as exc:  # noqa: BLE001 — narrate is best-effort; card still renders
            logger.warning(
                "narrate %s: stream failed: %s: %s",
                ticker,
                type(exc).__name__,
                exc,
            )
            return {
                "narrative": None,
                "messages": _append_assistant_message(state, None),
            }

        logger.info("narrate %s: narrative_chars=%d", ticker, len(narrative))
        final_narrative = narrative or None
        coverage = float(state.get("confidence", 0.0))
        grounding_answer = "\n\n".join(
            text for text in (final_narrative, payload_markdown, prior_thesis_markdown) if text
        )
        grounding_result, grounding_rate = _runtime_grounding_check(
            grounding_answer,
            _runtime_report_texts(state),
        )
        confidence = _composite_confidence(coverage, grounding_rate)
        if grounding_result.ok:
            logger.info(
                "narrate %s: grounding_rate=%s confidence=%s",
                ticker,
                grounding_rate,
                confidence,
            )
        else:
            logger.warning(
                "narrate %s: grounding miss rate=%s unsupported=%s confidence=%s",
                ticker,
                grounding_rate,
                grounding_result.reason(),
                confidence,
            )
        return {
            "narrative": final_narrative,
            "messages": _append_assistant_message(state, final_narrative),
            "grounding_rate": grounding_rate,
            "grounding_unsupported": list(grounding_result.unsupported),
            "confidence": confidence,
        }

    def clarify_node(state: AgentState, config: RunnableConfig) -> dict[str, object]:
        """QNT-212: ask the user back when the question is ambiguous.

        Single LLM call into the ConversationalAnswer schema, prompted in the
        ADR-020 analyst voice. Output ``answer`` reads as a clarifying
        question (e.g. "Which ticker did you have in mind?"); ``suggestions``
        carries 0-3 concrete alternatives the user could click. On LLM
        failure the node falls through to a deterministic ``domain_redirect``
        payload so the panel still renders something in-domain — never a
        stack trace.

        Wired by the conditional edge from classify: only reachable when
        ``state['ambiguity_kind']`` is set. Always exits through narrate;
        narrate then short-circuits because ``state['conversational']`` is
        populated (same gate the synthesize-fallback path uses).
        """
        ticker = state["ticker"]
        question = state.get("question", "")
        ambiguity_kind = state.get("ambiguity_kind")

        prompt = build_clarify_prompt(
            ambiguity_kind=str(ambiguity_kind) if ambiguity_kind else "needs_ticker",
            question=question,
            ticker=ticker,
            tickers=TICKERS,
        )
        structured_llm = (
            get_llm()
            .with_structured_output(ConversationalAnswer)
            .with_retry(
                stop_after_attempt=2,
                retry_if_exception_type=(ValidationError, OutputParserException),
            )
        )
        try:
            response = _linked_invoke(structured_llm, prompt, config, "clarify-prompt")
        except Exception as exc:  # noqa: BLE001 — fall through to domain_redirect
            logger.warning(
                "clarify %s: structured output failed: %s: %s",
                ticker,
                type(exc).__name__,
                exc,
            )
            response = None
        conversational = _coerce_conversational(response)
        if conversational is None:
            fallback = domain_redirect(
                reason=_CLARIFY_FALLBACK_REASON.get(
                    str(ambiguity_kind), "I had trouble interpreting that."
                ),
                tickers=TICKERS,
            )
            logger.info(
                "clarify %s: fallback to domain_redirect (%s)",
                ticker,
                ambiguity_kind,
            )
            return {"conversational": fallback}
        # QNT-244: keep clarify suggestions concrete and in-scope. The
        # needs_second_ticker branch biases to comparison pairs; needs_ticker
        # to a balanced mix; needs_prior_turn legitimately carries none.
        conversational = _with_coerced_suggestions(
            conversational, hint=_CLARIFY_SUGGESTION_HINT.get(str(ambiguity_kind))
        )
        logger.info("clarify %s: ambiguity_kind=%s clarify=ok", ticker, ambiguity_kind)
        return {"conversational": conversational}

    def _classify_router(state: AgentState) -> str:
        """QNT-212/QNT-215: pick the next node from classify_node's output.

        Ambiguity always wins -- a clarify run never burns the plan/gather
        LLM call. Conversational and followup short-circuit to synthesize so
        warm thread behavior stays unchanged. QNT-215 exploration owns broad
        anchored scan prompts even when the classifier labels them as news,
        but named-lens, quick_fact, comparison, clarify, and normal follow-up
        flows keep their existing routes.
        """
        if state.get("ambiguity_kind"):
            return "clarify"
        intent = state.get("intent", "thesis")
        if intent in _SHORT_CIRCUIT_INTENTS:
            return "synthesize"
        if intent == "exploration":
            return "explore_supervisor"
        if intent in {"quick_fact", "comparison"}:
            return "plan"
        question = state.get("question", "")
        _, has_prior_turn = _prior_turn_context(state, question)
        if _should_route_exploration(intent, question, has_prior_turn=has_prior_turn):
            return "explore_supervisor"
        return "plan"

    def _wrap_path(
        node_name: str, fn: Callable[..., dict[str, object]]
    ) -> Callable[..., dict[str, object]]:
        """QNT-212: append this node's name to ``intent_path`` after the
        wrapped node returns. Classify is the per-turn entry node, so it
        RESETS the list to ``["classify"]`` -- otherwise a checkpointer-
        hydrated path from the prior turn would leak into the current turn's
        intent_path. Every other node reads ``state["intent_path"]`` (already
        merged with the prior nodes' returns) and appends its own name.

        Done this way instead of with an ``Annotated[list, add]`` reducer
        because the reducer accumulates the prior turn's path too -- the
        checkpointer hydrates state before classify even runs.
        """

        def wrapped(state: AgentState, config: RunnableConfig) -> dict[str, object]:
            result = fn(state, config)
            if not isinstance(result, dict):
                return result
            if node_name == "classify":
                result["intent_path"] = ["classify"]
            else:
                existing = list(state.get("intent_path") or [])
                result["intent_path"] = [*existing, node_name]
            return result

        return wrapped

    builder: StateGraph = StateGraph(AgentState)
    builder.add_node("classify", _wrap_path("classify", classify_node))
    builder.add_node("plan", _wrap_path("plan", plan_node))
    builder.add_node("gather", _wrap_path("gather", gather_node))
    builder.add_node("synthesize", _wrap_path("synthesize", synthesize_node))
    builder.add_node("narrate", _wrap_path("narrate", narrate_node))
    builder.add_node("clarify", _wrap_path("clarify", clarify_node))
    builder.add_node(
        "explore_supervisor",
        _wrap_path("explore_supervisor", explore_supervisor_node),
    )
    builder.add_edge(START, "classify")
    # QNT-212: classify routes by ambiguity / intent rather than always
    # falling through to plan. Three destinations:
    #   - clarify  : ambiguous question (no ticker, only one ticker for a
    #                compare, etc.) — ask back, exit through narrate.
    #   - synthesize: conversational greeting or followup-no-refetch — skip
    #                 plan + gather, save the latency of 2 no-op nodes.
    #   - explore_supervisor: anchored exploratory ask — bounded iterative
    #                         report selection, then normal synthesize/narrate.
    #   - plan     : thesis / focused / quick_fact / comparison — existing
    #                full-pipeline behavior.
    builder.add_conditional_edges(
        "classify",
        _classify_router,
        {
            "clarify": "clarify",
            "synthesize": "synthesize",
            "explore_supervisor": "explore_supervisor",
            "plan": "plan",
        },
    )
    builder.add_edge("plan", "gather")
    # QNT-156: always run synthesize. Empty reports no longer short-circuit
    # to END — synthesize handles every failure surface (no reports, empty
    # payload, structured-output crash) by emitting a deterministic
    # conversational redirect via ``domain_redirect``. The panel never sees
    # a blank state again.
    builder.add_edge("gather", "synthesize")
    builder.add_edge("explore_supervisor", "synthesize")
    # QNT-212: clarify exits through narrate the same way the rest of the
    # pipeline does. narrate sees ``state['conversational']`` is populated
    # and short-circuits (the bubble would duplicate the clarify question),
    # but we keep the edge for topology consistency + intent_path tracking.
    builder.add_edge("clarify", "narrate")
    # QNT-211: narrate streams a 1-4 sentence analyst-voice paragraph above
    # whichever structured shape synthesize produced. Always runs (even on
    # conversational + fallback redirects -- the node short-circuits those
    # internally) so the topology stays linear.
    builder.add_edge("synthesize", "narrate")
    builder.add_edge("narrate", END)
    # QNT-209: passing a checkpointer enables the followup path — the next
    # invocation against the same thread_id sees ``reports`` hydrated from
    # the prior turn. None ⇒ ephemeral compile (curl, tests, non-frontend
    # callers, the QNT-209 ephemeral fallback in the SSE endpoint).
    if checkpointer is not None:
        return builder.compile(checkpointer=checkpointer)
    return builder.compile()


__all__ = [
    "OPTIONAL_TOOLS",
    "REPORT_TOOLS",
    "AgentState",
    "AmbiguityKind",
    "ComparisonAnswer",
    "ConversationalAnswer",
    "EventEmitter",
    "ExplorationAnswer",
    "FocusedAnalysis",
    "Intent",
    "QuickFactAnswer",
    "ReportToolName",
    "Thesis",
    "ThesisPlan",
    "ToolFn",
    "build_graph",
]
