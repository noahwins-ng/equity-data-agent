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

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict

from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError, field_validator
from shared.tickers import TICKERS

from agent.comparison import ComparisonAnswer
from agent.conversational import ConversationalAnswer, domain_redirect
from agent.focused import FocusedAnalysis
from agent.intent import ClassifierSource, Intent, classify_intent_with_source, extract_tickers
from agent.llm import get_llm
from agent.prompts import (
    REPORT_TOOLS,
    ConversationMessage,
    build_clarify_prompt,
    build_comparison_prompt,
    build_conversational_prompt,
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
ReportToolName = Literal["company", "technical", "fundamental", "news"]


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
    """Stable 10-char hash of all system prompts + tool registry.

    Mirrors the implementation in agent.evals.golden_set but lives here to
    avoid a circular import (golden_set imports build_graph). Computed once
    at module load and cached in _PROMPT_VERSION.
    """
    from hashlib import sha256

    from agent.prompts import (
        CLARIFY_SYSTEM_PROMPT,
        COMPARISON_SYSTEM_PROMPT,
        CONVERSATIONAL_SYSTEM_PROMPT,
        FOCUSED_SYSTEM_PROMPT,
        FOLLOWUP_SYSTEM_PROMPT,
        QUICK_FACT_SYSTEM_PROMPT,
        SYSTEM_PROMPT,
    )

    payload = (
        SYSTEM_PROMPT
        + "\n"
        + QUICK_FACT_SYSTEM_PROMPT
        + "\n"
        + COMPARISON_SYSTEM_PROMPT
        + "\n"
        + CONVERSATIONAL_SYSTEM_PROMPT
        + "\n"
        + FOCUSED_SYSTEM_PROMPT
        + "\n"
        + FOLLOWUP_SYSTEM_PROMPT
        + "\n"
        + CLARIFY_SYSTEM_PROMPT
        + "\n"
        + ",".join(sorted(REPORT_TOOLS))
    )
    return sha256(payload.encode("utf-8")).hexdigest()[:10]


# Computed once at module load — deterministic over a process lifetime.
# Propagated to every LLM call's config metadata so Langfuse traces are
# filterable by prompt version (QNT-187).
_PROMPT_VERSION: str = _prompt_version()


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

_MAX_TOOL_ATTEMPTS = 2  # first try + one retry

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

AmbiguityKind = Literal["needs_second_ticker", "needs_ticker", "needs_prior_turn"]

# QNT-212: deterministic fallback prose for when the clarify LLM call fails.
# Reasons must stay digit-free -- ``domain_redirect`` rejects digits at the
# boundary so a future caller cannot trip the hallucination guardrail.
_CLARIFY_FALLBACK_REASON: dict[str, str] = {
    "needs_ticker": "Which ticker did you have in mind?",
    "needs_second_ticker": "Which second ticker should I compare against?",
    "needs_prior_turn": "I don't have an earlier turn on this thread to follow up on.",
}


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
    conversational: NotRequired[ConversationalAnswer | None]
    focused: NotRequired[FocusedAnalysis | None]
    confidence: NotRequired[float]
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
        "Pick 2-4 of the available reports that are most relevant to the user's question. "
        "Always include company when it is available; it is cheap context that anchors the "
        "analysis in the business. Choose fundamental for valuation, earnings, margins, "
        "or balance-sheet questions. Choose technical for chart, trend, momentum, RSI, "
        "or setup questions. Choose news for headlines, catalysts, events, sentiment, "
        "or what changed recently.\n\n"
        "Return a structured plan with:\n"
        "- tools: the selected report names only\n"
        "- rationale: 1-2 analyst-note sentences that cite what the question is asking "
        "about and why these reports are enough. Example voice: Your question is about "
        "valuation, so I'll lean on fundamentals and the company profile; technicals "
        "and news matter less here."
    )


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
    """Confidence = fraction of planned reports that were actually gathered.
    An honest heuristic — LLM self-reported confidence is known to be poorly
    calibrated, so we anchor it to report coverage instead."""
    if not plan:
        return 0.0
    return round(len(reports) / len(plan), 2)


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
        reports[name] = result
    return reports, errors


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


