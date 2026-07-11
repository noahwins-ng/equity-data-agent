"""Tests for the QNT-211 narrate node + narrative-only followup path.

Covers the four scenarios the ticket calls out:

1. narrate assembles narrative text + fires narrative_chunk events in order
   when given a populated Thesis.
2. narrate failure (LLM .stream() raises) does NOT raise -- the graph
   terminates with narrative=None and the structured payload still landed
   in state.
3. Followup narrative-only path: synthesize's followup branch leaves
   quick_fact=None when the question carries no quick-fact token; narrate
   then produces the only spoken response.
4. Followup metric-ask path: same setup + a question that names a metric
   ("elaborate on the RSI") keeps the QuickFactAnswer alive AND runs
   narrate on top.

The graph runs against an in-memory SqliteSaver so the followup scenarios
exercise the real hydration path. LLM calls are stubbed -- we're testing
the graph topology + event_emitter wiring, not real generation.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import MagicMock

import pytest
from agent import graph as graph_module
from agent.graph import build_graph
from agent.nodes.deps import GraphDeps
from agent.nodes.narrate import narrate_node
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver

from ._thesis_factory import make_thesis


def _stub_quick_fact() -> QuickFactAnswer:
    return QuickFactAnswer(
        answer="RSI 78 overbought (source: technical).",
        cited_value="78",
        source="technical",
    )


class _StubLLM:
    """Mirror of test_followup._StubLLM with a deterministic .stream() that
    yields N AIMessage chunks. The chunk list is read off ``stream_chunks``
    so a single test can swap it (e.g. inject a stream that raises)."""

    def __init__(self, stream_chunks: list[str] | None = None, stream_raises: bool = False) -> None:
        self.invoke = MagicMock(return_value=AIMessage(content="technical, fundamental, news"))
        self.stream_chunks = stream_chunks or ["On balance ", "the read here is cautious."]
        self.stream_raises = stream_raises
        self.stream_prompts: list[Any] = []
        thesis = make_thesis()
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

    def with_structured_output(self, schema: type, **_kwargs: object) -> MagicMock:
        return self._make_structured(schema)

    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        if self.stream_raises:
            raise RuntimeError("simulated stream failure")
        if _args:
            self.stream_prompts.append(_args[0])
        return iter(AIMessage(content=c) for c in self.stream_chunks)


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch) -> _StubLLM:
    stub = _StubLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)
    return stub


@pytest.fixture
def saver() -> Any:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    return SqliteSaver(conn)


def _default_tools() -> dict[str, MagicMock]:
    return {
        "technical": MagicMock(return_value="## technical\nRSI 78\n"),
        "fundamental": MagicMock(return_value="## fundamental\nP/E 80\n"),
        "company": MagicMock(return_value="## company\nDescription\n"),
        "news": MagicMock(return_value="## news\n- headline\n"),
    }


def _make_emitter() -> tuple[list[tuple[str, dict[str, object]]], Any]:
    events: list[tuple[str, dict[str, object]]] = []

    def emit(event: str, data: dict[str, object]) -> None:
        events.append((event, dict(data)))

    return events, emit


def test_narrate_assembles_text_and_emits_chunks_in_order(stub_llm: _StubLLM) -> None:
    """AC1: narrate node on a thesis run populates state['narrative'] AND
    fires narrative_chunk events in stream order via the emitter."""
    stub_llm.stream_chunks = ["I'd lean ", "Overweight ", "on this one."]
    events, emit = _make_emitter()
    graph = build_graph(_default_tools(), event_emitter=emit)
    result = graph.invoke({"ticker": "TSLA", "question": "is TSLA overvalued?"})

    assert result["intent"] == "thesis"
    assert isinstance(result.get("answer"), Thesis)
    # The narrate node assembled the chunks into one string.
    assert result.get("narrative") == "I'd lean Overweight on this one."
    # And the same chunks streamed via the emitter, in order.
    chunks = [data["delta"] for event, data in events if event == "narrative_chunk"]
    assert chunks == ["I'd lean ", "Overweight ", "on this one."]


def test_synthesize_emits_card_before_narrate(stub_llm: _StubLLM) -> None:
    """QNT-229 AC2: synthesize_node emits the structured card via the emitter
    the instant it is ready -- BEFORE narrate streams the analyst-voice bubble.
    The early card event must precede the first narrative_chunk, and its payload
    must equal the model_dump of the same card landed in state."""
    stub_llm.stream_chunks = ["I'd lean ", "Overweight."]
    events, emit = _make_emitter()
    graph = build_graph(_default_tools(), event_emitter=emit)
    result = graph.invoke({"ticker": "TSLA", "question": "is TSLA overvalued?"})

    names = [event for event, _ in events]
    assert "thesis" in names, f"expected an early thesis card event, got {names}"
    assert "narrative_chunk" in names, f"expected narrate to stream, got {names}"
    first_card = names.index("thesis")
    first_narrate = names.index("narrative_chunk")
    assert first_card < first_narrate, (
        f"early card (idx {first_card}) must arrive before narrate (idx {first_narrate}): {names}"
    )
    # The emitted payload is the model dump of the same thesis carried in state.
    card_payload = next(data for event, data in events if event == "thesis")
    assert isinstance(result.get("answer"), Thesis)
    assert card_payload == result["answer"].model_dump()


def test_synthesize_does_not_emit_card_for_conversational(
    stub_llm: _StubLLM,  # noqa: ARG001
) -> None:
    """QNT-229 AC2: conversational / fallback-redirect shapes carry no card
    slot, so synthesize emits no early card event for them (their prose still
    streams via prose_chunk on the API side)."""
    events, emit = _make_emitter()
    graph = build_graph(_default_tools(), event_emitter=emit)
    result = graph.invoke({"ticker": "TSLA", "question": "hi"})

    assert result["intent"] == "conversational"
    card_shapes = {
        "thesis",
        "quick_fact",
        "comparison",
        "comparison_lean",
        "focused",
        "exploration",
    }
    assert not any(event in card_shapes for event, _ in events), (
        f"conversational must emit no early card event, got {[e for e, _ in events]}"
    )


def test_narrate_grounding_miss_is_advisory_after_chunks(
    stub_llm: _StubLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-221: runtime grounding runs after completed streaming, lowers
    confidence, and never blocks the structured card or bubble."""
    stub_llm.stream_chunks = ["RSI is ", "99."]
    events, emit = _make_emitter()
    original_check = graph_module._runtime_grounding_check

    def wrapped_check(answer: str, reports: list[str]) -> Any:
        assert any(event == "narrative_chunk" for event, _ in events)
        return original_check(answer, reports)

    monkeypatch.setattr(graph_module, "_runtime_grounding_check", wrapped_check)
    graph = build_graph(_default_tools(), event_emitter=emit)

    result = graph.invoke({"ticker": "TSLA", "question": "is TSLA overvalued?"})

    assert isinstance(result.get("answer"), Thesis)
    assert result.get("narrative") == "RSI is 99."
    assert result["grounding_rate"] < 1.0
    assert result["confidence"] < 1.0
    assert "99" in result["grounding_unsupported"]


