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
from typing import TYPE_CHECKING, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from shared.tickers import TICKERS

from agent.comparison import ComparisonAnswer
from agent.conversational import ConversationalAnswer, domain_redirect
from agent.intent import Intent, classify_intent, extract_tickers
from agent.llm import get_llm
from agent.prompts import (
    REPORT_TOOLS,
    build_comparison_prompt,
    build_conversational_prompt,
    build_quick_fact_prompt,
    build_synthesis_prompt,
)
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from agent.tracing import langfuse, observe

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)

ToolFn = Callable[[str], str]

# Tool registry is canonical in ``agent.prompts.system`` so the citation list
# in SYSTEM_PROMPT and the dispatch list here can't drift. ``summary`` is
# intentionally omitted from the trio: plan/gather select from what the thesis
# composes over; adding summary is a QNT-60 call if the eval shows it improves
# grounding.

# News is optional — Qdrant or the news ingest can be down without invalidating
# the thesis. Technical & fundamental are load-bearing.
OPTIONAL_TOOLS: frozenset[str] = frozenset({"news"})

_MAX_TOOL_ATTEMPTS = 2  # first try + one retry


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
    plan: NotRequired[list[str]]
    reports: NotRequired[dict[str, str]]
    comparison_tickers: NotRequired[list[str]]
    reports_by_ticker: NotRequired[dict[str, dict[str, str]]]
    errors: NotRequired[dict[str, str]]
    thesis: NotRequired[Thesis | None]
    quick_fact: NotRequired[QuickFactAnswer | None]
    comparison: NotRequired[ComparisonAnswer | None]
    conversational: NotRequired[ConversationalAnswer | None]
    confidence: NotRequired[float]


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
        # doesn't touch and burns provider quota.
        bias = (
            "The user asked a single-metric question; pick ONLY the report(s) "
            "directly needed to answer it. Omit anything not strictly required. "
            "If unsure, prefer the smallest plan that can answer the question."
        )
    else:
        # Both ``thesis`` and ``comparison`` over-fetch — the comparison path
        # then re-runs the same plan against each ticker, so a narrow plan
        # would starve the second ticker too.
        bias = (
            "Include every report that is even marginally relevant; omit only "
            "reports that are clearly irrelevant to the question."
        )
    return (
        f"You are planning which reports to fetch for an investment analysis of {ticker}.\n"
        f"Question: {question or '(general thesis)'}\n"
        f"Available reports: {options}\n\n"
        "Respond with a comma-separated list of report names to fetch from the available set. "
        f"{bias} Respond with the list only, no prose."
    )


def _parse_plan(raw: str, available: list[str]) -> list[str]:
    """Return the subset of ``available`` named in ``raw``, preserving the
    order in ``available``. Falls back to the full list if parsing yields
    nothing — we'd rather over-fetch than strand the synthesize node."""
    tokens = {t.strip().lower() for t in raw.replace("\n", ",").split(",") if t.strip()}
    chosen = [t for t in available if t in tokens]
    return chosen or list(available)


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
    """Normalise whatever ``traced_invoke`` hands back into a ``Thesis``.

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
    """Normalise whatever ``traced_invoke`` hands back into a ``QuickFactAnswer``.

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
    """Normalise whatever ``traced_invoke`` hands back into a ``ComparisonAnswer``."""
    if isinstance(response, ComparisonAnswer):
        return response
    if isinstance(response, dict):
        parsed = response.get("parsed")
        if isinstance(parsed, ComparisonAnswer):
            return parsed
    return None


def _coerce_conversational(response: object) -> ConversationalAnswer | None:
    """Normalise whatever ``traced_invoke`` hands back into a ``ConversationalAnswer``."""
    if isinstance(response, ConversationalAnswer):
        return response
    if isinstance(response, dict):
        parsed = response.get("parsed")
        if isinstance(parsed, ConversationalAnswer):
            return parsed
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
    return None


