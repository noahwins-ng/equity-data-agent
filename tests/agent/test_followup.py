"""Tests for the QNT-209 followup intent + SqliteSaver wiring.

Covers the four scenarios the ticket calls out:

1. Same thread_id, second turn = "why?" → intent == followup, gather is
   NOT called, response is a structured QuickFactAnswer.
2. Fresh thread_id, first turn = "why?" → falls through to thesis (no
   prior turn anchors the pronoun), gather IS called.
3. Same thread_id, second turn = "what's NVDA's RSI?" → quick_fact intent
   (named ticker + metric), not followup.
4. Heuristic-only check: bare "why?" without prior turn → None (defers
   to LLM); with prior turn → "followup".

We use an in-memory SqliteSaver (`:memory:`) so the test exercises the
real persistence layer end-to-end without touching disk.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from unittest.mock import MagicMock

import pytest
from agent import graph as graph_module
from agent.graph import _resolve_single_ticker_context, build_graph
from agent.intent import _heuristic_intent
from agent.prompts import RETRIEVED_NEWS_HEADING
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver


def _stub_thesis() -> Thesis:
    from ._thesis_factory import make_thesis

    return make_thesis(
        company_summary="TSLA framing (source: company).",
        supports=["EV/EBITDA 65 (source: fundamental)"],
        challenges=["RSI 78 overbought (source: technical)"],
        verdict="Underweight",
        verdict_rationale="Premium multiple paired with Uptrend exhaustion (source: technical).",
    )


def _stub_quick_fact() -> QuickFactAnswer:
    return QuickFactAnswer(
        answer=(
            "Premium fundamentals collide with an overbought technical "
            "(source: fundamental, technical)."
        ),
        cited_value="78",
        source="technical",
    )


class _StubLLM:
    """Three-channel stub.

    - ``invoke`` returns an AIMessage (used by the plan-LLM call on quick_fact
      paths, never invoked on followup paths because plan short-circuits).
    - ``with_structured_output(schema).invoke`` dispatches by schema: Thesis
      returns the stub Thesis, QuickFactAnswer returns the stub QuickFact.
    - ``stream`` returns an iterable of AIMessage chunks for the QNT-211
      narrate node. Each chunk carries a ``.content`` token so the narrate
      node can assemble the narrative + fire ``narrative_chunk`` events.
    """

    def __init__(self) -> None:
        self.invoke = MagicMock(return_value=AIMessage(content="technical, fundamental, news"))
        thesis = _stub_thesis()
        quick_fact = _stub_quick_fact()

        def make_structured(schema: type) -> MagicMock:
            m = MagicMock()
            if schema is Thesis:
                m.invoke = MagicMock(return_value=thesis)
            elif schema is QuickFactAnswer:
                m.invoke = MagicMock(return_value=quick_fact)
            else:
                m.invoke = MagicMock(return_value=None)
            m.with_retry.return_value = m
            return m

        self._make_structured = make_structured

    def with_structured_output(self, schema: type) -> MagicMock:
        return self._make_structured(schema)

    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        return iter(
            [
                AIMessage(content="On balance "),
                AIMessage(content="the read here is cautious."),
            ]
        )


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch) -> _StubLLM:
    stub = _StubLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    # Bias the intent classifier too: heuristic resolves obvious thesis/
    # quick_fact/followup asks; only the LLM fallback path hits this stub.
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)
    return stub


@pytest.fixture
def saver() -> Any:
    """In-memory SqliteSaver for the followup persistence loop."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    return SqliteSaver(conn)


def _tool_calls(tools: dict[str, MagicMock]) -> int:
    return sum(t.call_count for t in tools.values())


def test_heuristic_followup_requires_prior_turn() -> None:
    """Bare 'why?' with no prior turn ⇒ defer to LLM (return None).
    Same question with prior turn ⇒ 'followup'."""
    assert _heuristic_intent("why?") is None
    assert _heuristic_intent("why?", has_prior_turn=True) == "followup"
    assert _heuristic_intent("tell me more", has_prior_turn=True) == "followup"
    assert _heuristic_intent("elaborate", has_prior_turn=True) == "followup"