def test_narrate_failure_does_not_break_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4: narrate LLM raise → state.narrative is None, no narrative_chunk
    events emitted, thesis payload still populated, run terminates normally."""
    stub = _StubLLM(stream_raises=True)
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)

    events, emit = _make_emitter()
    graph = build_graph(_default_tools(), event_emitter=emit)
    result = graph.invoke({"ticker": "TSLA", "question": "is TSLA overvalued?"})

    # Structured payload still landed.
    assert isinstance(result.get("answer"), Thesis)
    # Narrative degraded to None.
    assert result.get("narrative") is None
    # No narrative_chunk events made it to the emitter.
    assert not any(event == "narrative_chunk" for event, _ in events)


def test_followup_narrative_only_skips_quick_fact(
    stub_llm: _StubLLM,
    saver: Any,
) -> None:
    """AC5: thesis turn → followup turn 'what does that mean for retail?'
    (no metric ask) leaves quick_fact=None and narrative carries the answer."""
    tools = _default_tools()
    events, emit = _make_emitter()
    graph = build_graph(tools, event_emitter=emit, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "narrative-only:TSLA"}}

    # Turn 1: thesis hydrates state.
    first = graph.invoke({"ticker": "TSLA", "question": "is TSLA overvalued?"}, config=config)
    assert first["intent"] == "thesis"

    events.clear()
    for t in tools.values():
        t.reset_mock()

    # Turn 2: conversational continuation -- "tell me more" is a followup
    # token, no quick-fact token. _followup_is_metric_ask returns False
    # → narrative-only path.
    second = graph.invoke({"ticker": "TSLA", "question": "tell me more"}, config=config)
    assert second["intent"] == "followup"
    # No tool calls on the followup turn.
    assert sum(t.call_count for t in tools.values()) == 0
    # The defining assertion: no QuickFactAnswer; narrate owned the response.
    assert second.get("answer") is None
    # narrative_chunk events DID arrive (narrate ran).
    chunk_events = [e for e in events if e[0] == "narrative_chunk"]
    assert chunk_events, "expected narrative_chunk events on the followup turn"
    # And the assembled narrative landed in state.
    assert second.get("narrative")


def test_followup_metric_ask_keeps_quick_fact(
    stub_llm: _StubLLM,
    saver: Any,
) -> None:
    """AC6: same setup + 'elaborate on the RSI' (metric ask) keeps the
    QuickFactAnswer path AND runs narrate on top."""
    tools = _default_tools()
    events, emit = _make_emitter()
    graph = build_graph(tools, event_emitter=emit, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "metric-ask:TSLA"}}

    # Turn 1: thesis hydrates state.
    graph.invoke({"ticker": "TSLA", "question": "is TSLA overvalued?"}, config=config)

    events.clear()
    for t in tools.values():
        t.reset_mock()

    # Turn 2: followup + metric token.
    second = graph.invoke({"ticker": "TSLA", "question": "elaborate on the RSI"}, config=config)
    assert second["intent"] == "followup"
    # Both signals must be present: the structured card AND the bubble.
    assert isinstance(second.get("answer"), QuickFactAnswer)
    chunk_events = [e for e in events if e[0] == "narrative_chunk"]
    assert chunk_events, "expected narrative_chunk events on metric-ask followup"
    assert second.get("narrative")


def test_narrate_prompt_strips_structured_payload_disclaimer(
    stub_llm: _StubLLM,
) -> None:
    """The card markdown keeps its footer, but narrate should not read it."""
    from agent.disclaimer import DISCLAIMER

    graph = build_graph(_default_tools())

    result = graph.invoke({"ticker": "NVDA", "question": "Give me an NVDA thesis."})

    assert DISCLAIMER in result["answer"].to_markdown()
    rendered_prompt = "\n".join(
        str(getattr(message, "content", message))
        for prompt in stub_llm.stream_prompts
        for message in prompt
    )
    assert DISCLAIMER not in rendered_prompt


def test_quick_fact_intent_skips_narrate(stub_llm: _StubLLM) -> None:
    """QNT-232 #3 (AC2): a quick_fact turn makes exactly ONE default-alias LLM
    call -- synthesize -- and skips narrate. The QuickFactAnswer card carries
    the answer + cited value; no narrative bubble streams above it."""
    events, emit = _make_emitter()
    graph = build_graph(
        {"technical": MagicMock(return_value="## technical\nRSI 78\n")}, event_emitter=emit
    )

    result = graph.invoke({"ticker": "NVDA", "question": "What's NVDA's RSI right now?"})

    assert result["intent"] == "quick_fact"
    # Surviving surface: the structured card carries the answer + cited value.
    qf = result.get("answer")
    assert isinstance(qf, QuickFactAnswer)
    assert qf.answer
    assert qf.cited_value == "78"
    # narrate skipped: no narrative bubble streamed and the stub's .stream()
    # (the only default-alias call narrate would make) was never invoked.
    assert result.get("narrative") is None
    assert not any(event == "narrative_chunk" for event, _ in events)
    assert stub_llm.stream_prompts == []
    # The card still emitted (early, from synthesize) as the lone surface.
    assert any(event == "quick_fact" for event, _ in events)


def test_conversational_intent_skips_narrate(
    stub_llm: _StubLLM,  # noqa: ARG001
) -> None:
    """Conversational intent already speaks in the right voice -- narrate
    short-circuits so the bubble doesn't echo the same prose twice."""
    events, emit = _make_emitter()
    graph = build_graph(_default_tools(), event_emitter=emit)
    result = graph.invoke({"ticker": "TSLA", "question": "hi"})

    assert result["intent"] == "conversational"
    # narrate node ran but produced nothing — no narrative_chunk events.
    assert not any(event == "narrative_chunk" for event, _ in events)
    assert result.get("narrative") is None


