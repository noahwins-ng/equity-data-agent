"""QNT-294 (AC1): synthesize node(s), extracted from build_graph.

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
from pydantic import BaseModel

from agent import graph

logger = logging.getLogger("agent.graph")

if TYPE_CHECKING:
    from agent.graph import AgentState
    from agent.nodes.deps import GraphDeps


def _synthesize_payload(state: AgentState, config: RunnableConfig) -> dict[str, object]:
    ticker = state["ticker"]
    question = state.get("question", "")
    reports = state.get("reports", {})
    errors = state.get("errors", {})
    plan = state.get("plan", [])
    intent = state.get("intent", "thesis")
    confidence = graph._confidence_from_reports(reports, plan)
    # QNT-232 #13: intent-aware history budget for every prompt this node
    # assembles (fresh analytical asks trim to a few turns; continuations
    # keep the full HISTORY_TURN_LIMIT).
    history_budget = graph._history_budget(str(intent))

    # QNT-294 / QNT-307: project a synthesized payload into the answer state via
    # the single ``project_answer`` writer (writes the one ``answer`` field) and
    # attach this run's confidence. Each branch below returns ``_answer(payload)``
    # so no branch hand-assembles the dict -- the union enforces exactly-one.
    def _answer(payload: graph.AnswerPayload | None) -> dict[str, object]:
        # QNT-320 (G-1): a card-bearing path names no narrate substrate -- clear the
        # key so a prior drop-card turn's "news"/"fundamental" can't persist through
        # the checkpointer and be mis-read next turn. synthesize writes
        # narrative_substrate on EVERY return path (fresh each turn), keeping it a
        # true single-writer key with no cross-turn staleness for narrate to guard.
        return {
            **graph.project_answer(payload),
            "confidence": confidence,
            "narrative_substrate": None,
        }

    # Helper: deterministic fallback when a path can't produce its
    # primary payload. Used by every branch below — the panel never
    # sees a blank state.
    def _fallback(reason: str) -> dict[str, object]:
        logger.info(
            "synthesize %s: fallback to conversational redirect (%s)",
            ticker,
            reason,
        )
        return _answer(
            graph.domain_redirect(
                reason=reason,
                tickers=graph.TICKERS,
                hint=graph._hint_from_intent(intent),
            )
        )

    # QNT-209: followup reasons over the prior turn's hydrated reports
    # via a single LLM call into QuickFactAnswer. We deliberately reuse
    # an existing structured shape rather than mint a new one — the
    # frontend already renders QuickFactAnswer and the AC explicitly
    # forbids introducing a new schema for followup.
    if intent == "followup":
        # QNT-307: the prior turn's answer, snapshotted into ``prior_answer`` by
        # classify_node (replaces the old hydrated ``thesis`` slot). None on a
        # cold thread or an old-shape checkpoint with no ``answer`` -- reports are
        # usually still hydrated, so the branch below proceeds and degrades the
        # prior-answer framing rather than the whole turn.
        prior_thesis = state.get("prior_answer")
        if not reports and not prior_thesis:
            # No prior context to reason over — degrade to the redirect
            # so the user sees an in-domain reply rather than blank.
            return _fallback("I don't have a prior turn on this thread to follow up on.")
        # QNT-211: narrative-only followup path. If the question is pure
        # conversational continuation ("what does that mean for retail?")
        # rather than a metric ask ("what's the P/E?"), skip the
        # QuickFactAnswer LLM call entirely -- narrate owns the response
        # and no quick_fact event fires downstream. The frontend renders
        # the bubble alone, no card.
        if not graph._followup_is_metric_ask(question):
            logger.info(
                "synthesize %s: followup narrative-only (no metric ask)",
                ticker,
            )
            followup_confidence = 1.0 if reports else confidence
            # QNT-307: clear this turn's card (answer=None). ``prior_answer`` is a
            # separate channel classify already set, so nulling ``answer`` here no
            # longer risks the prior-turn substrate the next followup reuses.
            # QNT-320 (G-1): narrate speaks from the prior turn's answer on this
            # path (picked via _pick_payload) -- record the substrate so narrate
            # reads the decision from state rather than re-deriving it.
            return {
                "answer": None,
                "confidence": followup_confidence,
                "narrative_substrate": "prior_answer",
            }
        prompt = graph.build_followup_prompt(
            ticker,
            question,
            reports,
            prior_thesis,
            history=graph._history_before_current(
                state.get("messages"), question, max_turns=history_budget
            ),
        )
        followup = graph._structured_call(graph.QuickFactAnswer, prompt, config, "followup-prompt")
        if followup is None:
            return _fallback("I had trouble building a follow-up answer for that.")
        # plan is empty on followup runs, so the report-coverage
        # heuristic would render 0% confidence -- a misleading chip
        # given we reused EVERY hydrated report. Treat reuse as full
        # coverage.
        followup_confidence = 1.0 if reports else confidence
        logger.info("synthesize %s: confidence=%s followup=ok", ticker, followup_confidence)
        # QNT-307: write this turn's card to ``answer``; ``prior_answer`` (the
        # earlier turn's substrate) is a separate channel classify owns, so this
        # write can no longer clobber the next followup's prior context.
        # QNT-320 (G-1): card-bearing path -- clear narrative_substrate (see _answer).
        return {
            "answer": followup,
            "confidence": followup_confidence,
            "narrative_substrate": None,
        }

    if intent == "conversational":
        # QNT-217: thread prior conversation into the conversational
        # prompt. When history exists, build_conversational_prompt selects
        # the warm-thread system prompt -- it stays in the latest analysis
        # context and suppresses the cold-start capability card. A fresh
        # thread (no history) keeps the cold capability response.
        prompt = graph.build_conversational_prompt(
            question,
            history=graph._history_before_current(
                state.get("messages"), question, max_turns=history_budget
            ),
        )
        conversational = graph._structured_call(
            graph.ConversationalAnswer,
            prompt,
            config,
            "conversational-prompt",
            # QNT-258 follow-up: force function_calling so DeepSeek cannot return
            # the reply as bare prose (json_invalid on the default json_schema).
            method="function_calling",
        )
        if conversational is None:
            # Deterministic redirect when the LLM itself fails — the
            # whole point of this path is the user always gets prose.
            return _fallback("I had trouble answering that.")
        # QNT-244: the prose answer is LLM-generated, but the clickable
        # suggestions must be concrete answerable prompts. Replace generic
        # placeholder lists ("trend for a specific stock?") with
        # deterministic in-scope picks so a clicked starter never routes to
        # clarify.
        conversational = graph._with_coerced_suggestions(conversational, hint=None)
        logger.info("synthesize %s: confidence=%s conversational=ok", ticker, confidence)
        return _answer(conversational)

    if intent == "comparison":
        comparison_tickers = state.get("comparison_tickers", [])
        reports_by_ticker = state.get("reports_by_ticker", {})
        if len(comparison_tickers) < graph._MIN_COMPARISON_TICKERS:
            # QNT-224: distinguish "too many" (5+, plan emptied the list)
            # from "couldn't find two". Re-read the named count so the
            # redirect tells the user the actual constraint.
            if len(graph.extract_tickers(question)) > graph._MAX_COMPARISON_TICKERS:
                return _fallback(
                    "I can compare up to four tickers at a time — "
                    "pick the four you care about most."
                )
            return _fallback(
                "I can compare two tickers I cover, but I couldn't find two in your question."
            )

        # QNT-224: 3-4 tickers take the lean metrics path. The table is
        # pure pre-computed data (math in SQL, formatted in the API), so
        # per ADR-003 there is NO LLM synthesis call — we parse the metrics
        # JSON gather stashed and build the answer deterministically. The
        # narrate node speaks the qualitative contrast over to_markdown().
        if len(comparison_tickers) > graph._MIN_COMPARISON_TICKERS:
            metrics_json = (state.get("reports") or {}).get("comparison_metrics")
            lean = graph._build_lean_comparison(metrics_json, comparison_tickers)
            if lean is None:
                return _fallback("I couldn't pull comparison metrics right now.")
            logger.info(
                "synthesize %s: confidence=%s comparison_lean=%s",
                ticker,
                confidence,
                [r.ticker for r in lean.rows],
            )
            return _answer(lean)

        # Need at least one report for each ticker — comparing an empty
        # column to anything is just a half thesis.
        if not all(reports_by_ticker.get(t) for t in comparison_tickers):
            return _fallback("I couldn't pull reports for both of those tickers right now.")

        prompt = graph.build_comparison_prompt(
            comparison_tickers,
            question,
            reports_by_ticker,
            history=graph._history_before_current(
                state.get("messages"), question, max_turns=history_budget
            ),
        )
        comparison = graph._structured_call(
            graph.ComparisonAnswer, prompt, config, "comparison-prompt"
        )
        if comparison is None:
            return _fallback("I had trouble building that comparison.")
        logger.info(
            "synthesize %s: confidence=%s comparison=%s",
            ticker,
            confidence,
            [s.ticker for s in comparison.sections],
        )
        return _answer(comparison)

    if intent == "exploration":
        # QNT-220 follow-up: a broad anchored scan rendered as the
        # dedicated verdict-free, multi-lens exploration card. Mirrors the
        # focused path's structured-output + fallback contract.
        if not reports:
            return _fallback("I couldn't pull any reports to scan for that right now.")
        prompt = graph.build_exploration_prompt(
            ticker,
            question,
            reports,
            history=graph._history_before_current(
                state.get("messages"), question, max_turns=history_budget
            ),
        )
        exploration = graph._structured_call(
            graph.ExplorationAnswer, prompt, config, "exploration-prompt"
        )
        if exploration is None:
            return _fallback("I had trouble pulling that scan together.")
        logger.info("synthesize %s: confidence=%s exploration=ok", ticker, confidence)
        return _answer(exploration)

    if intent in graph._FOCUSED_REPORT:
        focus_report = graph._FOCUSED_REPORT[intent]
        if focus_report not in reports:
            return _fallback("I couldn't pull a report to answer that focused analysis right now.")
        # QNT-226/276: focused read that actually fired a RAG search AND got
        # hits -> lighter shape. Skip the focused-card LLM call and let
        # narrate own the spoken answer (mirrors the QNT-211 followup
        # narrative-only path: synthesize returns the payload slot as None,
        # narrate speaks). The retrieved-sources list renders below the voice
        # as the structured surface, so the user still sees the hits even if
        # narrate degrades. QNT-276 extends this from the news focus
        # (search_news) to the fundamental focus (search_earnings), so an
        # earnings-narrative ask foregrounds the retrieved 8-K excerpt the
        # same way instead of synthesizing a valuation card around it. Gated
        # on retrieved_sources being non-empty: a degraded search (Qdrant
        # down, zero hits) keeps the full focused card, since the canned
        # digest is then the only substrate and the card is the right shape.
        fired_search = (intent == "news" and state.get("needs_news_search")) or (
            intent == "fundamental" and state.get("needs_earnings_search")
        )
        if fired_search and state.get("retrieved_sources"):
            logger.info(
                "synthesize %s: %s narrative-only (focused card dropped)",
                ticker,
                intent,
            )
            # QNT-307: no card this turn -- clear ``answer``; narrate speaks from
            # the retrieved sources.
            # QNT-320 (G-1): record which folded report narrate speaks from
            # (``focus_report`` is "news" / "fundamental" here) so narrate no
            # longer re-derives the needs_*_search drop predicate.
            return {
                "answer": None,
                "confidence": confidence,
                "narrative_substrate": focus_report,
            }
        prompt = graph.build_focused_prompt(
            intent,
            ticker,
            question,
            reports,
            history=graph._history_before_current(
                state.get("messages"), question, max_turns=history_budget
            ),
        )
        focused = graph._structured_call(graph.FocusedAnalysis, prompt, config, "focused-prompt")
        if focused is None:
            return _fallback("I had trouble pulling that focused analysis together.")
        # Re-assert the focus discriminator from intent — defends against
        # a misbehaving provider that echoed the wrong literal back.
        # Re-validate rather than model_copy (which skips validators) so
        # QNT-302's verdict-family / news-field normalization tracks the
        # corrected focus.
        if focused.focus != intent:
            focused = graph.FocusedAnalysis.model_validate(
                {**focused.model_dump(), "focus": intent}
            )
        logger.info(
            "synthesize %s: confidence=%s focused=%s",
            ticker,
            confidence,
            intent,
        )
        return _answer(focused)

    if intent == "quick_fact":
        if not reports:
            return _fallback("I couldn't pull a report to answer that quick fact right now.")
        prompt = graph.build_quick_fact_prompt(
            ticker,
            question,
            reports,
            history=graph._history_before_current(
                state.get("messages"), question, max_turns=history_budget
            ),
            errors=errors,
        )
        quick_fact = graph._structured_call(
            graph.QuickFactAnswer, prompt, config, "quick-fact-prompt"
        )
        if quick_fact is None:
            return _fallback("I had trouble pulling a single answer to that.")
        logger.info(
            "synthesize %s: confidence=%s quick_fact=ok",
            ticker,
            confidence,
        )
        return _answer(quick_fact)

    # Default thesis path
    if not reports:
        return _fallback("I couldn't pull any reports for that ticker right now.")
    prompt = graph.build_synthesis_prompt(
        ticker,
        question,
        reports,
        history=graph._history_before_current(
            state.get("messages"), question, max_turns=history_budget
        ),
        errors=errors,
    )
    # ``_structured_call(Thesis, ...)`` forces the LLM into the four-section
    # schema. Errors from a misbehaving provider (Gemini occasionally
    # returns malformed tool-call JSON) surface as a fallback redirect
    # rather than crashing the whole run. The shared retry policy recovers
    # transient parse failures (measured at 5.5% on this branch — QNT-196).
    thesis = graph._structured_call(graph.Thesis, prompt, config, "system-prompt")
    if thesis is None:
        return _fallback("I had trouble pulling a thesis together for that.")
    logger.info("synthesize %s: confidence=%s thesis=ok", ticker, confidence)
    return _answer(thesis)


def synthesize_node(
    state: AgentState, config: RunnableConfig, deps: GraphDeps
) -> dict[str, object]:
    """QNT-229 #2b: run synthesis, then emit the structured card through the
    ``event_emitter`` the moment it is ready -- BEFORE narrate streams the
    analyst-voice bubble above it.

    Moves the card forward by roughly one narrate duration: the SSE wrapper
    relays the emitted event verbatim, so the panel renders the card while
    the narrative is still streaming. The post-graph emission in
    agent_chat.py stays as the idempotent safety net (the panel's
    ``updateRun`` overwrites with the same payload). Conversational and
    fallback-redirect shapes carry no card slot, so nothing is emitted
    early for them -- their prose still streams via ``prose_chunk``.
    """
    result = _synthesize_payload(state, config)
    if deps.event_emitter is not None and isinstance(result, dict):
        # QNT-305 follow-up: strip untrustworthy retrieved anchors (out of
        # range OR wrong-corpus) from the EARLY card emit too, with the same
        # gate as the post-graph strip in agent_chat (``intent_path`` already
        # carries "gather" here, appended by the node wrapper before synthesize
        # runs). Without this the early card renders a bad anchor that the
        # stripped post-graph emit then removes -- the card's own flicker, the
        # twin of the narrate one.
        intent_path = state.get("intent_path") or []
        anchor_sources = state.get("retrieved_sources") or [] if "gather" in intent_path else []
        # QNT-294 (AC2): read the single answer union; its shape's slot name
        # is the SSE event name. conversational carries no card (streams as
        # prose_chunk), so it is skipped.
        payload = result.get("answer")
        if isinstance(payload, BaseModel):
            slot = graph.answer_slot(payload)
            if slot is not None and slot != "conversational":
                try:
                    deps.event_emitter(
                        slot, graph.strip_bad_anchors_in_obj(payload.model_dump(), anchor_sources)
                    )
                except Exception as exc:  # noqa: BLE001 — never let SSE plumbing crash synthesize
                    logger.warning(
                        "synthesize %s: card emit (%s) failed: %s (continuing)",
                        state.get("ticker", "?"),
                        slot,
                        exc,
                    )
    return result