def build_graph(tools: dict[str, ToolFn]) -> CompiledStateGraph:
    """Compile the classify -> plan -> gather -> synthesize graph (QNT-149).

    Tools are a plain ``{name: callable}`` mapping. Callables take a ticker
    string and return a report string. Exceptions are caught and retried;
    optional tools (see ``OPTIONAL_TOOLS``) are silently dropped after retry
    exhaustion, required tools surface in ``state['errors']``.
    """

    @observe(name="classify")
    def classify_node(state: AgentState) -> dict[str, object]:
        ticker = state["ticker"]
        question = state.get("question", "")
        # ``classify_intent`` already biases to "thesis" on internal LLM
        # failures, but a failure in the surrounding observability stack
        # (Langfuse SDK outage, decorator hook crash) would propagate and
        # kill the run — same shape as plan_node / synthesize_node, both
        # of which wrap their LLM call in BLE001. Mirror that contract here
        # so the bias-to-thesis invariant the rest of the graph relies on
        # cannot be defeated by an unrelated dependency.
        try:
            intent: Intent = classify_intent(question)
        except Exception as exc:  # noqa: BLE001 — preserve the safe default
            logger.warning("classify %s: defaulting to thesis: %s", ticker, exc)
            intent = "thesis"
        logger.info("classify %s: intent=%s", ticker, intent)
        return {"intent": intent}

    @observe(name="plan")
    def plan_node(state: AgentState) -> dict[str, object]:
        ticker = state["ticker"]
        question = state.get("question", "")
        intent = state.get("intent", "thesis")

        # Conversational path skips tool gathering entirely — the answer
        # comes from the LLM with no report context. We still pass through
        # plan_node so the graph topology stays linear; the gather node
        # then no-ops when ``plan`` is empty.
        if intent == "conversational":
            logger.info("plan %s: skipped (conversational)", ticker)
            return {
                "plan": [],
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

        prompt = _build_plan_prompt(ticker, question, available, intent)
        response = langfuse.traced_invoke(get_llm(temperature=0.0), prompt, name="plan")
        content = response.content if hasattr(response, "content") else str(response)
        plan = _parse_plan(str(content), available)
        logger.info(
            "plan %s: %s (intent=%s, comparison_tickers=%s)",
            ticker,
            plan,
            intent,
            comparison_tickers,
        )
        return {
            "plan": plan,
            "reports": {},
            "errors": {},
            "comparison_tickers": comparison_tickers,
            "reports_by_ticker": {},
        }

    @observe(name="gather")
    def gather_node(state: AgentState) -> dict[str, object]:
        ticker = state["ticker"]
        intent = state.get("intent", "thesis")
        plan = state.get("plan", [])

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

    @observe(name="synthesize")
    def synthesize_node(state: AgentState) -> dict[str, object]:
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

        if intent == "conversational":
            prompt = build_conversational_prompt(question)
            structured_llm = get_llm().with_structured_output(ConversationalAnswer)
            try:
                response = langfuse.traced_invoke(structured_llm, prompt, name="synthesize")
            except Exception as exc:  # noqa: BLE001 — fall back to deterministic redirect
                logger.warning(
                    "synthesize %s: conversational structured output failed: %s",
                    ticker,
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

            prompt = build_comparison_prompt(comparison_tickers, question, reports_by_ticker)
            structured_llm = get_llm().with_structured_output(ComparisonAnswer)
            try:
                response = langfuse.traced_invoke(structured_llm, prompt, name="synthesize")
            except Exception as exc:  # noqa: BLE001 — fall back to redirect
                logger.warning(
                    "synthesize %s: comparison structured output failed: %s",
                    ticker,
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

        if intent == "quick_fact":
            if not reports:
                return _fallback("I couldn't pull a report to answer that quick fact right now.")
            prompt = build_quick_fact_prompt(ticker, question, reports)
            structured_llm = get_llm().with_structured_output(QuickFactAnswer)
            try:
                response = langfuse.traced_invoke(structured_llm, prompt, name="synthesize")
            except Exception as exc:  # noqa: BLE001 — surface as fallback redirect
                logger.warning(
                    "synthesize %s: quick-fact structured output failed: %s", ticker, exc
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
        prompt = build_synthesis_prompt(ticker, question, reports)
        # ``with_structured_output(Thesis)`` forces the LLM into the four-section
        # schema. Errors from a misbehaving provider (Gemini occasionally
        # returns malformed tool-call JSON) surface as a fallback redirect
        # rather than crashing the whole run.
        structured_llm = get_llm().with_structured_output(Thesis)
        try:
            response = langfuse.traced_invoke(structured_llm, prompt, name="synthesize")
        except Exception as exc:  # noqa: BLE001 — surface as fallback redirect
            logger.warning("synthesize %s: structured output failed: %s", ticker, exc)
            response = None
        thesis = _coerce_thesis(response)
        if thesis is None:
            return _fallback("I had trouble pulling a thesis together for that.")
        payload = _empty_payload()
        payload["thesis"] = thesis
        logger.info("synthesize %s: confidence=%s thesis=ok", ticker, confidence)
        return payload

    builder: StateGraph = StateGraph(AgentState)
    builder.add_node("classify", classify_node)
    builder.add_node("plan", plan_node)
    builder.add_node("gather", gather_node)
    builder.add_node("synthesize", synthesize_node)
    builder.add_edge(START, "classify")
    builder.add_edge("classify", "plan")
    builder.add_edge("plan", "gather")
    # QNT-156: always run synthesize. Empty reports no longer short-circuit
    # to END — synthesize handles every failure surface (no reports, empty
    # payload, structured-output crash) by emitting a deterministic
    # conversational redirect via ``domain_redirect``. The panel never sees
    # a blank state again.
    builder.add_edge("gather", "synthesize")
    builder.add_edge("synthesize", END)
    return builder.compile()


__all__ = [
    "OPTIONAL_TOOLS",
    "REPORT_TOOLS",
    "AgentState",
    "ComparisonAnswer",
    "ConversationalAnswer",
    "Intent",
    "QuickFactAnswer",
    "Thesis",
    "ToolFn",
    "build_graph",
]
