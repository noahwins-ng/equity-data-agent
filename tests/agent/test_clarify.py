"""Tests for the QNT-212 clarify node + ambiguity detection.

Covers the AC1 / AC2 / AC3 / AC4 scenarios:

1. Ambiguity detection sets the right ``ambiguity_kind`` for each trigger.
2. A clarify run routes classify → clarify → narrate, skipping plan + gather
   entirely (zero tool calls).
3. The clarify response is a ConversationalAnswer whose ``answer`` reads
   like a question; narrate ran and emitted no chunks (the bubble
   short-circuits over a conversational payload, same gate as the
   synthesize-fallback path).
4. A clarify LLM raise falls through to ``domain_redirect`` and still
   emits a ConversationalAnswer; the done event still lands.

LLM calls are stubbed -- we're testing the graph topology + ambiguity
helper, not real generation.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import MagicMock

import pytest
from agent import graph as graph_module
from agent.conversational import ConversationalAnswer
from agent.graph import _detect_ambiguity, build_graph
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver

from ._thesis_factory import make_thesis


def _stub_clarify_answer() -> ConversationalAnswer:
    return ConversationalAnswer(
        answer="Which ticker did you have in mind?",
        suggestions=[
            "Give me a thesis on NVDA",
            "How is AAPL trending technically?",
        ],
    )


class _StubLLM:
    """Same shape as test_narrate._StubLLM plus a ConversationalAnswer arm
    so the clarify node's structured-output call resolves to a sensible
    payload. ``raises_on_conversational`` simulates an LLM crash in clarify."""

    def __init__(
        self,
        *,
        raises_on_conversational: bool = False,
        stream_chunks: list[str] | None = None,
    ) -> None:
        self.invoke = MagicMock(return_value=AIMessage(content="technical, fundamental, news"))
        self.stream_chunks = stream_chunks or ["Hi ", "there."]
        thesis = make_thesis()
        quick_fact = QuickFactAnswer(
            answer="RSI 78 (source: technical).", cited_value="78", source="technical"
        )
        clarify = _stub_clarify_answer()
        self._raise_conv = raises_on_conversational

        def make_structured(schema: type) -> MagicMock:
            m = MagicMock()
            if schema is Thesis:
                m.invoke = MagicMock(return_value=thesis)
            elif schema is QuickFactAnswer:
                m.invoke = MagicMock(return_value=quick_fact)
            elif schema is ConversationalAnswer:
                if self._raise_conv:
                    m.invoke = MagicMock(side_effect=RuntimeError("simulated clarify failure"))
                else:
                    m.invoke = MagicMock(return_value=clarify)
            else:
                m.invoke = MagicMock(return_value=None)
            m.with_retry.return_value = m
            return m

        self._make_structured = make_structured

    def with_structured_output(self, schema: type) -> MagicMock:
        return self._make_structured(schema)

    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        return iter(AIMessage(content=c) for c in self.stream_chunks)


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch) -> _StubLLM:
    stub = _StubLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)
    return stub


def _default_tools() -> dict[str, MagicMock]:
    return {
        "technical": MagicMock(return_value="## technical\nRSI 78\n"),
        "fundamental": MagicMock(return_value="## fundamental\nP/E 80\n"),
        "company": MagicMock(return_value="## company\nDescription\n"),
        "news": MagicMock(return_value="## news\n- headline\n"),
    }


# ─── AC1: ambiguity detection ───────────────────────────────────────────────


def test_detect_ambiguity_comparison_one_ticker() -> None:
    """AC1: comparison intent with one ticker → needs_second_ticker."""
    assert (
        _detect_ambiguity("comparison", "compare to NVDA", has_prior_turn=False)
        == "needs_second_ticker"
    )


def test_detect_ambiguity_comparison_zero_tickers() -> None:
    """AC1: comparison intent with no ticker named also needs a second."""
    assert (
        _detect_ambiguity("comparison", "compare them", has_prior_turn=False)
        == "needs_second_ticker"
    )


def test_detect_ambiguity_comparison_two_tickers_passes() -> None:
    """Two tickers named → no ambiguity, comparison can run as-is."""
    assert _detect_ambiguity("comparison", "compare NVDA vs AAPL", has_prior_turn=False) is None


def test_detect_ambiguity_comparison_does_not_count_state_ticker() -> None:
    """Pinning test: the URL-context ticker (state.ticker, passed via the
    API request) is intentionally NOT counted toward the comparison ticker
    requirement. A user on /ticker/NVDA who types "compare to AAPL" still
    gets routed to clarify -- the design wants an explicit second-ticker
    naming in the QUESTION, not URL-derived. Pinning this invariant so a
    future "fix" doesn't silently swallow the edge case.

    ``_detect_ambiguity`` only takes the question string -- state.ticker
    never enters its signature -- so this test guards the contract by
    construction.
    """
    # Only AAPL named in the question. State.ticker (would be NVDA in a
    # real run) is not exposed to the helper.
    assert (
        _detect_ambiguity("comparison", "compare to AAPL", has_prior_turn=False)
        == "needs_second_ticker"
    )


def test_detect_ambiguity_thesis_no_ticker() -> None:
    """AC1: thesis with no ticker AND no prior turn → needs_ticker."""
    assert _detect_ambiguity("thesis", "what do you think?", has_prior_turn=False) == "needs_ticker"


@pytest.mark.parametrize("intent", ["fundamental", "technical", "news", "quick_fact"])
def test_detect_ambiguity_focused_no_ticker(intent: str) -> None:
    """AC1: focused / quick_fact intents also clarify when no ticker is named."""
    assert _detect_ambiguity(intent, "what's the read?", has_prior_turn=False) == "needs_ticker"  # type: ignore[arg-type]


@pytest.mark.parametrize("question", ["What do you think?", "Your thoughts?", "what's your take"])
def test_detect_ambiguity_view_gesture_conversational(question: str) -> None:
    """QNT-214 follow-up: the LLM mislabels a bare 'what do you think?' as
    conversational; the gesture branch still routes it to needs_ticker so the
    agent asks back instead of ploughing into a generic redirect.
    """
    assert _detect_ambiguity("conversational", question, has_prior_turn=False) == "needs_ticker"


def test_detect_ambiguity_compare_gesture_conversational() -> None:
    """QNT-214 follow-up: a bare 'compare them' mislabelled conversational
    routes to needs_second_ticker rather than fabricating a peer."""
    assert (
        _detect_ambiguity("conversational", "compare them", has_prior_turn=False)
        == "needs_second_ticker"
    )


def test_detect_ambiguity_view_gesture_with_ticker_passes() -> None:
    """A named ticker anchors the view gesture -- answer it, don't ask back."""
    assert (
        _detect_ambiguity("thesis", "what do you think about NVDA?", has_prior_turn=False) is None
    )


