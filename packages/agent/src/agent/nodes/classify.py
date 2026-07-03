"""QNT-294 (AC1): classify + routing node(s), extracted from build_graph.

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


def classify_node(state: AgentState, config: RunnableConfig, deps: GraphDeps) -> dict[str, object]:
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
    history, has_prior_turn = graph._prior_turn_context(state, question)
    try:
        (
            intent,
            classifier_source,
            needs_news_search,
            needs_earnings_search,
            search_query,
        ) = graph.classify_intent_with_source(
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
    question_tickers = graph.extract_tickers(question)
    if (
        graph.has_comparison_phrase(question)
        and len(question_tickers) == 1
        and ticker.upper() in graph.TICKERS
        and ticker.upper() not in question_tickers
    ):
        intent = "comparison"
    if graph._should_route_exploration(intent, question, has_prior_turn=has_prior_turn):
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
    ambiguity_kind = graph._detect_ambiguity(
        intent,
        question,
        has_prior_turn=has_prior_turn,
        has_context_ticker=ticker.upper() in graph.TICKERS,
        context_ticker=ticker,
    )
    if ambiguity_kind is not None:
        logger.info(
            "classify %s: ambiguity_kind=%s (intent=%s)",
            ticker,
            ambiguity_kind,
            intent,
        )
    effective_ticker = graph._resolve_single_ticker_context(
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
    if deps.event_emitter is not None:
        try:
            deps.event_emitter("intent", {"intent": intent})
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
        "messages": graph._append_user_message(history, question),
    }


def _classify_router(state: AgentState, deps: GraphDeps) -> str:
    """QNT-212/QNT-215: pick the next node from classify_node's output.

    Ambiguity always wins -- a clarify run never burns the plan/gather
    LLM call. Conversational short-circuits to synthesize unconditionally.
    QNT-290: followup short-circuits to synthesize too UNLESS the
    classifier flagged a targeted RAG need on this warm-thread turn
    (``_followup_fires_search``) -- then it routes through plan/gather
    like any other intent so the RAG branch can fire (plan_node still
    returns an empty plan for followup, so no report re-fetch happens).
    QNT-215 exploration owns broad anchored scan prompts even when the
    classifier labels them as news, but named-lens, quick_fact,
    comparison, clarify, and pure follow-up flows keep their existing
    routes.
    """
    if state.get("ambiguity_kind"):
        return "clarify"
    intent = state.get("intent", "thesis")
    if intent == "followup" and deps.followup_fires_search(state):
        return "plan"
    if intent in graph._SHORT_CIRCUIT_INTENTS:
        return "synthesize"
    if intent == "exploration":
        return "explore_supervisor"
    if intent in {"quick_fact", "comparison"}:
        return "plan"
    question = state.get("question", "")
    _, has_prior_turn = graph._prior_turn_context(state, question)
    if graph._should_route_exploration(intent, question, has_prior_turn=has_prior_turn):
        return "explore_supervisor"
    return "plan"
