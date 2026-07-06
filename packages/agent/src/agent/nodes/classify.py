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
    # QNT-307: snapshot the prior turn's Thesis at the turn boundary, replacing
    # the retired ``thesis`` slot the followup path used to lean on. classify is
    # the entry node, so ``state.get("answer")`` here is the checkpointer-hydrated
    # answer from the PRIOR turn (this turn has written nothing yet). This
    # reproduces the old ``thesis``-slot lifetime EXACTLY -- only a Thesis is ever
    # carried (every non-thesis intent went through project_answer, which nulled
    # the slot), and a narrative-only followup (answer=None) preserves the earlier
    # Thesis across the chain (followup returns never cleared it). Carrying a
    # non-thesis payload here would feed it into build_followup_prompt's "earlier
    # thesis" section and narrate's prior substrate -- a synthesis behaviour change
    # the ticket puts out of scope. synthesize/narrate read ``prior_answer`` to
    # reason over the earlier turn while they overwrite ``answer`` mid-run.
    hydrated_answer = state.get("answer")
    if isinstance(hydrated_answer, graph.Thesis):
        prior_answer = hydrated_answer
    elif hydrated_answer is None:
        prior_answer = state.get("prior_answer")
    else:
        prior_answer = None
    # QNT-320 (G-2): compute the routing decision ONCE here so ``_classify_router``
    # is a pure state read. ``followup_fires_search`` reads this turn's freshly
    # resolved intent + needs_*_search flags -- classify has not written its return
    # yet, so overlay them onto ``state`` for the check.
    followup_fires = intent == "followup" and deps.followup_fires_search(
        {
            **state,
            "intent": intent,
            "needs_news_search": needs_news_search,
            "needs_earnings_search": needs_earnings_search,
        }
    )
    route = _classify_route(intent, ambiguity_kind, followup_fires)
    # QNT-323 (G-4): classify owns the whole turn boundary. ``_turn_boundary_reset``
    # clears every per-turn scratch key (plan / plan_rationale / errors /
    # reports_by_ticker / comparison_tickers / retrieved_sources / confidence /
    # grounding_* / supervisor_iterations / comparison_rag_demand, plus reports for
    # non-followup intents) so a prior turn's value can't leak across the
    # checkpointer into this one. Downstream
    # nodes no longer carry defensive resets -- they return only what they
    # produce, overwriting these within the same turn. The keys below are the ones
    # classify itself produces; they never overlap the scratch set.
    return {
        **graph._turn_boundary_reset(intent),
        "ticker": effective_ticker,
        "analysis_ticker": effective_ticker,
        "prior_answer": prior_answer,
        "intent": intent,
        "route": route,
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


def _classify_route(intent: str, ambiguity_kind: object, followup_fires: bool) -> str:
    """QNT-212/QNT-215/QNT-290/QNT-320: the routing decision, computed once in
    classify_node (:func:`_classify_router` is now a pure read of the result).

    Ambiguity always wins -- a clarify run never burns the plan/gather LLM call.
    Conversational short-circuits to synthesize unconditionally. QNT-290: followup
    short-circuits too UNLESS the classifier flagged a targeted RAG need this
    warm-thread turn (``followup_fires``) -- then it routes through plan/gather so
    the RAG branch can fire (plan_node still returns an empty plan for followup, so
    no report re-fetch happens). QNT-215 exploration owns broad anchored scan
    prompts; every other intent (thesis / focused / quick_fact / comparison) plans.

    QNT-320 (G-2): there is no second ``_should_route_exploration`` re-check here.
    classify_node already rewrote the intent to "exploration" when that predicate
    fired, so the old router's re-check could never be True for a non-exploration
    intent -- it was dead code that read like a live path, and is deleted.
    """
    if ambiguity_kind:
        return "clarify"
    if intent == "followup" and followup_fires:
        return "plan"
    if intent in graph._SHORT_CIRCUIT_INTENTS:
        return "synthesize"
    if intent == "exploration":
        return "explore_supervisor"
    return "plan"


def _classify_router(state: graph.AgentState) -> str:
    """QNT-320 (G-2): return the route classify_node computed into ``state['route']``.

    A pure state read -- no predicate calls. Defaults to "plan" for a stubbed test
    graph or old checkpoint that predates the key (classify runs first on every
    real turn, so ``route`` is always present in practice).

    The ``state`` hint is spelled ``graph.AgentState`` (the runtime-imported module)
    rather than the ``TYPE_CHECKING``-only ``AgentState`` because, unlike the
    partial-wrapped node functions, this router is registered directly on the graph
    -- LangGraph resolves its annotations at build time, so the name must exist at
    runtime.
    """
    return state.get("route", "plan")
