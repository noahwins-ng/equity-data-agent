"""QNT-294 (AC1): clarify node(s), extracted from build_graph.

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


def clarify_node(state: AgentState, config: RunnableConfig, deps: GraphDeps) -> dict[str, object]:
    """QNT-212: ask the user back when the question is ambiguous.

    Single LLM call into the ConversationalAnswer schema, prompted in the
    ADR-020 analyst voice. Output ``answer`` reads as a clarifying
    question (e.g. "Which ticker did you have in mind?"); ``suggestions``
    carries 0-3 concrete alternatives the user could click. On LLM
    failure the node falls through to a deterministic ``domain_redirect``
    payload so the panel still renders something in-domain — never a
    stack trace.

    Wired by the conditional edge from classify: only reachable when
    ``state['ambiguity_kind']`` is set. Always exits through narrate; narrate
    fires the deterministic clarify lead-in (gated on ``ambiguity_kind``, not on
    the answer shape) above the clarify card ``answer`` holds.
    """
    ticker = state["ticker"]
    question = state.get("question", "")
    ambiguity_kind = state.get("ambiguity_kind")

    prompt = graph.build_clarify_prompt(
        ambiguity_kind=str(ambiguity_kind) if ambiguity_kind else "needs_ticker",
        question=question,
        ticker=ticker,
        tickers=graph.TICKERS,
    )
    conversational = graph._structured_call(
        graph.ConversationalAnswer, prompt, config, "clarify-prompt"
    )
    if conversational is None:
        fallback = graph.domain_redirect(
            reason=graph._CLARIFY_FALLBACK_REASON.get(
                str(ambiguity_kind), "I had trouble interpreting that."
            ),
            tickers=graph.TICKERS,
        )
        logger.info(
            "clarify %s: fallback to domain_redirect (%s)",
            ticker,
            ambiguity_kind,
        )
        # QNT-307: set only ``answer`` narrowly (not via project_answer, which
        # carries no extra keys anyway) -- the clarify card is the fallback
        # redirect; narrate speaks the deterministic lead-in above it.
        return {"answer": fallback}
    # QNT-244: keep clarify suggestions concrete and in-scope. The
    # needs_second_ticker branch biases to comparison pairs; needs_ticker
    # to a balanced mix; needs_prior_turn legitimately carries none.
    conversational = graph._with_coerced_suggestions(
        conversational, hint=graph._CLARIFY_SUGGESTION_HINT.get(str(ambiguity_kind))
    )
    logger.info("clarify %s: ambiguity_kind=%s clarify=ok", ticker, ambiguity_kind)
    return {"answer": conversational}
