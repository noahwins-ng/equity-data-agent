"""LangGraph research agent: classify -> plan -> gather -> synthesize (ADR-007, QNT-149).

The graph is the executive layer of the three-role architecture: it reasons
over pre-computed report strings returned by FastAPI tools and never does
arithmetic or touches the database.

Tools are injected at build time via a ``{name: callable}`` mapping. Tests
pass mock callables; production wiring (QNT-60) passes real HTTP tools.
Keeping tools outside the module makes the graph unit-testable offline.

Pipeline (QNT-149):

1. ``classify`` — pick a response shape from the user's question. The two
   shapes today are ``thesis`` (the existing Setup / Bull / Bear / Verdict
   treatment) and ``quick_fact`` (a short prose answer + a single cited
   value, no thesis card). Defaults to ``thesis`` on any classifier failure
   so existing eval contracts (QNT-67, QNT-128) cannot regress.
2. ``plan`` — pick which report tools to fetch. Bias depends on intent:
   thesis over-fetches (anything marginally relevant), quick-fact narrows
   to the specific report the question implies.
3. ``gather`` — drive the planned tools, retry transient failures, drop
   optional-tool failures silently.
4. ``synthesize`` — branch on intent. Thesis path uses
   :class:`agent.thesis.Thesis` via ``with_structured_output``; quick-fact
   path uses :class:`agent.quick_fact.QuickFactAnswer`. Exactly one of
   ``state['thesis']`` / ``state['quick_fact']`` is populated per run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph

from agent.intent import Intent, classify_intent
from agent.llm import get_llm
from agent.prompts import REPORT_TOOLS, build_quick_fact_prompt, build_synthesis_prompt
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
    strings keyed by tool name; ``errors`` records tool-name -> error
    message for any tool that failed after retries. Exactly one of
    ``thesis`` / ``quick_fact`` is populated per run, matching ``intent``.
    """

    ticker: str
    question: NotRequired[str]
    intent: NotRequired[Intent]
    plan: NotRequired[list[str]]
    reports: NotRequired[dict[str, str]]
    errors: NotRequired[dict[str, str]]
    thesis: NotRequired[Thesis | None]
    quick_fact: NotRequired[QuickFactAnswer | None]
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
        available = [t for t in REPORT_TOOLS if t in tools]
        if not available:
            logger.warning("plan %s: no tools registered", ticker)
            return {"plan": [], "reports": {}, "errors": {}}

        prompt = _build_plan_prompt(ticker, question, available, intent)
        response = langfuse.traced_invoke(get_llm(temperature=0.0), prompt, name="plan")
        content = response.content if hasattr(response, "content") else str(response)
        plan = _parse_plan(str(content), available)
        logger.info("plan %s: %s (intent=%s)", ticker, plan, intent)
        return {"plan": plan, "reports": {}, "errors": {}}

    @observe(name="gather")
    def gather_node(state: AgentState) -> dict[str, object]:
        ticker = state["ticker"]
        reports, errors = _gather_reports(ticker, state.get("plan", []), tools)
        logger.info(
            "gather %s: gathered=%s errors=%s",
            ticker,
            sorted(reports),
            sorted(errors),
        )
        return {"reports": reports, "errors": errors}

    @observe(name="synthesize")
    def synthesize_node(state: AgentState) -> dict[str, object]:
        ticker = state["ticker"]
        question = state.get("question", "")
        reports = state.get("reports", {})
        plan = state.get("plan", [])
        intent = state.get("intent", "thesis")
        confidence = _confidence_from_reports(reports, plan)

        if intent == "quick_fact":
            prompt = build_quick_fact_prompt(ticker, question, reports)
            structured_llm = get_llm().with_structured_output(QuickFactAnswer)
            try:
                response = langfuse.traced_invoke(structured_llm, prompt, name="synthesize")
            except Exception as exc:  # noqa: BLE001 — surface as empty answer
                logger.warning(
                    "synthesize %s: quick-fact structured output failed: %s", ticker, exc
                )
                response = None
            quick_fact = _coerce_quick_fact(response)
            logger.info(
                "synthesize %s: confidence=%s quick_fact=%s",
                ticker,
                confidence,
                quick_fact is not None,
            )
            # Emit both keys so consumers can switch on intent without
            # worrying about stale keys; the unused branch is None.
            return {"thesis": None, "quick_fact": quick_fact, "confidence": confidence}

        prompt = build_synthesis_prompt(ticker, question, reports)
        # ``with_structured_output(Thesis)`` forces the LLM into the four-section
        # schema. Errors from a misbehaving provider (Gemini occasionally
        # returns malformed tool-call JSON) surface as a None thesis rather
        # than crashing the whole run; the CLI / API treat that the same as
        # the "no reports gathered" short-circuit.
        structured_llm = get_llm().with_structured_output(Thesis)
        try:
            response = langfuse.traced_invoke(structured_llm, prompt, name="synthesize")
        except Exception as exc:  # noqa: BLE001 — surface as empty thesis, log, continue
            logger.warning("synthesize %s: structured output failed: %s", ticker, exc)
            response = None
        thesis = _coerce_thesis(response)
        logger.info(
            "synthesize %s: confidence=%s thesis=%s", ticker, confidence, thesis is not None
        )
        return {"thesis": thesis, "quick_fact": None, "confidence": confidence}

    def _after_gather(state: AgentState) -> str:
        # Short-circuit to END when gather produced nothing — calling the LLM
        # with an empty prompt would just hallucinate a thesis out of the
        # system prompt. Caller sees no thesis + confidence 0.0.
        return "synthesize" if state.get("reports") else END

    builder: StateGraph = StateGraph(AgentState)
    builder.add_node("classify", classify_node)
    builder.add_node("plan", plan_node)
    builder.add_node("gather", gather_node)
    builder.add_node("synthesize", synthesize_node)
    builder.add_edge(START, "classify")
    builder.add_edge("classify", "plan")
    builder.add_edge("plan", "gather")
    builder.add_conditional_edges("gather", _after_gather, {"synthesize": "synthesize", END: END})
    builder.add_edge("synthesize", END)
    return builder.compile()


__all__ = [
    "OPTIONAL_TOOLS",
    "REPORT_TOOLS",
    "AgentState",
    "Intent",
    "QuickFactAnswer",
    "Thesis",
    "ToolFn",
    "build_graph",
]