def test_heuristic_followup_blocked_by_ticker_mention() -> None:
    """A short question that names a ticker is not a followup — the user
    anchored on a new symbol, even if a pronoun token is present."""
    # "why NVDA?" mentions a ticker and a pronoun; the followup heuristic
    # MUST defer rather than swallow it.
    result = _heuristic_intent("why NVDA?", has_prior_turn=True)
    assert result != "followup"


def test_followup_reuses_reports_and_skips_gather(
    stub_llm: _StubLLM,  # noqa: ARG001
    saver: Any,
) -> None:
    """Same thread_id, turn 1 = thesis, turn 2 = pronoun-style metric ask →
    second turn returns followup intent + zero tool calls + QuickFactAnswer
    (QNT-211: the metric-ask gate keeps the structured card on this path).
    """
    tools = {
        "technical": MagicMock(return_value="## technical\nRSI 78\n"),
        "fundamental": MagicMock(return_value="## fundamental\nP/E 80\n"),
        "company": MagicMock(return_value="## company\nDescription\n"),
        "news": MagicMock(return_value="## news\n- headline\n"),
    }
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "test:TSLA"}}

    # Turn 1: full thesis run hydrates state for turn 2.
    first = graph.invoke({"ticker": "TSLA", "question": "is TSLA overvalued?"}, config=config)
    assert first["intent"] == "thesis"
    assert first["reports"]  # tools ran, reports landed in state
    calls_after_turn_1 = _tool_calls(tools)
    assert calls_after_turn_1 > 0  # sanity: real tools fired

    # Reset call counts so the followup turn's assertion is unambiguous.
    for t in tools.values():
        t.reset_mock()

    # Turn 2: pronoun-shaped followup that DOES name a metric — the
    # QNT-211 metric-ask gate keeps this on the QuickFactAnswer path so
    # the card still lands. "elaborate on the RSI" satisfies all three:
    # has_prior_turn (state hydrated), short, no ticker named, followup
    # token ("elaborate"), AND a quick-fact token ("rsi").
    second = graph.invoke({"ticker": "TSLA", "question": "elaborate on the RSI"}, config=config)
    assert second["intent"] == "followup"
    # AC4: zero tool calls on the followup turn.
    assert _tool_calls(tools) == 0
    # Metric-ask path: QuickFactAnswer is populated.
    assert isinstance(second.get("answer"), QuickFactAnswer)


def test_old_shape_checkpoint_hydrates_and_followup_works(
    stub_llm: _StubLLM,  # noqa: ARG001
    saver: Any,
) -> None:
    """QNT-307 (AC2): a checkpoint written in the PRE-refactor state shape --
    the legacy ``thesis`` / ``reports`` / ``messages`` slots populated, and NO
    ``answer`` field -- hydrates into the new state without error, and a
    warm-thread followup on that thread still works (degrades gracefully).

    The concern (QNT-209 / QNT-216 backwards-compat): existing SqliteSaver
    threads persist the old shape. QNT-307 retired the seven legacy answer slots,
    so the ``thesis`` channel no longer exists -- LangGraph silently IGNORES the
    stored value for a channel the current graph doesn't declare (proven below:
    it is absent from the hydrated state, no raise). ``answer`` is likewise absent
    (reads as None) and ``prior_answer`` is unset, so the followup has no prior
    card to reason over -- but ``reports`` still hydrate, so turn 2 reuses them
    with zero tool calls and produces a QuickFactAnswer. We seed such a checkpoint
    directly (never writing ``answer``) and confirm turn 2 works.
    """
    tools = {
        "technical": MagicMock(return_value="## technical\nRSI 78\n"),
        "fundamental": MagicMock(return_value="## fundamental\nP/E 80\n"),
        "company": MagicMock(return_value="## company\nDescription\n"),
    }
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "legacy:TSLA"}}

    # Seed a PRE-QNT-294 checkpoint: legacy answer slot (``thesis``) + reports +
    # transcript, but deliberately NO ``answer`` key -- exactly what a thread
    # persisted before the discriminated-union migration carries.
    old_shape_state: dict[str, Any] = {
        "ticker": "TSLA",
        "analysis_ticker": "TSLA",
        "question": "is TSLA overvalued?",
        "intent": "thesis",
        "reports": {
            "technical": "## technical\nRSI 78\n",
            "fundamental": "## fundamental\nP/E 80\n",
            "company": "## company\nDescription\n",
        },
        "thesis": _stub_thesis(),
        "confidence": 0.9,
        "messages": [
            {"role": "user", "content": "is TSLA overvalued?"},
            {
                "role": "assistant",
                "content": "TSLA looks rich.\nStructured payload: thesis verdict=Underweight",
            },
        ],
    }
    graph.update_state(config, old_shape_state)

    # Sanity: the seeded checkpoint has no ``answer`` channel, and the retired
    # ``thesis`` channel is ignored on load (QNT-307) -- not carried into state.
    hydrated = graph.get_state(config).values
    assert "answer" not in hydrated
    assert "thesis" not in hydrated
    assert hydrated["reports"]  # the surviving prior-turn context

    for t in tools.values():
        t.reset_mock()

    # Turn 2: a metric-shaped followup on the OLD-shape thread. It must hydrate
    # the old checkpoint (no crash), skip gather (reuse hydrated reports), and
    # produce a QuickFactAnswer reasoned over the hydrated prior thesis.
    second = graph.invoke({"ticker": "TSLA", "question": "elaborate on the RSI"}, config=config)
    assert second["intent"] == "followup"
    assert _tool_calls(tools) == 0
    # The card is produced (works): the new-shape ``answer`` union is now written
    # on the once-legacy thread, reasoned from the hydrated reports.
    assert isinstance(second.get("answer"), QuickFactAnswer)