def _coerce_conversational(response: object) -> ConversationalAnswer | None:
    """Normalise whatever ``llm.invoke`` hands back into a ``ConversationalAnswer``."""
    if isinstance(response, ConversationalAnswer):
        return response
    if isinstance(response, dict):
        parsed = response.get("parsed")
        if isinstance(parsed, ConversationalAnswer):
            return parsed
    return None


def _coerce_focused(response: object) -> FocusedAnalysis | None:
    """Normalise whatever ``llm.invoke`` hands back into a ``FocusedAnalysis``."""
    if isinstance(response, FocusedAnalysis):
        return response
    if isinstance(response, dict):
        parsed = response.get("parsed")
        if isinstance(parsed, FocusedAnalysis):
            return parsed
    return None


def _detect_ambiguity(
    intent: Intent,
    question: str,
    *,
    has_prior_turn: bool,
) -> AmbiguityKind | None:
    """Return the kind of ambiguity in ``question`` for ``intent``, or None.

    QNT-212: heuristic-only check fired by classify_node right after the
    intent resolves. The three triggers map directly to the AC1 scenarios:

    * comparison + fewer than 2 tickers named in the question ⇒
      ``needs_second_ticker`` — the user gestured at a compare but only
      named one symbol. State.ticker is intentionally NOT counted; if the
      user wanted to compare with the URL-context ticker they would have
      typed "compare to AAPL", not "compare them".
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
    if intent == "comparison" and len(question_tickers) < 2:
        return "needs_second_ticker"
    if intent in _TICKER_REQUIRING_INTENTS and not question_tickers and not has_prior_turn:
        return "needs_ticker"
    if intent == "followup" and not has_prior_turn:
        return "needs_prior_turn"
    return None


def _resolve_comparison_tickers(primary: str, question: str) -> list[str]:
    """Return up to 2 tickers to compare, in user-named order.

    Ticker symbols mentioned in ``question`` come first (in the order the
    user wrote them); ``primary`` (the URL-derived ticker the chat panel
    sends) is appended when missing so a question like "compare to AAPL"
    fired from /ticker/NVDA still works. The list is capped at 2 — three or
    more named tickers fall out of scope per the QNT-156 ticket and trigger
    a conversational redirect.
    """
    chosen: list[str] = list(extract_tickers(question))
    primary_upper = primary.upper()
    if primary_upper in TICKERS and primary_upper not in chosen:
        chosen.append(primary_upper)
    return chosen[:2]


def _followup_is_metric_ask(question: str) -> bool:
    """Return True if a followup question targets a specific metric.

    Reuses the same quick-fact token list the classifier heuristic uses
    (RSI, P/E, EPS, volume, etc.). A hit means the followup should still
    produce a QuickFactAnswer card; a miss routes to the narrative-only
    path so narrate owns the response and no quick_fact event fires.
    """
    from agent.intent import _QUICK_FACT_TOKENS, _matches_any

    return _matches_any(question.lower(), _QUICK_FACT_TOKENS) is not None


def _history_before_current(
    messages: list[ConversationMessage] | None,
    question: str,
) -> list[ConversationMessage]:
    """Return prior transcript, excluding the current user turn if appended."""
    history = trim_message_history(messages)
    if (
        history
        and history[-1].get("role") == "user"
        and history[-1].get("content") == question.strip()
    ):
        return history[:-1]
    return history


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

    comparison = state.get("comparison")
    if comparison is not None:
        differences = getattr(comparison, "differences", "")
        return "\n".join(
            part for part in (prefix or str(differences), "Structured payload: comparison") if part
        ).strip()

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
    return None


def build_graph(
    tools: dict[str, ToolFn],
    *,
    event_emitter: EventEmitter | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
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
    """

    def classify_node(state: AgentState, config: RunnableConfig) -> dict[str, object]:
        # QNT-181: nodes accept ``config`` so the LangGraph CallbackHandler
        # propagates to inner ``llm.invoke(prompt, config=config)`` calls.
        # Without it, generation observations would not nest under the
        # parent agent-chat trace.
        ticker = state["ticker"]
        question = state.get("question", "")
        history = trim_message_history(state.get("messages"))
        # ``classify_intent`` already biases to "thesis" on internal LLM
        # failures, but a failure in the surrounding observability stack
        # would propagate and kill the run — same shape as plan_node /
        # synthesize_node which wrap their LLM call in BLE001. Mirror that
        # contract here so the bias-to-thesis invariant the rest of the
        # graph relies on cannot be defeated by an unrelated dependency.
        # QNT-216: prior turn can be detected from the transcript first, with
        # the old QNT-209 reports/thesis hydration kept as backwards-compatible
        # signal for checkpoints created before ``messages`` existed.
        has_prior_turn = bool(history or state.get("reports") or state.get("thesis"))
        try:
            intent, classifier_source = classify_intent_with_source(
                question,
                config=config,
                has_prior_turn=has_prior_turn,
                history=history,
            )
        except Exception as exc:  # noqa: BLE001 — preserve the safe default
            logger.warning("classify %s: defaulting to thesis: %s", ticker, exc)
            intent = "thesis"
            classifier_source = "fallback"
        logger.info("classify %s: intent=%s source=%s", ticker, intent, classifier_source)
        # QNT-212: heuristic ambiguity check on the resolved intent. Drives
        # the conditional edge below: a non-None ambiguity_kind routes to
        # clarify; None falls through to the existing plan/synthesize path
        # (or the new conversational/followup short-circuits).
        ambiguity_kind = _detect_ambiguity(intent, question, has_prior_turn=has_prior_turn)
        if ambiguity_kind is not None:
            logger.info(
                "classify %s: ambiguity_kind=%s (intent=%s)",
                ticker,
                ambiguity_kind,
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
            "intent": intent,
            "classifier_source": classifier_source,
            "ambiguity_kind": ambiguity_kind,
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
            comparison_tickers = _resolve_comparison_tickers(ticker, question)
            if len(comparison_tickers) < 2:
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
            response = get_llm(temperature=0.0).invoke(prompt, config=config)
            content = response.content if hasattr(response, "content") else str(response)
            plan = _parse_plan(str(content), available, intent)
            plan_rationale = None
        elif intent == "thesis":
            prompt = _build_thesis_plan_prompt(ticker, question, available)
            structured_llm = (
                get_llm(temperature=0.0)
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
            if len(comparison_tickers) < 2:
                # Fall through with empty bundle — synthesize will redirect.
                logger.info(
                    "gather %s: comparison needs 2 tickers, got %s",
                    ticker,
                    comparison_tickers,
                )
                return {"reports": {}, "errors": {}, "reports_by_ticker": {}}

            reports_by_ticker: dict[str, dict[str, str]] = {}
            errors: dict[str, str] = {}
            for cmp_ticker in comparison_tickers:
                ticker_reports, ticker_errors = _gather_reports(cmp_ticker, plan, tools)
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

        reports, errors = _gather_reports(ticker, plan, tools)
        logger.info(
            "gather %s: gathered=%s errors=%s",
            ticker,
            sorted(reports),
            sorted(errors),
        )
        return {"reports": reports, "errors": errors, "reports_by_ticker": {}}

    def synthesize_node(state: AgentState, config: RunnableConfig) -> dict[str, object]:
        ticker = state["ticker"]
        question = state.get("question", "")
        reports = state.get("reports", {})
        plan = state.get("plan", [])
        intent = state.get("intent", "thesis")
        confidence = _confidence_from_reports(reports, plan)

        # Helper: build the all-None payload skeleton so each branch only has
        # to set its own slot. Keeps consumers free to switch on intent
        # without worrying about stale keys from a previous shape.
        def _empty_payload() -> dict[str, object]:
            return {
                "thesis": None,
                "quick_fact": None,
                "comparison": None,
                "conversational": None,
                "focused": None,
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
                history=_history_before_current(state.get("messages"), question),
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
            prompt = build_conversational_prompt(question)
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
            payload = _empty_payload()
            payload["conversational"] = conversational
            logger.info("synthesize %s: confidence=%s conversational=ok", ticker, confidence)
            return payload

        if intent == "comparison":
            comparison_tickers = state.get("comparison_tickers", [])
            reports_by_ticker = state.get("reports_by_ticker", {})
            if len(comparison_tickers) < 2:
                return _fallback(
                    "I can compare two tickers I cover, but I couldn't find two in your question."
                )
            # Need at least one report for each ticker — comparing an empty
            # column to anything is just a half thesis.
            if not all(reports_by_ticker.get(t) for t in comparison_tickers):
                return _fallback("I couldn't pull reports for both of those tickers right now.")

            prompt = build_comparison_prompt(
                comparison_tickers,
                question,
                reports_by_ticker,
                history=_history_before_current(state.get("messages"), question),
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

        if intent in _FOCUSED_REPORT:
            if not reports:
                return _fallback(
                    "I couldn't pull a report to answer that focused analysis right now."
                )
            prompt = build_focused_prompt(
                intent,
                ticker,
                question,
                reports,
                history=_history_before_current(state.get("messages"), question),
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
                history=_history_before_current(state.get("messages"), question),
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
            history=_history_before_current(state.get("messages"), question),
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

    def narrate_node(state: AgentState, config: RunnableConfig) -> dict[str, object]:
        """QNT-211: stream a 1-4 sentence analyst-voice paragraph that wraps
        whichever structured payload synthesize produced.

        Tokens stream out via ``event_emitter("narrative_chunk", {"delta": ...})``
        so the chat panel can render a prose bubble above the card before the
        card composes. On any LLM failure we log, set ``narrative=None``, and
        terminate normally — the structured card still renders.

        Conversational intent skips this node: that path's answer is already
        prose, so re-narrating would just echo it. Followup narrative-only
        path (synthesize set ``quick_fact=None``) routes through here and
        produces the only spoken response the user sees.
        """
        intent = state.get("intent", "thesis")
        ticker = state["ticker"]
        question = state.get("question", "")

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
        # route sets it. Allow narrate through when it's present.
        is_clarify = state.get("ambiguity_kind") is not None
        if intent == "conversational" or (
            state.get("conversational") is not None and not is_clarify
        ):
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
            or state.get("focused")
            or state.get("conversational")
        )
        payload_markdown = ""
        to_md: Any = getattr(payload_obj, "to_markdown", None)
        if callable(to_md):
            try:
                payload_markdown = str(to_md())
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
                    prior_thesis_markdown = str(prior_to_md())
                except Exception:  # noqa: BLE001
                    prior_thesis_markdown = None

        prompt = build_narrate_prompt(
            intent=str(intent),
            ticker=ticker,
            question=question,
            payload_markdown=payload_markdown,
            prior_thesis_markdown=prior_thesis_markdown,
            plan_rationale=state.get("plan_rationale"),
            history=_history_before_current(state.get("messages"), question),
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
        return {
            "narrative": final_narrative,
            "messages": _append_assistant_message(state, final_narrative),
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
        logger.info("clarify %s: ambiguity_kind=%s clarify=ok", ticker, ambiguity_kind)
        return {"conversational": conversational}

    def _classify_router(state: AgentState) -> str:
        """QNT-212: pick the next node from classify_node's output.

        Ambiguity always wins -- a clarify run never burns the plan/gather
        LLM call. Conversational and followup short-circuit to synthesize
        so a greeting or a hydrated-thread followup doesn't walk the full
        4-node pipeline. Everything else (thesis, focused, comparison,
        quick_fact) falls through to plan as today.
        """
        if state.get("ambiguity_kind"):
            return "clarify"
        intent = state.get("intent", "thesis")
        if intent in _SHORT_CIRCUIT_INTENTS:
            return "synthesize"
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
    builder.add_edge(START, "classify")
    # QNT-212: classify routes by ambiguity / intent rather than always
    # falling through to plan. Three destinations:
    #   - clarify  : ambiguous question (no ticker, only one ticker for a
    #                compare, etc.) — ask back, exit through narrate.
    #   - synthesize: conversational greeting or followup-no-refetch — skip
    #                 plan + gather, save the latency of 2 no-op nodes.
    #   - plan     : thesis / focused / quick_fact / comparison — existing
    #                full-pipeline behavior.
    builder.add_conditional_edges(
        "classify",
        _classify_router,
        {"clarify": "clarify", "synthesize": "synthesize", "plan": "plan"},
    )
    builder.add_edge("plan", "gather")
    # QNT-156: always run synthesize. Empty reports no longer short-circuit
    # to END — synthesize handles every failure surface (no reports, empty
    # payload, structured-output crash) by emitting a deterministic
    # conversational redirect via ``domain_redirect``. The panel never sees
    # a blank state again.
    builder.add_edge("gather", "synthesize")
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
    "FocusedAnalysis",
    "Intent",
    "QuickFactAnswer",
    "ReportToolName",
    "Thesis",
    "ThesisPlan",
    "ToolFn",
    "build_graph",
]