def test_fallback_redirect_skips_narrate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: when synthesize hits _fallback() (no reports, structured-
    output crash, etc.) it leaves the original intent intact and populates
    state['conversational'] with a domain_redirect. narrate must NOT narrate
    over the redirect — otherwise the user sees a duplicate bubble above
    the same conversational card."""
    stub = _StubLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)

    # No tools registered → plan_node sees no available tools → gather returns
    # empty reports → synthesize's thesis path hits the "I couldn't pull any
    # reports..." fallback, populating state['conversational'] while keeping
    # intent='thesis'.
    events, emit = _make_emitter()
    graph = build_graph({}, event_emitter=emit)
    result = graph.invoke({"ticker": "TSLA", "question": "thesis on TSLA?"})

    # The fallback fired (conversational redirect present, thesis None).
    assert result.get("answer") is not None
    # narrate stayed silent — no narrative_chunk events, narrative=None.
    assert not any(event == "narrative_chunk" for event, _ in events)
    assert result.get("narrative") is None


def _bare_deps() -> GraphDeps:
    return GraphDeps(
        tools={},
        event_emitter=None,
        compact_company_tool=None,
        comparison_metrics_tool=None,
        active_retrievals=(),
    )


def test_dropped_card_substrate_reads_from_state_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """QNT-320 (G-1) AC3: on the focused RAG-drop path (answer=None) narrate picks
    its substrate from the ``narrative_substrate`` key synthesize wrote -- it no
    longer re-derives the needs_news_search / needs_earnings_search predicate that
    used to mirror the synthesize-side drop condition.

    The state below sets the key and the folded news report but deliberately leaves
    ``needs_news_search`` / ``retrieved_sources`` UNSET, so a narrate prompt built
    over ``reports['news']`` proves the substrate came from the key alone."""
    captured: dict[str, Any] = {}

    def _fake_prompt(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "PROMPT"

    monkeypatch.setattr(graph_module, "build_narrate_prompt", _fake_prompt)
    stub = _StubLLM(stream_chunks=["ok."])
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)

    config: RunnableConfig = {"configurable": {"thread_id": "drop:NVDA"}}
    news_report = "## news\nNVDA strikes Micron HBM4 supply deal\n"
    narrate_node(
        {
            "ticker": "NVDA",
            "question": "any news on the Micron deal?",
            "intent": "news",
            "answer": None,
            "narrative_substrate": "news",
            "reports": {"news": news_report},
        },
        config,
        _bare_deps(),
    )
    assert captured["payload_markdown"] == news_report

    # With the key absent, narrate does NOT fall back to the old predicate even
    # when needs_news_search + retrieved_sources are present -- the mirrored
    # re-derivation is gone, so the substrate stays empty.
    captured.clear()
    narrate_node(
        {
            "ticker": "NVDA",
            "question": "any news on the Micron deal?",
            "intent": "news",
            "answer": None,
            "needs_news_search": True,
            "retrieved_sources": [{"headline": "H", "corpus": "news"}],
            "reports": {"news": news_report},
        },
        config,
        _bare_deps(),
    )
    assert captured["payload_markdown"] == ""


_FUND_REPORT_WITH_PEERS = (
    "# FUNDAMENTAL REPORT — NVDA\n"
    "## SCALE\nRevenue $60.9B (source: fundamental)\n\n"
    "## PEER CONTEXT\n"
    "Sector median P/E (Technology, 5 peers in coverage): 24.5 -- NVDA at 42.1 (72% premium)\n"
    "Sector median EV/EBITDA (Technology, 5 peers in coverage): 18.0 -- NVDA at 30.0 "
    "(67% premium)\n\n"
    "## QUARTERLY\n"
    "### QUARTERLY VALUATION\n"
    "P/E 42.1 (range 18.0–45.0 over last 5y, near the high, prior period 40.9) — Premium\n"
    "### QUARTERLY GROWTH (YoY)\n"
    "Revenue +85.2% YoY (source: fundamental)\n"
)


def test_narrate_folds_peer_and_own_history_when_payload_has_valuation_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-359 AC1 (fix B): a narrate payload carrying a valuation label
    (Thesis fundamental **Label:** Premium) folds BOTH the fundamental report's
    ## PEER CONTEXT block (peer-delta %) AND its ### QUARTERLY VALUATION block
    (the own-history "range ... over last 5y" line that drives the label when
    peer coverage is thin) into narrate's input substrate -- so a spoken
    comparison magnitude, versus peers OR its own history, is quotable-verbatim
    instead of fabricated. Stays tight: the growth subsection is NOT dragged in."""
    captured: dict[str, Any] = {}

    def _fake_prompt(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "PROMPT"

    monkeypatch.setattr(graph_module, "build_narrate_prompt", _fake_prompt)
    stub = _StubLLM(stream_chunks=["ok."])
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)

    config: RunnableConfig = {"configurable": {"thread_id": "peer-fold:NVDA"}}
    narrate_node(
        {
            "ticker": "NVDA",
            "question": "is NVDA's P/E stretched?",
            "intent": "thesis",
            "answer": make_thesis(),  # fundamental.label = "Premium"
            "reports": {"fundamental": _FUND_REPORT_WITH_PEERS},
        },
        config,
        _bare_deps(),
    )

    payload = captured["payload_markdown"]
    # The peer-delta magnitude reaches the prompt, quotable verbatim.
    assert "72% premium" in payload
    assert "## PEER CONTEXT" in payload
    # The own-history range line reaches it too (the axis the prompt invites).
    assert "range 18.0–45.0 over last 5y" in payload
    assert "### QUARTERLY VALUATION" in payload
    # Tight fold: the growth subsection after the valuation block is NOT dragged in.
    assert "### QUARTERLY GROWTH" not in payload
    assert "+85.2% YoY" not in payload


