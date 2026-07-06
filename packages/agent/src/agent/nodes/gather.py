"""QNT-294 (AC1): gather + exploration supervisor node(s), extracted from build_graph.

Module-level function(s) taking ``(state, config, deps)`` -- unit-testable
without compiling a graph. Every ``agent.graph``-provided helper/constant is
referenced via the ``graph`` module object (attribute access at call time), so
the tests' ``monkeypatch.setattr(graph, "<name>", ...)`` seams keep working.
Build-time wiring comes in via ``deps`` (:class:`agent.nodes.deps.GraphDeps`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig

from agent import graph

logger = logging.getLogger("agent.graph")

if TYPE_CHECKING:
    from agent.graph import AgentState
    from agent.nodes.deps import GraphDeps


def _emit_retrieved_sources(
    deps: GraphDeps, ticker: str, retrieved_sources: list[dict[str, str]]
) -> None:
    """QNT-305 follow-up / QNT-321 (G-9): emit the retrieved-sources rows NOW.

    gather runs before synthesize/narrate, so this reaches the client BEFORE the
    narrate bubble streams. The frontend needs the row count during streaming:
    the anchor-integrity guard can only tell an in-range id from a fabricated one
    once it knows how many rows exist, and without the early count a hallucinated
    Rn renders mid-stream and then vanishes when the count finally lands (a
    jarring flicker). Shared by BOTH the cold and followup RAG branches -- the
    followup branch (QNT-290 warm-thread retrieval) originally returned without
    this emit, so its narrate streamed Rn anchors with no row count until the
    post-graph emit: the exact flicker the early emit fixes, alive on the one path
    that skipped it. The post-graph emit in agent_chat stays as the idempotent
    safety net (updateRun overwrites with the same payload).
    """
    if deps.event_emitter is None or not retrieved_sources:
        return
    try:
        deps.event_emitter("retrieved_sources", {"sources": retrieved_sources})
    except Exception as exc:  # noqa: BLE001 — never let SSE plumbing crash gather
        logger.warning(
            "gather %s: early retrieved_sources emit failed: %s (continuing)",
            ticker,
            exc,
        )


def _comparison_rag_demand(state: AgentState) -> str:
    """QNT-326 (G-14): the RAG corpora a comparison turn asked for but can't use.

    comparison's ``IntentPolicy.rag_corpora`` is empty because
    ``RetrievalSpec.fold`` cannot address ``reports_by_ticker`` yet, so a
    targeted-event comparison ("compare NVDA and AMD on their antitrust
    exposure") silently drops the classifier's ``needs_news_search`` /
    ``needs_earnings_search`` flags. Returns the '+'-joined corpora those flags
    requested ("news" / "earnings" / "news+earnings"), or ``""`` when neither
    fired. This is a DETECTOR only -- no per-ticker fold is built (RAG-seams
    lesson: ship the detector with the deferral); the returned string is the
    demand counter for deciding whether the fold is worth building.
    """
    corpora: list[str] = []
    if state.get("needs_news_search"):
        corpora.append("news")
    if state.get("needs_earnings_search"):
        corpora.append("earnings")
    return "+".join(corpora)


def gather_node(state: AgentState, config: RunnableConfig, deps: GraphDeps) -> dict[str, object]:  # noqa: ARG001 — config received for LangGraph contract; tools are HTTP, no LLM call
    ticker = state["ticker"]
    intent = state.get("intent", "thesis")
    plan = state.get("plan", [])

    # QNT-209/290: followup keeps the hydrated reports verbatim by
    # default -- the report PLAN never re-runs on followup (plan_node
    # returns an empty plan above), so there is no ``_gather_reports``
    # call here either way. When the classifier flagged a targeted RAG
    # need on THIS turn (a warm-thread pivot to a new event), fold fresh
    # search hits onto the hydrated reports copy instead of a no-op --
    # this branch is only reached at all when ``_followup_fires_search``
    # already said yes (``_classify_router``), so the two predicate
    # calls below are just picking which corpus/corpora to actually call.
    if intent == "followup":
        # QNT-209/290/291: followup keeps the hydrated reports verbatim
        # and re-runs no report PLAN (plan_node returns an empty plan);
        # the only fresh I/O is the gated retrieval fold(s). This branch
        # is only reached when ``_followup_fires_search`` already said yes
        # (``_classify_router``), and ``_run_retrievals`` re-checks each
        # spec's gate to pick which corpus/corpora to actually fold.
        reports = dict(state.get("reports") or {})
        reports, retrieved_sources = deps.run_retrievals(state, reports)
        # QNT-321 (G-9): emit before returning, same as the cold path -- else
        # the warm-thread followup narrate flickers on unbounded Rn anchors.
        _emit_retrieved_sources(deps, ticker, retrieved_sources)
        logger.info(
            "gather %s: followup RAG fired, reports=%s retrieved=%d",
            ticker,
            sorted(reports),
            len(retrieved_sources),
        )
        return {"reports": reports, "retrieved_sources": retrieved_sources}

    # Conversational path: nothing to gather. QNT-323 (G-4): unreachable in
    # practice (conversational short-circuits classify->synthesize, so gather
    # never runs) and classify already reset the scratch keys; kept for topology
    # symmetry and direct-call safety, producing nothing.
    if intent == "conversational":
        logger.info("gather %s: skipped (conversational)", ticker)
        return {}

    if intent == "comparison":
        comparison_tickers = state.get("comparison_tickers", [])
        # QNT-326 (G-14): record demand for a per-ticker RAG fold this turn
        # wanted but can't serve (comparison's rag_corpora is empty). Same value
        # on every return below so the log and the comparison_rag_demand trace
        # tag agree; "" (no flags) leaves the tag off.
        rag_demand = _comparison_rag_demand(state)
        if rag_demand:
            logger.info(
                "gather %s: comparison RAG demand (%s) discarded -- no per-ticker "
                "fold yet (QNT-326 G-14 detector)",
                ticker,
                rag_demand,
            )
        if len(comparison_tickers) < graph._MIN_COMPARISON_TICKERS:
            # Fall through with empty bundle — synthesize will redirect.
            # QNT-323 (G-4): carry only comparison_rag_demand; classify reset the
            # empty reports/errors/reports_by_ticker at the turn boundary.
            logger.info(
                "gather %s: comparison needs 2 tickers, got %s",
                ticker,
                comparison_tickers,
            )
            return {"comparison_rag_demand": rag_demand}

        # QNT-224: 3-4 tickers take the lean metrics path — ONE fetch of a
        # compact metrics row per ticker, not a full bundle each. The JSON
        # text is stashed into ``reports`` so _runtime_report_texts (and
        # thus the narrate grounding check) sees the numbers the lean card
        # and narration quote. reports_by_ticker stays empty (no rich
        # bundle), so synthesize routes to the lean branch.
        if len(comparison_tickers) > graph._MIN_COMPARISON_TICKERS:
            if deps.comparison_metrics_tool is None:
                logger.info(
                    "gather %s: 3-4 way comparison but no metrics tool wired — redirect",
                    ticker,
                )
                # QNT-323 (G-4): produce only the demand marker; classify reset
                # the rest.
                return {"comparison_rag_demand": rag_demand}
            metrics_json = deps.comparison_metrics_tool(comparison_tickers)
            if graph._is_tool_error(metrics_json):
                logger.warning("gather %s: comparison-metrics failed: %s", ticker, metrics_json)
                # QNT-323 (G-4): produce the error + demand marker only.
                return {
                    "errors": {"comparison_metrics": metrics_json},
                    "comparison_rag_demand": rag_demand,
                }
            logger.info("gather %s: lean comparison metrics for %s", ticker, comparison_tickers)
            # QNT-323 (G-4): produce the lean metrics report + demand marker; the
            # empty reports_by_ticker (so synthesize routes to the lean branch)
            # comes from classify's turn-boundary reset.
            return {
                "reports": {"comparison_metrics": metrics_json},
                "comparison_rag_demand": rag_demand,
            }

        # QNT-321 (G-3): fan every (ticker, tool) pair onto ONE shared bounded
        # pool instead of looping tickers as N sequential parallel batches. The
        # QNT-300 cap is about concurrent connections, so this holds the same
        # max-4-in-flight bound while overlapping across tickers (~1.7s -> ~0.9s
        # on a rich 2-ticker turn). reports_by_ticker and the ticker-prefixed
        # errors map are byte-identical to the old serial loop.
        effective_tools = deps.effective_tools(intent)  # compact company on comparison
        reports_by_ticker, errors = graph._gather_reports_multi(
            comparison_tickers, plan, effective_tools
        )

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
            "comparison_rag_demand": rag_demand,
        }

    # QNT-220 (#8): thesis gets the compact company variant when supplied.
    reports, errors = graph._gather_reports(ticker, plan, deps.effective_tools(intent))

    # QNT-291: targeted retrieval. A classifier-flagged ask
    # (``needs_news_search`` for a targeted-event news ask -- litigation,
    # CEO, buyback, recall, ...; ``needs_earnings_search`` for an
    # earnings-narrative ask -- guidance, outlook, management framing)
    # additionally searches the matching Qdrant corpus and folds the hits
    # into the report its synthesis reads (news -> reports["news"],
    # earnings -> reports["fundamental"]). Retrieved hits LEAD the canned
    # digest (QNT-276), each carrying corpus-tagged provenance so the
    # frontend distinguishes which corpus a citation came from. The whole
    # dispatch is one loop over RETRIEVAL_SPECS (``_run_retrievals``); each
    # spec's gate scopes the fire to the flag AND the intents whose
    # synthesis reads that corpus (QNT-288 policy table). A generic "news
    # on AAPL" leaves the flag False and keeps the canned digest.
    reports, retrieved_sources = deps.run_retrievals(state, reports)

    # QNT-305 follow-up / QNT-321 (G-9): emit the retrieved-sources rows NOW so
    # the narrate bubble has the row count before it streams (see helper).
    _emit_retrieved_sources(deps, ticker, retrieved_sources)

    logger.info(
        "gather %s: gathered=%s errors=%s",
        ticker,
        sorted(reports),
        sorted(errors),
    )
    # QNT-323 (G-4): carry the keys this path produces; the empty
    # reports_by_ticker (single-ticker path) comes from classify's reset.
    return {
        "reports": reports,
        "errors": errors,
        "retrieved_sources": retrieved_sources,
    }


def explore_supervisor_node(
    state: AgentState, config: RunnableConfig, deps: GraphDeps
) -> dict[str, object]:  # noqa: ARG001 — config kept for LangGraph node contract; deterministic policy makes no LLM call
    """QNT-215: bounded exploratory tool selection before synthesis.

    This is deliberately an internal route, not a replacement topology:
    classify only sends unambiguous, anchored exploratory turns here. The
    node gathers at most three existing report tools, then hands the
    accumulated reports to the normal synthesize/narrate tail.
    """
    ticker = state["ticker"]
    question = state.get("question", "")
    available = [t for t in graph.REPORT_TOOLS if t in deps.tools]
    reports: dict[str, str] = dict(state.get("reports") or {})

    if not available:
        logger.warning("explore_supervisor %s: no tools registered", ticker)
        # QNT-323 (G-4): reports_by_ticker / comparison_tickers are never
        # populated on the exploration path -- classify's reset owns them.
        return {
            "intent": "thesis",
            "plan": [],
            "plan_rationale": None,
            "reports": {},
            "errors": {},
            "supervisor_iterations": 0,
        }

    # QNT-220 (#4): deterministic broad-exploration policy -- 0 LLM calls.
    # The old QNT-215 loop asked the LLM for one tool at a time but never
    # showed it the report bodies, so the deterministic guardrail drove the
    # plan anyway (see _deterministic_exploration_plan). Gather the planned
    # lenses in one shot and hand them to the normal synthesize/narrate tail.
    plan = graph._deterministic_exploration_plan(question, available)
    # QNT-220 follow-up: a broad anchored scan always renders as the
    # dedicated exploration card -- a verdict-free, multi-lens shape -- so
    # the output intent is constant. "exploration" is in
    # _COMPACT_COMPANY_INTENTS, so the non-news-led [company, news] plan
    # still gets the compact company report (lever #8 savings preserved).
    output_intent: graph.Intent = "exploration"
    tool_reports, errors = graph._gather_reports(ticker, plan, deps.effective_tools(output_intent))
    reports.update(tool_reports)
    logger.info(
        "explore_supervisor %s: deterministic plan=%s output_intent=%s gathered=%s errors=%s",
        ticker,
        plan,
        output_intent,
        sorted(tool_reports),
        sorted(errors),
    )

    # QNT-298: same SSE surfacing as plan_node's thesis rationale (see
    # comment there) -- exploration gathers inline in this single node,
    # so the rationale lands right after this turn's tool_result events
    # rather than before them, but still well ahead of synthesize.
    exploration_rationale = graph._exploration_rationale(question, plan)
    if exploration_rationale is not None and deps.event_emitter is not None:
        try:
            deps.event_emitter("plan_rationale", {"text": exploration_rationale})
        except Exception as exc:  # noqa: BLE001 — never let SSE plumbing crash the graph
            logger.warning(
                "explore_supervisor %s: event_emitter failed: %s (continuing)", ticker, exc
            )

    # QNT-323 (G-4): carry only what exploration produces; classify's turn-boundary
    # reset owns the empty reports_by_ticker / comparison_tickers.
    return {
        "intent": output_intent,
        "plan": plan,
        "plan_rationale": exploration_rationale,
        "reports": reports,
        "errors": errors,
        "supervisor_iterations": len(plan),
        "confidence": graph._confidence_from_reports(reports, plan),
    }