def test_fresh_thread_with_pronoun_routes_to_clarify(
    stub_llm: _StubLLM,  # noqa: ARG001
    saver: Any,
) -> None:
    """QNT-212 update: fresh thread_id, first message is 'why?' → no prior
    turn AND no ticker → the new ambiguity detector fires the clarify path
    instead of falling through to a fabricated thesis. gather does NOT run.

    Pre-QNT-212 behaviour was thesis-default with gather firing; that path
    silently produced a thesis built around whatever placeholder ticker
    the request carried and is exactly the failure mode QNT-212 closes.
    """
    tools = {
        "technical": MagicMock(return_value="## technical\nRSI 50\n"),
        "fundamental": MagicMock(return_value="## fundamental\nP/E 20\n"),
        "company": MagicMock(return_value="## company\nDescription\n"),
        "news": MagicMock(return_value="## news\n- headline\n"),
    }
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "fresh:NVDA"}}

    result = graph.invoke({"ticker": "NVDA", "question": "why?"}, config=config)
    # The heuristic sees "why?" as a followup token, but has_prior_turn
    # is False, so it defers. The LLM classifier then biases to thesis
    # (safe default). With no ticker in the question AND no prior turn,
    # the ambiguity detector fires ``needs_ticker`` and routes to clarify.
    assert result.get("ambiguity_kind") == "needs_ticker"
    assert _tool_calls(tools) == 0  # gather did NOT run -- clarify short-circuited


def test_followup_thread_then_named_metric_routes_quick_fact(
    stub_llm: _StubLLM,
    saver: Any,
) -> None:
    """Same thread with prior turn + a fully-anchored question (ticker +
    metric) routes via the quick_fact heuristic, NOT followup."""
    tools = {
        "technical": MagicMock(return_value="## technical\nRSI 50\n"),
        "fundamental": MagicMock(return_value="## fundamental\nP/E 20\n"),
        "company": MagicMock(return_value="## company\nDescription\n"),
    }
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "anchor:TSLA"}}

    # Turn 1: build prior turn so reports hydrate.
    graph.invoke({"ticker": "TSLA", "question": "thesis on TSLA?"}, config=config)

    # Turn 2: fully-anchored short question with a quick_fact token (rsi)
    # AND a named ticker. The followup heuristic must defer (a named
    # ticker means the user is anchoring a new question, not gesturing
    # at the prior answer). The quick_fact heuristic fires instead.
    # Note: `stub_llm` here is unused — the heuristic resolves it without
    # an LLM call, so the structured-output dispatch matters only because
    # quick_fact synthesis runs against the QuickFactAnswer stub.
    _ = stub_llm  # keep fixture referenced for clarity
    result = graph.invoke({"ticker": "TSLA", "question": "what's NVDA's RSI?"}, config=config)
    assert result["intent"] == "quick_fact"


