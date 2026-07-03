"""Push deterministic eval scores onto the current Langfuse trace (QNT-182).

The local golden-set harness (`agent.evals`) computes four scorers per record
and writes them to `history.csv`. Two of those are deterministic and cheap
enough to run on every prod chat:

* ``hallucination_ok`` -- every numeric token in the rendered answer must
  appear in some fetched report. Implemented in
  :mod:`agent.evals.hallucination`.
* ``plan_adherence`` -- the planner-requested tools must be a subset of the
  tools the gather node successfully fetched. Set comparison, no LLM cost.

The other two scorers (``judge_score``, ``cosine``) need either an LLM call or
a reference thesis, neither of which is free at prod-chat scale. We
deliberately do NOT push those.

QNT-208 dropped two deterministic verdict-shape scores that depended on
fields the v2 schema no longer carries. The new ``verdict_rationale`` is
narrative; if a post-deploy review surfaces analogous v2 checks (e.g.
rationale must mention an aspect label verbatim) we can add them back as
their own ticket.

Wiring: :func:`push_to_trace_id` is called from ``api.routers.agent_chat``
after ``graph.invoke()`` returns, with the ``trace_id`` resolved from the
LangGraph ``CallbackHandler``'s ``last_trace_id`` attribute. When Langfuse
keys are unset (eval bench runs strip them at import time, ADR-019) or
the handler never produced a trace (no LLM call inside the graph) the
call is a safe no-op.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.citations import find_bad_anchors
from agent.comparison import ComparisonAnswer
from agent.conversational import ConversationalAnswer
from agent.evals.hallucination import HallucinationResult
from agent.evals.hallucination import check as check_hallucination
from agent.exploration import ExplorationAnswer
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
    saw -- so conversational wins over thesis when both are present.

    Order: comparison > conversational > quick_fact > focused > exploration
    > thesis.
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
    exploration = state.get("exploration")
    if isinstance(exploration, ExplorationAnswer):
        return exploration.to_markdown()
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
    comparison answer is degraded -- flag it.
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


def compute_anchor_integrity(state: dict[str, Any]) -> list[str]:
    """Return the retrieved anchors the answer cited that are untrustworthy --
    out of range (a fake footnote) OR corpus-mismatched (``fundamental R1`` where
    R1 is a news row), each as it was cited (QNT-305 + corpus follow-up).

    Deterministic (no LLM), so it runs on every prod chat as a regression guard
    alongside ``hallucination_ok``. Scans both the rendered card answer and the
    streamed narrate bubble (``state["narrative"]``), since the narrate voice is
    the shape most prone to mis-anchoring. An empty list means every cited anchor
    is trustworthy (or none was cited).

    ``sources`` mirrors the render-boundary gate in ``api.routers.agent_chat``
    exactly: retrieved rows only count when gather actually ran this turn. A pure
    followup skips gather and reuses checkpointer-hydrated (stale) sources; the
    render boundary counts those as zero rows, so the detector must too --
    otherwise the stale rows would let a bad anchor pass as ``clean`` on exactly
    the case the guard strips.
    """
    intent_path = state.get("intent_path") or []
    sources = state.get("retrieved_sources") or [] if "gather" in intent_path else []
    answer = _render_answer(state)
    narrative = state.get("narrative")
    if isinstance(narrative, str):
        answer = f"{answer}\n{narrative}"
    return find_bad_anchors(answer, sources)


def compute_scores(state: dict[str, Any]) -> tuple[HallucinationResult, set[str]]:
    """Compute ``(hallucination_result, missing_planned_tools)`` for one run.

    Pure: no I/O, no Langfuse client. Exposed so tests can assert on the raw
    scorer output without monkeypatching the Langfuse SDK.
    """
    answer_md = _render_answer(state)
    reports = _flatten_reports(state)
    hallucination = check_hallucination(answer_md, reports)

    planned = set(state.get("plan") or [])
    missing = _missing_planned_tools(state, planned) if planned else set()

    return hallucination, missing


def push_to_trace_id(state: dict[str, Any], trace_id: str | None) -> None:
    """Compute and attach ``hallucination_ok`` + ``plan_adherence`` scores to
    the Langfuse trace identified by ``trace_id``.

    Failures are logged at WARNING and swallowed: observability must never
    break the SSE response.
    """
    if langfuse is None or not trace_id:
        return
    try:
        hallucination, missing_tools = compute_scores(state)
        langfuse.create_score(
            trace_id=trace_id,
            name="hallucination_ok",
            value=1.0 if hallucination.ok else 0.0,
            data_type="NUMERIC",
            comment=hallucination.reason(),
        )
        langfuse.create_score(
            trace_id=trace_id,
            name="plan_adherence",
            value=0.0 if missing_tools else 1.0,
            data_type="NUMERIC",
            comment=(f"missing: {', '.join(sorted(missing_tools))}" if missing_tools else "clean"),
        )
        bad_anchors = compute_anchor_integrity(state)
        langfuse.create_score(
            trace_id=trace_id,
            name="anchor_integrity_ok",
            value=0.0 if bad_anchors else 1.0,
            data_type="NUMERIC",
            comment=(
                f"bad retrieved anchors: {', '.join(bad_anchors)}" if bad_anchors else "clean"
            ),
        )
        grounding_rate = state.get("grounding_rate")
        if isinstance(grounding_rate, int | float):
            unsupported = state.get("grounding_unsupported") or []
            comment = (
                f"unsupported: {', '.join(str(v) for v in unsupported[:8])}"
                if unsupported
                else "clean"
            )
            langfuse.create_score(
                trace_id=trace_id,
                name="runtime_grounding_rate",
                value=float(grounding_rate),
                data_type="NUMERIC",
                comment=comment,
            )
    except Exception as exc:  # noqa: BLE001 — telemetry must not crash the request
        logger.warning("eval-score push failed: %s", exc)


__all__ = [
    "compute_anchor_integrity",
    "compute_scores",
    "push_to_trace_id",
]