def test_detect_ambiguity_view_gesture_with_prior_turn_passes() -> None:
    """On a warm thread a bare 'what do you think?' is a followup, not a
    cold-start clarify -- the has_prior_turn guard keeps it off the gesture path."""
    assert _detect_ambiguity("conversational", "what do you think?", has_prior_turn=True) is None


@pytest.mark.parametrize("greeting", ["hi", "hello", "what can you do?", "what do you do?"])
def test_detect_ambiguity_greetings_stay_conversational(greeting: str) -> None:
    """Greetings / capability asks must NOT match the gesture tokens -- they
    stay on the conversational path and get the capability card."""
    assert _detect_ambiguity("conversational", greeting, has_prior_turn=False) is None


def test_detect_ambiguity_thesis_no_ticker_with_prior_turn_passes() -> None:
    """A hydrated thread anchors the ambiguous ask -- no clarify fires."""
    assert _detect_ambiguity("thesis", "should I buy?", has_prior_turn=True) is None


def test_detect_ambiguity_thesis_with_ticker_passes() -> None:
    """A named ticker is enough anchor regardless of prior turn."""
    assert _detect_ambiguity("thesis", "thesis on NVDA?", has_prior_turn=False) is None


def test_detect_ambiguity_followup_no_prior_turn() -> None:
    """AC1: followup intent on a cold thread → needs_prior_turn (defensive
    against the LLM classifier returning followup without a hydrated thread).
    """
    assert _detect_ambiguity("followup", "why?", has_prior_turn=False) == "needs_prior_turn"