# ─── QNT-245: ticker-agnostic thread (cross-ticker navigation) ─────────────


def test_explicit_ask_rebases_over_url_and_stored_ticker() -> None:
    """AC4: a new explicit ask naming a ticker after navigation rebases to the
    NAMED ticker, even though both the input URL ticker and the thread's stored
    analysis_ticker differ from it.

    With a single ticker-agnostic thread (QNT-245), the URL ticker, the stored
    analysis_ticker, and the question-named ticker can all disagree on one turn.
    The question-named ticker must win for single-name intents — this is the
    QNT-228 message-wins rule, re-pinned here for the cross-ticker thread.
    """
    # URL=AMZN (just navigated), stored analysis_ticker=MSFT (earlier turn),
    # question explicitly names NVDA. All three distinct; NVDA must win.
    resolved = _resolve_single_ticker_context(
        current_ticker="AMZN",
        question="how is NVDA trending technically?",
        intent="technical",
        prior_ticker="MSFT",
    )
    assert resolved == "NVDA"


def test_fresh_nonfollowup_uses_url_not_stored_ticker() -> None:
    """A fresh (non-followup) ask that names NO ticker falls back to the URL
    ticker, NOT the stale stored analysis_ticker.

    Complements AC4: after navigating to /ticker/AMZN, a generic "give me a
    thesis" must analyse AMZN (the page), not whatever the thread last discussed.
    Only a bare *followup* inherits the stored subject.
    """
    resolved = _resolve_single_ticker_context(
        current_ticker="AMZN",
        question="give me a thesis",
        intent="thesis",
        prior_ticker="NVDA",
    )
    assert resolved == "AMZN"


def test_bare_followup_inherits_stored_ticker_over_url() -> None:
    """A bare followup inherits the stored analysis_ticker even when the URL
    ticker differs — the unit-level mechanism behind AC5's checkpoint test."""
    resolved = _resolve_single_ticker_context(
        current_ticker="NVDA",
        question="why?",
        intent="followup",
        prior_ticker="AMZN",
    )
    assert resolved == "AMZN"


def test_single_thread_spans_two_tickers_then_followup_inherits(
    stub_llm: _StubLLM,  # noqa: ARG001 — patches get_llm via fixture side-effect
    saver: Any,
) -> None:
    """AC5 / AC3: one ticker-agnostic thread, sequential turns on two tickers,
    bare followup inherits the most recent subject WITHOUT losing the checkpoint.

    Mirrors the QNT-245 cross-ticker navigation flow on a single thread_id:
      turn 1  /ticker/NVDA  "is NVDA overvalued?"   -> thesis,    analysis=NVDA
      turn 2  /ticker/NVDA  "what's AMZN's RSI?"     -> quick_fact, analysis=AMZN
      turn 3  /ticker/NVDA  "why?"                   -> followup,  inherits AMZN

    Turn 3 is asked with URL ticker NVDA on purpose: the followup must inherit
    AMZN from the checkpoint, not snap back to the page ticker, and must reuse
    the hydrated reports (zero new tool calls).
    """
    tools = {
        "technical": MagicMock(return_value="## technical\nRSI 50\n"),
        "fundamental": MagicMock(return_value="## fundamental\nP/E 20\n"),
        "company": MagicMock(return_value="## company\nDescription\n"),
        "news": MagicMock(return_value="## news\n- headline\n"),
    }
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "conversation:xyz"}}

    # Turn 1: thesis on the page ticker (NVDA).
    t1 = graph.invoke({"ticker": "NVDA", "question": "is NVDA overvalued?"}, config=config)
    assert t1["intent"] == "thesis"
    assert t1["analysis_ticker"] == "NVDA"

    # Turn 2: still on the NVDA page, but explicitly ask about AMZN. The
    # question-named ticker rebases the analytical subject to AMZN.
    t2 = graph.invoke({"ticker": "NVDA", "question": "what's AMZN's RSI?"}, config=config)
    assert t2["intent"] == "quick_fact"
    assert t2["analysis_ticker"] == "AMZN"

    for t in tools.values():
        t.reset_mock()

    # Turn 3: bare followup, still URL=NVDA. Must inherit AMZN (most recent
    # subject), reuse hydrated reports (zero tool calls), keep the checkpoint.
    t3 = graph.invoke({"ticker": "NVDA", "question": "why?"}, config=config)
    assert t3["intent"] == "followup"
    assert t3["analysis_ticker"] == "AMZN"  # inherited, not snapped to NVDA
    assert _tool_calls(tools) == 0  # checkpoint reused, not re-gathered
    assert t3["reports"]  # hydrated reports survived the followup branch


