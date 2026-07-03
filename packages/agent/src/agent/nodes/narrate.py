"""QNT-294 (AC1): narrate node(s), extracted from build_graph.

Module-level function(s) taking ``(state, config, deps)`` -- unit-testable
without compiling a graph. Every ``agent.graph``-provided helper/constant is
referenced via the ``graph`` module object (attribute access at call time), so
the tests' ``monkeypatch.setattr(graph, "<name>", ...)`` seams keep working.
Build-time wiring comes in via ``deps`` (:class:`agent.nodes.deps.GraphDeps`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig

from agent import graph

logger = logging.getLogger("agent.graph")

if TYPE_CHECKING:
    from agent.graph import AgentState
    from agent.nodes.deps import GraphDeps


def narrate_node(state: AgentState, config: RunnableConfig, deps: GraphDeps) -> dict[str, object]:
    """QNT-211: stream a 1-4 sentence analyst-voice paragraph that wraps
    whichever structured payload synthesize produced.

    Tokens stream out via ``event_emitter("narrative_chunk", {"delta": ...})``
    so the chat panel can render a prose bubble above the card before the
    card composes. On any LLM failure we log, set ``narrative=None``, and
    terminate normally — the structured card still renders.

    Conversational intent skips this node: that path's answer is already
    prose, so re-narrating would just echo it. QNT-232 #3: quick_fact skips
    it too -- its card answer is already analyst-voice prose, so the second
    70b call only paraphrased the card. Followup narrative-only path
    (synthesize set ``quick_fact=None``) routes through here and produces the
    only spoken response the user sees.
    """
    intent = state.get("intent", "thesis")
    ticker = state["ticker"]
    question = state.get("question", "")
    # QNT-232 #13: same intent-aware history budget the synthesize node uses.
    history_budget = graph._history_budget(str(intent))

    # Conversational already speaks in the right voice -- nothing to
    # narrate over. Same applies to ANY synthesize fallback-redirect
    # path: when synthesize hits _fallback() for a thesis/focused/
    # quick_fact/comparison failure it leaves the original ``intent``
    # intact AND populates ``state['conversational']`` with the
    # deterministic domain_redirect. Without this guard narrate would
    # re-narrate the redirect prose, producing a duplicate bubble above
    # the same conversational card. Gate on the payload, not just the
    # intent.
    #
    # QNT-212: the clarify path also populates ``state['conversational']``
    # (with the asked-back question) but is NOT a fallback -- AC3 wants
    # narrate to fire so the bubble streams above the clarify card.
    # ``ambiguity_kind`` is the distinguishing signal: only the clarify
    # route sets it.
    #
    # QNT-220 follow-up: gate ONLY on ``not is_clarify``. Previously the
    # leading ``intent == "conversational"`` clause meant a clarify turn the
    # classifier happened to label conversational skipped narrate (no
    # bubble), while the same ambiguous question labeled thesis got one --
    # the bubble flickered in/out with the classifier label. A clarify turn
    # always gets the (now non-restating, engaging) lead-in; genuine
    # conversational greetings and synthesize fallback-redirects still skip.
    is_clarify = state.get("ambiguity_kind") is not None
    # QNT-294 (AC2): read the answer union -- a ConversationalAnswer payload
    # is either a genuine conversational turn or a synthesize fallback
    # redirect (both skip narrate); clarify is distinguished by is_clarify.
    if not is_clarify and (
        intent == "conversational" or isinstance(state.get("answer"), graph.ConversationalAnswer)
    ):
        return {
            "narrative": None,
            "messages": graph._append_assistant_message(state, None),
        }

    # QNT-220 follow-up: clarify turns get a DETERMINISTIC lead-in, never an
    # LLM narration. No reports were gathered on a clarify turn, so letting
    # the narrator speak invents a stance (prod: "the read is constructive
    # for NVDA" with zero data). Emit a content-free readiness line keyed to
    # the ambiguity kind; the clarify card below owns the actual question.
    if is_clarify:
        lead_in = graph._CLARIFY_LEAD_IN.get(
            str(state.get("ambiguity_kind")), graph._CLARIFY_LEAD_IN_DEFAULT
        )
        if deps.event_emitter is not None:
            try:
                deps.event_emitter("narrative_chunk", {"delta": lead_in})
            except Exception as exc:  # noqa: BLE001 — never let SSE plumbing crash narrate
                logger.warning("narrate %s: clarify emit failed: %s (continuing)", ticker, exc)
        return {
            "narrative": lead_in,
            "messages": graph._append_assistant_message(state, lead_in),
        }

    # QNT-232 #3 (option a): quick_fact skips narrate. The QuickFactAnswer
    # card is already a one-or-two-sentence analyst-voice answer + cited
    # value, so a second 70b call would only paraphrase the card it sits
    # above (the v5 voice/card review measured near-total overlap; quick_fact
    # has no probe close). Dropping it makes a quick_fact turn exactly one
    # default-alias LLM call (synthesize) -- mirrors the conversational gate
    # above. Gated on the card actually landing: a quick_fact whose
    # synthesize failed sets ``conversational`` (caught by the gate above) or
    # leaves quick_fact None, in which case there is no surface to skip for.
    # The followup path reuses QuickFactAnswer but keeps intent="followup",
    # so it is unaffected and still narrates.
    #
    # QNT-296: skipping the LLM call must not skip the grounding check
    # that call's tail would otherwise have produced -- _quick_fact_grounding
    # runs the same _runtime_grounding_check / _composite_confidence pair
    # against the card's own markdown so the confidence chip is never
    # coverage-only for the one shape whose entire contract is a single
    # cited number.
    answer_obj = state.get("answer")
    quick_fact_answer = answer_obj if isinstance(answer_obj, graph.QuickFactAnswer) else None
    if intent == "quick_fact" and quick_fact_answer is not None:
        return {
            "narrative": None,
            "messages": graph._append_assistant_message(state, None),
            **graph._quick_fact_grounding(state, quick_fact_answer),
        }

    # Pick the structured payload to summarise (QNT-294 AC2). A followup
    # narrative-only turn carries answer=None but reuses the hydrated prior
    # ``thesis`` as its substrate, so it takes priority in the ``or`` -- for
    # every other turn ``thesis`` is either the answer itself or cleared to
    # None, so this reduces to the single answer union. narrator reads the
    # same markdown the panel renders.
    payload_obj: object | None = state.get("thesis") or answer_obj
    payload_markdown = ""
    to_md: Any = getattr(payload_obj, "to_markdown", None)
    if callable(to_md):
        try:
            payload_markdown = graph._strip_disclaimer(str(to_md()))
        except Exception:  # noqa: BLE001 — never let formatting kill narrate
            payload_markdown = ""

    # Followup narrative-only path: quick_fact is None but a prior thesis
    # is hydrated. Feed the prior thesis as the substrate the narrator
    # reacts to. For other intents the payload above already carries
    # everything narrate needs.
    prior_thesis_markdown: str | None = None
    if intent == "followup" and not payload_markdown:
        prior_thesis = state.get("thesis")
        prior_to_md: Any = getattr(prior_thesis, "to_markdown", None)
        if callable(prior_to_md):
            try:
                prior_thesis_markdown = graph._strip_disclaimer(str(prior_to_md()))
            except Exception:  # noqa: BLE001
                prior_thesis_markdown = None

    # QNT-290: a flagged followup ran gather THIS turn and folded fresh
    # search hits into reports["news"]/["fundamental"]. Surface that
    # evidence to the narrator on top of whatever payload_markdown /
    # prior_thesis_markdown already carry (typically the prior turn's
    # thesis) so the spoken answer reaches the new headline/excerpt, not
    # just the old framing. Gated on the corpus tags in THIS turn's
    # retrieved_sources (not just report presence) so a search that fired
    # but found zero hits doesn't pad the prompt with the unchanged
    # canned digest, and a pure followup (gather never ran) can't leak a
    # stale hydrated report in.
    if intent == "followup" and "gather" in (state.get("intent_path") or []):
        followup_corpora = {
            str(source.get("corpus")) for source in (state.get("retrieved_sources") or [])
        }
        followup_reports = state.get("reports") or {}
        retrieval_blocks = [
            str(followup_reports[report_key])
            for report_key, corpus in (("news", "news"), ("fundamental", "earnings"))
            if corpus in followup_corpora and followup_reports.get(report_key)
        ]
        if retrieval_blocks:
            retrieval_text = "\n\n".join(retrieval_blocks)
            payload_markdown = (
                f"{payload_markdown}\n\n{retrieval_text}" if payload_markdown else retrieval_text
            )

    # QNT-226/276: narrative-only substrate. When synthesize dropped the
    # focused card (search fired + hits, focused=None), no structured payload
    # exists, so feed the gathered report -- which now LEADS with the folded
    # RAG block -- as the substrate the narrator speaks from. Without this the
    # prompt would say "no structured payload -- speak from the prior turn"
    # and the narrator would invent an answer with nothing to ground it.
    # news -> reports["news"]; fundamental (earnings) -> reports["fundamental"].
    # The guards mirror the synthesize-side drop condition exactly so this
    # only fires on the path synthesize dropped the card for. The runtime
    # grounding check below runs against the same reports, so ADR-003 numeric
    # grounding still applies to the spoken answer.
    dropped_focus_report: str | None = None
    if not payload_markdown and state.get("retrieved_sources"):
        if intent == "news" and state.get("needs_news_search"):
            dropped_focus_report = "news"
        elif intent == "fundamental" and state.get("needs_earnings_search"):
            dropped_focus_report = "fundamental"
    if dropped_focus_report is not None:
        report_body = (state.get("reports") or {}).get(dropped_focus_report)
        if report_body:
            payload_markdown = str(report_body)

    prompt = graph.build_narrate_prompt(
        intent=str(intent),
        ticker=ticker,
        question=question,
        payload_markdown=payload_markdown,
        prior_thesis_markdown=prior_thesis_markdown,
        plan_rationale=state.get("plan_rationale"),
        history=graph._history_before_current(
            state.get("messages"), question, max_turns=history_budget
        ),
        is_clarify=is_clarify,
    )

    try:
        chunks: list[str] = []
        stream = graph.get_llm(temperature=0.3).stream(prompt, config=config)
        for chunk in stream:
            delta_obj = getattr(chunk, "content", None)
            if delta_obj is None:
                continue
            delta = str(delta_obj)
            if not delta:
                continue
            chunks.append(delta)
            if deps.event_emitter is not None:
                try:
                    deps.event_emitter("narrative_chunk", {"delta": delta})
                except Exception as exc:  # noqa: BLE001 — never let SSE plumbing crash narrate
                    logger.warning(
                        "narrate %s: event_emitter failed: %s (continuing)",
                        ticker,
                        exc,
                    )
        narrative = "".join(chunks).strip()
    except Exception as exc:  # noqa: BLE001 — narrate is best-effort; card still renders
        logger.warning(
            "narrate %s: stream failed: %s: %s",
            ticker,
            type(exc).__name__,
            exc,
        )
        return {
            "narrative": None,
            "messages": graph._append_assistant_message(state, None),
        }

    logger.info("narrate %s: narrative_chars=%d", ticker, len(narrative))
    final_narrative = narrative or None
    coverage = float(state.get("confidence", 0.0))
    grounding_answer = "\n\n".join(
        text for text in (final_narrative, payload_markdown, prior_thesis_markdown) if text
    )
    grounding_result, grounding_rate = graph._runtime_grounding_check(
        grounding_answer,
        graph._runtime_report_texts(state),
    )
    confidence = graph._composite_confidence(coverage, grounding_rate)
    if grounding_result.ok:
        logger.info(
            "narrate %s: grounding_rate=%s confidence=%s",
            ticker,
            grounding_rate,
            confidence,
        )
    else:
        logger.warning(
            "narrate %s: grounding miss rate=%s unsupported=%s confidence=%s",
            ticker,
            grounding_rate,
            grounding_result.reason(),
            confidence,
        )
    return {
        "narrative": final_narrative,
        "messages": graph._append_assistant_message(state, final_narrative),
        "grounding_rate": grounding_rate,
        "grounding_unsupported": list(grounding_result.unsupported),
        "confidence": confidence,
    }