def test_detect_ambiguity_followup_with_prior_turn_passes() -> None:
    """Followup on a hydrated thread is the normal path -- no clarify."""
    assert _detect_ambiguity("followup", "why?", has_prior_turn=True) is None


def test_detect_ambiguity_conversational_never_fires() -> None:
    """Conversational intent is always free to run (no tools required)."""
    assert _detect_ambiguity("conversational", "hi", has_prior_turn=False) is None


# ─── AC2 / AC3: clarify routing + response ──────────────────────────────────


def test_clarify_skips_plan_and_gather(stub_llm: _StubLLM) -> None:  # noqa: ARG001
    """AC2: ambiguous question (thesis intent + no ticker) routes via clarify;
    plan and gather mocks see zero invocations."""
    tools = _default_tools()
    graph = build_graph(tools)
    result = graph.invoke({"ticker": "NVDA", "question": "what do you think?"})

    # Zero tool calls -- gather never ran.
    assert sum(t.call_count for t in tools.values()) == 0
    # Path skipped plan + gather entirely.
    assert result["intent_path"] == ["classify", "clarify", "narrate"]
    # Ambiguity_kind landed in state.
    assert result["ambiguity_kind"] == "needs_ticker"


def test_clarify_responds_with_question_shaped_conversational(
    stub_llm: _StubLLM,
) -> None:  # noqa: ARG001
    """AC3: clarify produces a ConversationalAnswer whose answer reads as a
    clarifying question. narrate runs AND emits narrative_chunk events so
    the panel still renders the analyst-voice bubble above the question
    card (ticket: "the bubble still streams"). The narrate gate is
    differentiated from the synthesize-fallback redirect via
    ``state['ambiguity_kind']`` -- set only on the clarify route."""
    events: list[tuple[str, dict[str, object]]] = []

    def emit(event: str, data: dict[str, object]) -> None:
        events.append((event, dict(data)))

    graph = build_graph(_default_tools(), event_emitter=emit)
    result = graph.invoke({"ticker": "NVDA", "question": "what do you think?"})

    conv = result.get("conversational")
    assert isinstance(conv, ConversationalAnswer)
    assert conv.answer.endswith("?"), conv.answer
    # narrate ran AND emitted chunks -- the bubble streams above the clarify
    # card, same shape as a thesis run.
    assert "narrate" in result["intent_path"]
    assert any(name == "narrative_chunk" for name, _ in events), (
        "expected narrate to emit narrative_chunk events on the clarify path"
    )
    # And the assembled narrative landed in state.
    assert result.get("narrative")