# ─── QNT-290: flagged followup RAG retrieval (chained-followup dedupe) ─────


def test_chained_flagged_followups_do_not_duplicate_retrieved_block(
    stub_llm: _StubLLM,
    saver: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: two consecutive flagged followups on the same thread fold onto the
    checkpointer-hydrated reports dict, not a fresh fetch. The second fold must
    REPLACE the first turn's retrieved block, not stack a second one on top of
    it -- otherwise a long followup chain would grow the persisted news report
    without bound.
    """
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("followup", "llm", True, False, "NVDA buyback"),
    )
    search = MagicMock(
        side_effect=[
            json.dumps(
                [{"headline": "First hit headline", "source": "Reuters", "date": "2026-06-01"}]
            ),
            json.dumps(
                [{"headline": "Second hit headline", "source": "Bloomberg", "date": "2026-06-02"}]
            ),
        ]
    )
    tools = {
        "technical": MagicMock(return_value="## technical\nRSI 50\n"),
        "fundamental": MagicMock(return_value="## fundamental\nP/E 20\n"),
        "company": MagicMock(return_value="## company\nDescription\n"),
        "news": MagicMock(return_value="## news\nprior digest\n"),
    }
    graph = build_graph(tools, checkpointer=saver, search_news_tool=search)
    config: RunnableConfig = {"configurable": {"thread_id": "chain:NVDA"}}

    first = graph.invoke(
        {
            "ticker": "NVDA",
            "question": "and the buyback?",
            "reports": {"news": "## news\nprior digest\n"},
        },
        config=config,
    )
    assert first["reports"]["news"].count("First hit headline") == 1

    second = graph.invoke(
        {"ticker": "NVDA", "question": "anything new on the buyback?"}, config=config
    )
    assert second["reports"]["news"].count("Second hit headline") == 1
    assert "First hit headline" not in second["reports"]["news"]
    # Exactly one retrieved-block heading survives the chained fold -- never two.
    assert second["reports"]["news"].count(f"## {RETRIEVED_NEWS_HEADING}") == 1
    # Neither turn re-ran the report plan.
    assert _tool_calls(tools) == 0


def test_thread_persists_across_saver_restart(
    stub_llm: _StubLLM,  # noqa: ARG001 — patches get_llm via fixture side-effect
    tmp_path: Any,
) -> None:
    """AC2 in-process equivalent: a brand-new SqliteSaver pointed at the
    same on-disk file sees the prior thread's hydrated reports, so a
    followup question against that thread_id reuses them with zero tool
    calls. The two-saver setup mirrors the two-process boundary that
    ``docker compose restart api`` produces in prod.
    """
    db_path = tmp_path / "agent.db"

    # ── "Process 1": full thesis run persists state to disk.
    conn1 = sqlite3.connect(str(db_path), check_same_thread=False)
    saver1 = SqliteSaver(conn1)
    tools1 = {
        "technical": MagicMock(return_value="## technical\nRSI 78\n"),
        "fundamental": MagicMock(return_value="## fundamental\nP/E 80\n"),
        "company": MagicMock(return_value="## company\nDescription\n"),
        "news": MagicMock(return_value="## news\n- headline\n"),
    }
    graph1 = build_graph(tools1, checkpointer=saver1)
    config: RunnableConfig = {"configurable": {"thread_id": "persist:TSLA"}}
    r1 = graph1.invoke({"ticker": "TSLA", "question": "is TSLA overvalued?"}, config=config)
    assert r1["intent"] == "thesis"
    conn1.close()

    # ── "Process 2": brand-new connection + saver + graph against the
    # same on-disk file. No shared memory; only the SQLite file links them.
    conn2 = sqlite3.connect(str(db_path), check_same_thread=False)
    saver2 = SqliteSaver(conn2)
    tools2 = {
        "technical": MagicMock(return_value="## technical\nRSI 78\n"),
        "fundamental": MagicMock(return_value="## fundamental\nP/E 80\n"),
        "company": MagicMock(return_value="## company\nDescription\n"),
        "news": MagicMock(return_value="## news\n- headline\n"),
    }
    graph2 = build_graph(tools2, checkpointer=saver2)
    r2 = graph2.invoke({"ticker": "TSLA", "question": "why?"}, config=config)
    assert r2["intent"] == "followup"
    # The cross-process-boundary assertion: no tool re-fetched.
    assert _tool_calls(tools2) == 0
    # The state-preservation assertion (QNT-307): the prior turn's Thesis survived
    # the restart AND was snapshotted into ``prior_answer`` by classify, so the
    # narrative-only followup (answer=None this turn) can still reference the v2
    # framing the prompt expects. ``prior_answer`` replaces the old ``thesis``
    # slot that used to carry this across turns.
    assert isinstance(r2.get("prior_answer"), Thesis)
    assert r2["reports"]  # hydrated reports still present
    conn2.close()


# ─── QNT-307: prior_answer reproduces the retired thesis-slot lifetime ──────


def test_prior_answer_carries_original_thesis_across_followup_chain(
    stub_llm: _StubLLM,  # noqa: ARG001
    saver: Any,
) -> None:
    """The old ``thesis`` slot survived a chain of followups because followup
    returns never cleared it. ``prior_answer`` reproduces that: a thesis followed
    by two narrative-only followups keeps the ORIGINAL Thesis as the prior
    substrate on every turn."""
    tools = {
        "technical": MagicMock(return_value="## technical\nRSI 78\n"),
        "fundamental": MagicMock(return_value="## fundamental\nP/E 80\n"),
        "company": MagicMock(return_value="## company\nDescription\n"),
    }
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "prior-chain:TSLA"}}

    graph.invoke({"ticker": "TSLA", "question": "is TSLA overvalued?"}, config=config)
    # Turn 2: narrative-only followup -> this turn's answer is None, and classify
    # snapshotted the prior Thesis into prior_answer.
    t2 = graph.invoke({"ticker": "TSLA", "question": "tell me more"}, config=config)
    assert t2["intent"] == "followup"
    assert t2.get("answer") is None
    assert isinstance(t2.get("prior_answer"), Thesis)
    # Turn 3: another narrative-only followup -> the ORIGINAL Thesis is still
    # carried (turn 2 wrote answer=None, so prior_answer is preserved).
    t3 = graph.invoke({"ticker": "TSLA", "question": "why is that?"}, config=config)
    assert t3["intent"] == "followup"
    assert isinstance(t3.get("prior_answer"), Thesis)


def test_prior_answer_is_none_after_non_thesis_turn(
    stub_llm: _StubLLM,  # noqa: ARG001
    saver: Any,
) -> None:
    """``prior_answer`` carries ONLY a Thesis -- every non-thesis intent went
    through project_answer, which nulled the old ``thesis`` slot. A followup after
    a quick_fact turn therefore gets prior_answer=None (NOT the quick_fact), so
    build_followup_prompt's 'earlier thesis' section stays empty, matching the
    pre-refactor behaviour (out-of-scope synthesis change otherwise)."""
    tools = {
        "technical": MagicMock(return_value="## technical\nRSI 78\n"),
        "fundamental": MagicMock(return_value="## fundamental\nP/E 80\n"),
        "company": MagicMock(return_value="## company\nDescription\n"),
    }
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "prior-nonthesis:TSLA"}}

    graph.invoke({"ticker": "TSLA", "question": "is TSLA overvalued?"}, config=config)
    # Turn 2: named-ticker metric ask routes quick_fact (not followup), nulling the
    # thesis-equivalent -- prior_answer must not survive it.
    t2 = graph.invoke({"ticker": "TSLA", "question": "what's TSLA's RSI?"}, config=config)
    assert t2["intent"] == "quick_fact"
    # Turn 3: followup after the quick_fact turn -> no prior Thesis to carry.
    t3 = graph.invoke({"ticker": "TSLA", "question": "tell me more"}, config=config)
    assert t3["intent"] == "followup"
    assert t3.get("prior_answer") is None
