"""LangGraph research agent: plan -> gather -> synthesize (ADR-007).

The graph is the executive layer of the three-role architecture: it reasons
over pre-computed report strings returned by FastAPI tools and never does
arithmetic or touches the database.

Tools are injected at build time via a ``{name: callable}`` mapping. Tests
pass mock callables; production wiring (QNT-60) passes real HTTP tools.
Keeping tools outside the module makes the graph unit-testable offline.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph

from agent.llm import get_llm
from agent.prompts import REPORT_TOOLS, build_synthesis_prompt
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
    nodes as the graph runs. ``reports`` holds raw report strings keyed by
    tool name; ``errors`` records tool-name -> error-message for any tool
    that failed after retries. ``thesis`` and ``confidence`` are populated
    by the synthesize node.
    """

    ticker: str
    question: NotRequired[str]
    plan: NotRequired[list[str]]
    reports: NotRequired[dict[str, str]]
    errors: NotRequired[dict[str, str]]
    thesis: NotRequired[str]
    confidence: NotRequired[float]


def _build_plan_prompt(ticker: str, question: str, available: list[str]) -> str:
    options = ", ".join(available)
    return (
        f"You are planning which reports to fetch for an investment analysis of {ticker}.\n"
        f"Question: {question or '(general thesis)'}\n"
        f"Available reports: {options}\n\n"
        "Respond with a comma-separated list of report names to fetch from the available set. "
        "Include every report that is even marginally relevant; omit only reports that are "
        "clearly irrelevant to the question. Respond with the list only, no prose."
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


def build_graph(tools: dict[str, ToolFn]) -> CompiledStateGraph:
    """Compile the plan -> gather -> synthesize graph with the given tools.

    Tools are a plain ``{name: callable}`` mapping. Callables take a ticker
    string and return a report string. Exceptions are caught and retried;
    optional tools (see ``OPTIONAL_TOOLS``) are silently dropped after retry
    exhaustion, required tools surface in ``state['errors']``.
    """

    @observe(name="plan")
    def plan_node(state: AgentState) -> dict[str, object]:
        ticker = state["ticker"]
        question = state.get("question", "")
        available = [t for t in REPORT_TOOLS if t in tools]
        if not available:
            logger.warning("plan %s: no tools registered", ticker)
            return {"plan": [], "reports": {}, "errors": {}}

        prompt = _build_plan_prompt(ticker, question, available)
        response = langfuse.traced_invoke(get_llm(temperature=0.0), prompt, name="plan")
        content = response.content if hasattr(response, "content") else str(response)
        plan = _parse_plan(str(content), available)
        logger.info("plan %s: %s", ticker, plan)
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
        prompt = build_synthesis_prompt(ticker, question, reports)
        response = langfuse.traced_invoke(get_llm(), prompt, name="synthesize")
        # Guard against a provider returning ``content=None`` — surfaces as an
        # explicit error to the caller instead of a literal "None" thesis.
        content = response.content if hasattr(response, "content") else response
        thesis = str(content) if content is not None else ""
        confidence = _confidence_from_reports(reports, plan)
        logger.info("synthesize %s: confidence=%s", ticker, confidence)
        return {"thesis": thesis, "confidence": confidence}

    def _after_gather(state: AgentState) -> str:
        # Short-circuit to END when gather produced nothing — calling the LLM
        # with an empty prompt would just hallucinate a thesis out of the
        # system prompt. Caller sees empty thesis + confidence 0.0.
        return "synthesize" if state.get("reports") else END

    builder: StateGraph = StateGraph(AgentState)
    builder.add_node("plan", plan_node)
    builder.add_node("gather", gather_node)
    builder.add_node("synthesize", synthesize_node)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "gather")
    builder.add_conditional_edges("gather", _after_gather, {"synthesize": "synthesize", END: END})
    builder.add_edge("synthesize", END)
    return builder.compile()


__all__ = [
    "OPTIONAL_TOOLS",
    "REPORT_TOOLS",
    "AgentState",
    "ToolFn",
    "build_graph",
]
