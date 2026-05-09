"""Push deterministic eval scores onto the current Langfuse trace (QNT-182).

The local golden-set harness (`agent.evals`) computes four scorers per record
and writes them to `history.csv`. Two of those are deterministic and cheap
enough to run on every prod chat:

* ``hallucination_ok`` — every numeric token in the rendered answer must
  appear in some fetched report. Implemented in
  :mod:`agent.evals.hallucination`.
* ``plan_adherence`` — the planner-requested tools must be a subset of the
  tools the gather node successfully fetched. Set comparison, no LLM cost.

The other two scorers (``judge_score``, ``cosine``) need either an LLM call or
a reference thesis, neither of which is free at prod-chat scale. We
deliberately do NOT push those — see the QNT-182 ticket for the cost rationale.

Wiring: :func:`push_to_current_trace` is called from inside the
``@observe(name="agent-chat")``-decorated runner in
``api.routers.agent_chat`` after ``graph.invoke()`` returns. It uses
``langfuse.score_current_trace(...)`` so no trace ID extraction is needed.
When Langfuse keys are unset (eval bench runs strip them at import time, ADR-019)
the call is a safe no-op.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.comparison import ComparisonAnswer
from agent.conversational import ConversationalAnswer
from agent.evals.hallucination import HallucinationResult
from agent.evals.hallucination import check as check_hallucination
from agent.focused import FocusedAnalysis
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from agent.tracing import langfuse

logger = logging.getLogger(__name__)


def _render_answer(state: dict[str, Any]) -> str:
    """Render whichever answer shape the SSE handler streamed into markdown.

    Mirrors the SSE dispatch in
    :func:`api.routers.agent_chat._stream`, NOT the eval-bench dispatch in
    :func:`agent.evals.golden_set.run_record`. The two diverge on one case:
    when the graph populates BOTH ``thesis`` and ``conversational`` (the
    synthesize-path fallback path where intent picked thesis but synthesis
    failed and a domain-redirect conversational was filled in), ``run_record``
    scores the thesis but the SSE handler streams the conversational redirect
    to the user. For prod scoring we want to score what the user actually
    saw — so conversational wins over thesis when both are present.

    Order: comparison > conversational > quick_fact > focused > thesis.
    """
    comparison = state.get("comparison")
    if isinstance(comparison, ComparisonAnswer):
        return comparison.to_markdown()
    conversational = state.get("conversational")
    if isinstance(conversational, ConversationalAnswer):
        return conversational.to_markdown()
    quick_fact = state.get("quick_fact")
    if isinstance(quick_fact, QuickFactAnswer):
        return quick_fact.to_markdown()
    focused = state.get("focused")
    if isinstance(focused, FocusedAnalysis):
        return focused.to_markdown()
    thesis = state.get("thesis")
    if isinstance(thesis, Thesis):
        return thesis.to_markdown()
    return ""


def _flatten_reports(state: dict[str, Any]) -> list[str]:
    """Return every report body the graph fetched, regardless of intent.

    Single-ticker runs populate ``state["reports"]`` (``{tool: body}``).
    Comparison runs populate ``state["reports_by_ticker"]``
    (``{ticker: {tool: body}}``) AND mirror the primary ticker's bundle into
    ``state["reports"]`` for non-comparison consumers. We flatten everything
    so a thesis number sourced from any per-ticker report counts as supported.
    """
    reports_by_ticker = state.get("reports_by_ticker") or {}
    if reports_by_ticker:
        flat: list[str] = []
        for ticker_reports in reports_by_ticker.values():
            if isinstance(ticker_reports, dict):
                flat.extend(str(v) for v in ticker_reports.values())
        return flat
    reports = state.get("reports") or {}
    return [str(v) for v in reports.values()] if isinstance(reports, dict) else []


def _missing_planned_tools(state: dict[str, Any], planned: set[str]) -> set[str]:
    """Return tools the planner asked for that the gather node failed to fetch.

    Single-ticker runs: any planned tool not in ``state["reports"]``.

    Comparison runs populate ``state["reports_by_ticker"]``
    (``{ticker: {tool: body}}``). Per-ticker (intersection) is the strict
    model: a planned tool is considered fulfilled only if EVERY ticker
    fetched it. If NVDA gets ``fundamental`` but AAPL doesn't, the
    comparison answer is degraded — flag it. The looser union model
    (``any ticker has it``) would silently pass on partial gather
    failures, defeating the purpose of the score.
    """
    reports_by_ticker = state.get("reports_by_ticker") or {}
    if reports_by_ticker:
        missing: set[str] = set()
        for ticker_reports in reports_by_ticker.values():
            if not isinstance(ticker_reports, dict):
                continue
            missing.update(planned - set(ticker_reports.keys()))
        return missing
    reports = state.get("reports") or {}
    if not isinstance(reports, dict):
        return set(planned)
    return planned - set(reports.keys())


def compute_scores(state: dict[str, Any]) -> tuple[HallucinationResult, set[str]]:
    """Compute ``(hallucination_result, missing_planned_tools)`` for one run.

    ``missing_planned_tools`` is empty iff plan-adherence passes. Returning the
    set (not just a bool) lets the caller surface the failing tool names in
    the Langfuse score ``comment`` so prod debugging doesn't require
    cross-referencing logs.

    Pure: no I/O, no Langfuse client. Exposed so tests can assert on the raw
    scorer output without monkeypatching the Langfuse SDK.
    """
    answer_md = _render_answer(state)
    reports = _flatten_reports(state)
    hallucination = check_hallucination(answer_md, reports)

    planned = set(state.get("plan") or [])
    # An empty plan (e.g. conversational redirect) trivially satisfies
    # adherence — no tool was requested, no tool can be missing.
    missing = _missing_planned_tools(state, planned) if planned else set()

    return hallucination, missing


def push_to_current_trace(state: dict[str, Any]) -> None:
    """Compute and attach ``hallucination_ok`` + ``plan_adherence`` scores to
    the active Langfuse trace.

    Must be called from within an ``@observe``-decorated function so
    ``score_current_trace`` resolves the ambient trace. Safe no-op when:

    * Langfuse keys are unset (``langfuse is None``) — eval bench runs strip
      keys at import time so callbacks have no client to flow to.
    * No trace context is active (e.g. unit tests calling ``_runner``
      bypassing ``@observe``).

    Failures are logged at WARNING and swallowed: observability must never
    break the SSE response, same pattern as ``_UsageCallback`` in
    ``agent.llm``.
    """
    if langfuse is None:
        return
    try:
        hallucination, missing_tools = compute_scores(state)
        langfuse.score_current_trace(
            name="hallucination_ok",
            value=1.0 if hallucination.ok else 0.0,
            data_type="NUMERIC",
            comment=hallucination.reason(),
        )
        langfuse.score_current_trace(
            name="plan_adherence",
            value=0.0 if missing_tools else 1.0,
            data_type="NUMERIC",
            comment=(f"missing: {', '.join(sorted(missing_tools))}" if missing_tools else "clean"),
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must not crash the request
        logger.warning("eval-score push failed: %s", exc)


__all__ = [
    "compute_scores",
    "push_to_current_trace",
]
