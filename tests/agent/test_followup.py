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

import sqlite3
from typing import Any
from unittest.mock import MagicMock

import pytest
from agent import graph as graph_module
from agent.graph import build_graph
from agent.intent import _heuristic_intent
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
    assert isinstance(second.get("quick_fact"), QuickFactAnswer)


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
    # The state-preservation assertion: the prior turn's Thesis survived
    # the restart AND survived the followup branch's return (which must
    # NOT clobber it via _empty_payload). Future followups can still
    # reference the v2 framing the prompt expects.
    assert isinstance(r2.get("thesis"), Thesis)
    assert r2["reports"]  # hydrated reports still present
    conn2.close()