def test_clarify_routes_comparison_one_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A comparison ask with only one named ticker routes to clarify, not
    to a half-finished comparison. The heuristic requires 2 tickers to fire
    the comparison branch, so we drive the intent via the LLM stub."""
    from agent.intent import IntentDecision

    stub = _StubLLM()

    def _intent_invoke(prompt: Any, **_kw: Any) -> IntentDecision:
        return IntentDecision(intent="comparison")

    intent_stub = MagicMock()
    intent_stub.with_structured_output = MagicMock(return_value=MagicMock(invoke=_intent_invoke))
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: intent_stub)
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)

    tools = _default_tools()
    graph = build_graph(tools)
    # "compare to NVDA" -- heuristic defers (only one ticker), LLM returns
    # comparison, ambiguity detector sees question_tickers=['NVDA'] (one) and
    # fires needs_second_ticker.
    result = graph.invoke({"ticker": "AAPL", "question": "compare with NVDA please"})

    assert result["ambiguity_kind"] == "needs_second_ticker"
    assert result["intent_path"] == ["classify", "clarify", "narrate"]
    assert sum(t.call_count for t in tools.values()) == 0


def test_clarify_routes_followup_without_prior_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM classifier returning followup on a cold thread → needs_prior_turn
    → clarify. Heuristic rules out the trivial path (no _FOLLOWUP_TOKENS
    match) so we have to coerce intent to followup via the LLM stub."""
    from agent.intent import IntentDecision

    stub = _StubLLM()

    # Heuristic on "pick up where we left off" matches none of the predefined
    # tokens, so it defers to the LLM. Make the LLM say followup.
    # NOTE: this assumes _FOLLOWUP_TOKENS in agent.intent (NOT in this diff)
    # does not contain "left off". If a future intent change adds it, the
    # heuristic would short-circuit before the LLM stub fires and quietly
    # change the code path under test.
    def _intent_invoke(prompt: Any, **_kw: Any) -> IntentDecision:
        return IntentDecision(intent="followup")

    intent_stub = MagicMock()
    intent_stub.with_structured_output = MagicMock(return_value=MagicMock(invoke=_intent_invoke))
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: intent_stub)
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)

    tools = _default_tools()
    graph = build_graph(tools)
    # Fresh ephemeral compile = no checkpointer = state.reports is empty =
    # has_prior_turn=False. LLM says followup -> needs_prior_turn.
    result = graph.invoke({"ticker": "NVDA", "question": "pick up where we left off please"})

    assert result.get("ambiguity_kind") == "needs_prior_turn"
    assert result["intent_path"] == ["classify", "clarify", "narrate"]
    assert sum(t.call_count for t in tools.values()) == 0


# ─── AC4: clarify failure falls back to domain_redirect ─────────────────────


def test_clarify_llm_failure_falls_back_to_domain_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4: clarify LLM raises → state.conversational is populated by
    domain_redirect (not None), narrate still runs, the structured event
    chain still terminates normally."""
    stub = _StubLLM(raises_on_conversational=True)
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)

    tools = _default_tools()
    graph = build_graph(tools)
    result = graph.invoke({"ticker": "NVDA", "question": "what do you think?"})

    conv = result.get("conversational")
    assert isinstance(conv, ConversationalAnswer)
    # domain_redirect always lists covered tickers in the body.
    assert "NVDA" in conv.answer
    # No tools fired -- still skipped plan + gather even on failure.
    assert sum(t.call_count for t in tools.values()) == 0
    assert result["intent_path"] == ["classify", "clarify", "narrate"]


# ─── Cross-cut: clarify + checkpointer (state persists for next turn) ───────


def test_clarify_state_persists_for_resume(stub_llm: _StubLLM) -> None:  # noqa: ARG001
    """A clarify turn followed by a real answer ("TSLA") on the same
    thread_id resumes normally -- the second turn runs a thesis. Documents
    the AC8 user flow (frontend dev-test) as an in-graph regression."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    saver = SqliteSaver(conn)
    tools = _default_tools()
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "clarify-resume:NVDA"}}

    # Turn 1: clarify -- no tools, no thesis.
    first = graph.invoke({"ticker": "NVDA", "question": "what do you think?"}, config=config)
    assert first["ambiguity_kind"] == "needs_ticker"
    assert first.get("thesis") is None
    pre_tools = sum(t.call_count for t in tools.values())
    assert pre_tools == 0

    # Turn 2: user names TSLA -- thesis fires normally.
    second = graph.invoke({"ticker": "TSLA", "question": "thesis on TSLA?"}, config=config)
    assert second["intent"] == "thesis"
    assert isinstance(second.get("thesis"), Thesis)
    # Tools fired on the second turn only.
    assert sum(t.call_count for t in tools.values()) > 0
