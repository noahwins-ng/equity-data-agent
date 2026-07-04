"""Tests for the QNT-212 clarify node + ambiguity detection.

Covers the AC1 / AC2 / AC3 / AC4 scenarios:

1. Ambiguity detection sets the right ``ambiguity_kind`` for each trigger.
2. A clarify run routes classify → clarify → narrate, skipping plan + gather
   entirely (zero tool calls).
3. The clarify response is a ConversationalAnswer whose ``answer`` reads
   like a question; narrate runs AND emits chunks (an engaging lead-in
   bubble above the clarify card) on every clarify turn, regardless of the
   classifier's intent label (QNT-220 follow-up).
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

    def with_structured_output(self, schema: type, **_kwargs: object) -> MagicMock:
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


def test_detect_ambiguity_comparison_one_ticker_uses_context_ticker() -> None:
    """QNT-233 option (a): URL context can supply the other comparison side.

    This intentionally reverses the old QNT-212 pin. A user on /ticker/NVDA
    typing "compare to AAPL" should get NVDA-vs-AAPL, not a clarify card.
    """
    assert (
        _detect_ambiguity(
            "comparison",
            "compare to AAPL",
            has_prior_turn=False,
            has_context_ticker=True,
            context_ticker="NVDA",
        )
        is None
    )


def test_detect_ambiguity_comparison_one_ticker_without_context_clarifies() -> None:
    """One named ticker still clarifies when no URL/prior context can anchor it."""
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


@pytest.mark.parametrize(
    "question",
    ["what's the thesis of micron", "is intel a buy?", "thoughts on Tesla"],
)
def test_detect_ambiguity_company_name_only_passes(question: str) -> None:
    """QNT-257: a name-only ask (no symbol, no URL ticker, no prior turn) now
    resolves via extract_tickers and proceeds instead of bouncing to clarify."""
    assert _detect_ambiguity("thesis", question, has_prior_turn=False) is None


@pytest.mark.parametrize("question", ["What do you think?", "Your thoughts?", "what's your take"])
def test_detect_ambiguity_view_gesture_conversational(question: str) -> None:
    """QNT-214 follow-up: the LLM mislabels a bare 'what do you think?' as
    conversational; the gesture branch still routes it to needs_ticker so the
    agent asks back instead of ploughing into a generic redirect.
    """
    assert _detect_ambiguity("conversational", question, has_prior_turn=False) == "needs_ticker"


@pytest.mark.parametrize(
    "question",
    ["what's interesting?", "What stands out?", "anything interesting?", "what should I watch?"],
)
def test_detect_ambiguity_exploration_gesture_no_ticker(question: str) -> None:
    """QNT-220 follow-up: the exact /ticker/NVDA case. A tickerless exploration
    ask ('what's interesting?') the LLM labels conversational must clarify the
    ticker rather than fall through to the generic capability card."""
    assert _detect_ambiguity("conversational", question, has_prior_turn=False) == "needs_ticker"


def test_detect_ambiguity_exploration_gesture_with_ticker_passes() -> None:
    """A named ticker anchors the exploration ask -- it routes to the
    exploration card, not clarify."""
    assert (
        _detect_ambiguity("thesis", "what's interesting about NVDA?", has_prior_turn=False) is None
    )


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

    conv = result.get("answer")
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


def test_clarify_narrates_even_when_classified_conversational(
    stub_llm: _StubLLM, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ARG001
    """QNT-220 follow-up: a clarify turn the classifier labels 'conversational'
    must STILL emit the narrate bubble. Previously the leading
    ``intent == 'conversational'`` gate skipped narrate, so the bubble appeared
    or vanished depending on the classifier's label for the same ambiguous
    question."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda *a, **k: ("conversational", "llm", False, False, ""),
    )
    events: list[tuple[str, dict[str, object]]] = []
    graph = build_graph(_default_tools(), event_emitter=lambda e, d: events.append((e, dict(d))))
    result = graph.invoke({"ticker": "NVDA", "question": "what do you think?"})

    assert result.get("ambiguity_kind") == "needs_ticker"
    assert result["intent_path"] == ["classify", "clarify", "narrate"]
    assert any(name == "narrative_chunk" for name, _ in events), (
        "clarify must narrate even when the classifier labels it conversational"
    )
    assert result.get("narrative")


def test_comparison_one_ticker_with_url_context_routes_to_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-233 option (a): /ticker/AAPL + "compare with NVDA" runs as comparison."""
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
    result = graph.invoke({"ticker": "AAPL", "question": "compare with NVDA please"})

    assert result["ambiguity_kind"] is None
    assert result["intent_path"] == ["classify", "plan", "gather", "synthesize", "narrate"]
    assert tools["company"].call_count == 2


def test_comparison_phrase_one_ticker_uses_url_context_without_llm_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for dev AC: /ticker/NVDA + "compare with AAPL" compares directly."""
    stub = _StubLLM()
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("conversational", "heuristic", False, False, ""),
    )
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)

    tools = _default_tools()
    result = build_graph(tools).invoke({"ticker": "NVDA", "question": "compare with AAPL"})

    assert result["intent"] == "comparison"
    assert result["ambiguity_kind"] is None
    assert result["comparison_tickers"] == ["AAPL", "NVDA"]
    assert result["intent_path"] == ["classify", "plan", "gather", "synthesize", "narrate"]


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


# ─── QNT-258 follow-up: conversational schema forces function_calling ───────