def test_narrate_does_not_fold_peer_section_without_valuation_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-359: the fold is gated on the payload carrying a valuation label.
    A news-substrate turn (no Premium/Inline/Discounted label) leaves the
    substrate untouched -- no peer section is bolted on."""
    captured: dict[str, Any] = {}

    def _fake_prompt(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "PROMPT"

    monkeypatch.setattr(graph_module, "build_narrate_prompt", _fake_prompt)
    stub = _StubLLM(stream_chunks=["ok."])
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)

    config: RunnableConfig = {"configurable": {"thread_id": "no-fold:NVDA"}}
    news_report = "## news\nNVDA strikes Micron HBM4 supply deal\n"
    narrate_node(
        {
            "ticker": "NVDA",
            "question": "any news on the Micron deal?",
            "intent": "news",
            "answer": None,
            "narrative_substrate": "news",
            "reports": {"news": news_report, "fundamental": _FUND_REPORT_WITH_PEERS},
        },
        config,
        _bare_deps(),
    )

    assert captured["payload_markdown"] == news_report
    assert "PEER CONTEXT" not in captured["payload_markdown"]


def test_card_bearing_path_clears_narrative_substrate() -> None:
    """QNT-320 regression: synthesize writes ``narrative_substrate`` on EVERY return
    path -- a card-bearing path clears it to None. So a prior drop-card turn's
    "news"/"fundamental" cannot persist through the checkpointer into a later
    real-payload turn and get mis-read as substrate (the staleness class this
    anti-drift ticket exists to prevent). Uses the deterministic no-reports fallback
    so no LLM is involved."""
    from agent.nodes.synthesize import _synthesize_payload

    config: RunnableConfig = {"configurable": {"thread_id": "clear:TSLA"}}
    result = _synthesize_payload(
        {
            "ticker": "TSLA",
            "question": "thesis on TSLA?",
            "intent": "thesis",
            "reports": {},  # no reports -> deterministic domain_redirect fallback (no LLM)
            "narrative_substrate": "news",  # stale value a prior drop-card turn left
        },
        config,
    )
    assert result["narrative_substrate"] is None
