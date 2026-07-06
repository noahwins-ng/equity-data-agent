"""QNT-294 (AC1): plan node(s), extracted from build_graph.

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


def plan_node(state: AgentState, config: RunnableConfig, deps: GraphDeps) -> dict[str, object]:
    ticker = state["ticker"]
    question = state.get("question", "")
    intent = state.get("intent", "thesis")

    # QNT-209: followup reuses the prior turn's hydrated reports — set plan
    # empty so gather no-ops. QNT-323 (G-4): classify already reset the scratch
    # keys and kept ``reports`` hydrated for followup, so plan carries ONLY the
    # empty plan it produces here.
    if intent == "followup":
        logger.info("plan %s: skipped (followup)", ticker)
        return {"plan": []}

    # Conversational path skips tool gathering entirely — the answer
    # comes from the LLM with no report context. We still pass through
    # plan_node so the graph topology stays linear; the gather node
    # then no-ops when ``plan`` is empty.
    # QNT-323 (G-4): these paths produce only an empty plan; plan_rationale and
    # comparison_tickers are classify's turn-boundary reset to own (both are in
    # the scratch set), so carry only ``plan`` -- same shape as the followup
    # branch above.
    if intent == "conversational":
        logger.info("plan %s: skipped (conversational)", ticker)
        return {"plan": []}

    available = [t for t in graph.REPORT_TOOLS if t in deps.tools]
    if not available:
        logger.warning("plan %s: no tools registered", ticker)
        return {"plan": []}

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
        named = graph.extract_tickers(question)
        if len(named) > graph._MAX_COMPARISON_TICKERS:
            logger.info(
                "plan %s: comparison named %d tickers (>%d) — synthesize will redirect",
                ticker,
                len(named),
                graph._MAX_COMPARISON_TICKERS,
            )
        else:
            comparison_tickers = graph._resolve_comparison_tickers(ticker, question)
            if len(comparison_tickers) < graph._MIN_COMPARISON_TICKERS:
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
    if intent in graph._FOCUSED_REPORT:
        wanted = ("company", graph._FOCUSED_REPORT[intent])
        plan = [t for t in available if t in wanted]
        plan_rationale = None
    elif intent == "quick_fact":
        prompt = graph._build_plan_prompt(ticker, question, available, intent)
        # QNT-220 (#7): plan is a small structured/list call -> small alias.
        response = graph.get_llm(temperature=0.0, model_alias=graph.SMALL_NODE_ALIAS).invoke(
            prompt, config=config
        )
        content = response.content if hasattr(response, "content") else str(response)
        plan = graph._parse_plan(str(content), available, intent)
        plan_rationale = None
    elif intent == "thesis":
        prompt = graph._build_thesis_plan_prompt(ticker, question, available)
        # QNT-220 (#7): thesis-plan selection is a small structured call -> small alias.
        # QNT-294 (AC5): shares the one _structured_call ladder; the planner is
        # the non-linked, small-alias caller (no registered Langfuse prompt).
        thesis_plan = graph._structured_call(
            graph.ThesisPlan,
            prompt,
            config,
            f"plan {ticker}",
            llm=graph.get_llm(temperature=0.0, model_alias=graph.SMALL_NODE_ALIAS),
            linked=False,
        )
        if thesis_plan is None:
            logger.warning(
                "plan %s: thesis plan unavailable for question %r; falling back to all tools",
                ticker,
                question,
            )
            plan = list(available)
            plan_rationale = None
        else:
            plan = graph._tools_from_thesis_plan(thesis_plan, available)
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
    # QNT-298: surface the analyst-voice plan rationale over SSE as soon
    # as it resolves -- BEFORE gather's tool calls fire -- so the panel
    # can fill the gather->synthesize dead air with a real sentence
    # instead of a generic spinner. None (quick_fact/focused/comparison
    # plans) emits nothing; narrate's own consumption of plan_rationale
    # is unchanged.
    if plan_rationale is not None and deps.event_emitter is not None:
        try:
            deps.event_emitter("plan_rationale", {"text": plan_rationale})
        except Exception as exc:  # noqa: BLE001 — never let SSE plumbing crash the graph
            logger.warning("plan %s: event_emitter failed: %s (continuing)", ticker, exc)
    # QNT-323 (G-4): carry only the keys plan produces (plan / plan_rationale /
    # comparison_tickers). reports / errors / reports_by_ticker are gather's to
    # populate; classify already reset them at the turn boundary.
    return {
        "plan": plan,
        "plan_rationale": plan_rationale,
        "comparison_tickers": comparison_tickers,
    }