def test_clarify_uses_function_calling_method(monkeypatch: pytest.MonkeyPatch) -> None:
    """QNT-258 follow-up: the clarify ConversationalAnswer call must pass
    ``method="function_calling"`` so the paid DeepSeek primary cannot return the
    clarify question as bare prose (json_invalid on the default json_schema path
    -- the observed Sentry EQUITY-DATA-AGENT-8). Records the method kwarg the
    node hands to ``with_structured_output`` for the ConversationalAnswer schema.
    """
    seen: dict[type, object] = {}

    class _RecordingLLM:
        invoke = MagicMock(return_value=AIMessage(content="conversational"))

        def with_structured_output(self, schema: type, **kwargs: object) -> MagicMock:
            seen[schema] = kwargs.get("method")
            m = MagicMock()
            m.invoke = MagicMock(return_value=_stub_clarify_answer())
            m.with_retry.return_value = m
            return m

        def stream(self, *_a: Any, **_k: Any) -> Any:
            return iter([AIMessage(content="Hi ")])

    stub = _RecordingLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)

    build_graph(_default_tools()).invoke({"ticker": "NVDA", "question": "what do you think?"})

    assert seen.get(ConversationalAnswer) == "function_calling", (
        "clarify must request function_calling for the ConversationalAnswer schema"
    )


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

    conv = result.get("answer")
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
    assert not isinstance(first.get("answer"), Thesis)
    pre_tools = sum(t.call_count for t in tools.values())
    assert pre_tools == 0

    # Turn 2: user names TSLA -- thesis fires normally.
    second = graph.invoke({"ticker": "TSLA", "question": "thesis on TSLA?"}, config=config)
    assert second["intent"] == "thesis"
    assert isinstance(second.get("answer"), Thesis)
    # Tools fired on the second turn only.
    assert sum(t.call_count for t in tools.values()) > 0


def test_clarify_bubble_is_deterministic_not_llm(
    stub_llm: _StubLLM,
) -> None:  # noqa: ARG001
    """QNT-220 follow-up: the clarify bubble must be a DETERMINISTIC lead-in,
    never an LLM narration. A clarify turn gathered zero reports, so an LLM
    bubble fabricates a stance (observed in prod: "the read is constructive for
    NVDA" with no data). Assert the narrative is the exact deterministic phrase
    keyed to the ambiguity kind -- never the stub LLM's stream output."""
    from agent.graph import _CLARIFY_LEAD_IN

    events: list[tuple[str, dict[str, object]]] = []
    graph = build_graph(_default_tools(), event_emitter=lambda e, d: events.append((e, dict(d))))
    result = graph.invoke({"ticker": "NVDA", "question": "what do you think?"})

    assert result["ambiguity_kind"] == "needs_ticker"
    # The exact deterministic phrase -- NOT the stub LLM's stream chunks.
    assert result["narrative"] == _CLARIFY_LEAD_IN["needs_ticker"]
    assert "Hi there" not in (result.get("narrative") or "")
    # And it streamed as the bubble.
    assert any(
        name == "narrative_chunk" and data.get("delta") == _CLARIFY_LEAD_IN["needs_ticker"]
        for name, data in events
    )


def test_clarify_prompt_offers_url_context_ticker() -> None:
    """QNT-220 follow-up: a needs_ticker clarify must reference the page's
    URL-context ticker (the user is looking at it) rather than asking a generic
    'which name?'. Stays within QNT-212 -- it OFFERS the ticker, it does not
    silently answer on it."""
    from agent.prompts.system import build_clarify_prompt

    msgs = build_clarify_prompt(
        ambiguity_kind="needs_ticker",
        question="what's interesting?",
        ticker="NVDA",
        tickers=("NVDA", "AAPL", "MSFT"),
    )
    rendered = "\n".join(str(getattr(m, "content", m)) for m in msgs)
    assert "URL-context ticker: NVDA" in rendered  # the symbol is in the prompt
    assert "offer it as the likely subject" in rendered  # instructed to use it
    assert "do NOT silently answer" in rendered  # but not auto-confirm (QNT-212)


@pytest.mark.parametrize(
    "intent",
    ["thesis", "comparison", "followup", "news"],
)
def test_narrate_prompt_forward_intents_offer_optional_watch(intent: str) -> None:
    """QNT-285: forward-looking narrations carry the optional catalyst-gated
    Watch close (replacing the old always-on probe close)."""
    from agent.prompts.system import build_narrate_prompt

    msgs = build_narrate_prompt(
        intent=intent,
        ticker="NVDA",
        question="thesis on NVDA",
        payload_markdown="Setup ... Verdict: Overweight",
        is_clarify=False,
    )
    rendered = "\n".join(str(getattr(m, "content", m)) for m in msgs)
    assert 'begins "Watch:"' in rendered
    # The close is explicitly optional, not forced -- this is the de-bloat lever.
    assert "is optional" in rendered


@pytest.mark.parametrize("intent", ["exploration", "technical", "fundamental"])
def test_narrate_prompt_excludes_watch_close(intent: str) -> None:
    """QNT-285: exploration (broad discovery) and single-lens technical /
    fundamental (call + one driver) get no Watch close at all."""
    from agent.prompts.system import build_narrate_prompt

    msgs = build_narrate_prompt(
        intent=intent,
        ticker="NVDA",
        question="what's the read on NVDA?",
        payload_markdown="Interesting setup.",
        is_clarify=False,
    )
    rendered = "\n".join(str(getattr(m, "content", m)) for m in msgs)
    assert 'begins "Watch:"' not in rendered


def test_narrate_prompt_uses_bluf_structure() -> None:
    """QNT-285 AC1: the narrate prompt instructs a bold lead call + synthesis
    prose (not a single bloated paragraph)."""
    from agent.prompts.system import NARRATE_SYSTEM_PROMPT

    assert "BLUF" in NARRATE_SYSTEM_PROMPT
    assert "double asterisks" in NARRATE_SYSTEM_PROMPT
    assert "Synthesise, do not list" in NARRATE_SYSTEM_PROMPT
