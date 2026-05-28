"""Tests for the QNT-212 short-circuit edges from classify.

Conversational and followup-no-refetch intents skip plan + gather entirely,
routing classify → synthesize → narrate → END. The normal pipeline
(thesis / focused / quick_fact / comparison) still walks all five nodes.

The defining assertion: ``intent_path`` on the final state reads
``["classify", "synthesize", "narrate"]`` for short-circuit runs and
``["classify", "plan", "gather", "synthesize", "narrate"]`` for the full
pipeline. tools mocks see zero calls on the short-circuit paths.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import MagicMock

import pytest
from agent import graph as graph_module
from agent.conversational import ConversationalAnswer
from agent.graph import build_graph
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver

from ._thesis_factory import make_thesis


class _StubLLM:
    """Three-channel stub matching tests/agent/test_followup._StubLLM."""

    def __init__(self) -> None:
        self.invoke = MagicMock(return_value=AIMessage(content="technical, fundamental, news"))
        thesis = make_thesis()
        quick_fact = QuickFactAnswer(
            answer="RSI 78 (source: technical).", cited_value="78", source="technical"
        )
        conv = ConversationalAnswer(
            answer="Hi! Ask me about NVDA, AAPL, or any of the covered tickers.",
            suggestions=[],
        )

        def make_structured(schema: type) -> MagicMock:
            m = MagicMock()
            if schema is Thesis:
                m.invoke = MagicMock(return_value=thesis)
            elif schema is QuickFactAnswer:
                m.invoke = MagicMock(return_value=quick_fact)
            elif schema is ConversationalAnswer:
                m.invoke = MagicMock(return_value=conv)
            else:
                m.invoke = MagicMock(return_value=None)
            m.with_retry.return_value = m
            return m

        self._make_structured = make_structured

    def with_structured_output(self, schema: type) -> MagicMock:
        return self._make_structured(schema)

    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        return iter([AIMessage(content="On balance, "), AIMessage(content="the read here.")])


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


def test_conversational_greeting_short_circuits(stub_llm: _StubLLM) -> None:  # noqa: ARG001
    """AC: greeting routes classify → synthesize → narrate. plan + gather
    mocks see zero calls; the SSE-side ``intent_path`` reads exactly that."""
    tools = _default_tools()
    graph = build_graph(tools)
    result = graph.invoke({"ticker": "NVDA", "question": "hi"})

    assert result["intent"] == "conversational"
    # Zero tool calls.
    assert sum(t.call_count for t in tools.values()) == 0
    # Path skipped plan + gather.
    assert result["intent_path"] == ["classify", "synthesize", "narrate"]


def test_followup_short_circuits_on_hydrated_thread(stub_llm: _StubLLM) -> None:  # noqa: ARG001
    """AC: a hydrated followup ("why?" after a thesis on the same thread)
    skips plan + gather. intent_path reads classify → synthesize → narrate,
    and zero tools fire on the second turn."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    saver = SqliteSaver(conn)
    tools = _default_tools()
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "shortcircuit-followup:TSLA"}}

    # Turn 1: thesis hydrates state.
    first = graph.invoke({"ticker": "TSLA", "question": "is TSLA overvalued?"}, config=config)
    assert first["intent"] == "thesis"
    assert first["intent_path"] == ["classify", "plan", "gather", "synthesize", "narrate"]

    # Reset tool call counts so the second-turn assertion is unambiguous.
    for t in tools.values():
        t.reset_mock()

    # Turn 2: pronoun followup -- no tools, plan + gather skipped.
    second = graph.invoke({"ticker": "TSLA", "question": "why?"}, config=config)
    assert second["intent"] == "followup"
    assert sum(t.call_count for t in tools.values()) == 0
    assert second["intent_path"] == ["classify", "synthesize", "narrate"]


def test_thesis_walks_full_pipeline(stub_llm: _StubLLM) -> None:  # noqa: ARG001
    """Regression: a normal thesis ask still visits every node in order.
    Catches an over-eager short-circuit that would route thesis through
    synthesize without report context."""
    tools = _default_tools()
    graph = build_graph(tools)
    result = graph.invoke({"ticker": "TSLA", "question": "thesis on TSLA?"})

    assert result["intent"] == "thesis"
    assert result["intent_path"] == ["classify", "plan", "gather", "synthesize", "narrate"]
    # Tools fired (full pipeline).
    assert sum(t.call_count for t in tools.values()) > 0


def test_focused_intent_walks_full_pipeline(stub_llm: _StubLLM) -> None:  # noqa: ARG001
    """Focused-analysis intents are NOT in the short-circuit set; they need
    reports gathered to ground the answer. intent_path must include
    plan + gather."""
    tools = _default_tools()
    graph = build_graph(tools)
    result = graph.invoke({"ticker": "NVDA", "question": "give me the technical analysis of NVDA"})

    # Heuristic routes "technical analysis" to focused-technical.
    assert result["intent"] == "technical"
    assert result["intent_path"] == ["classify", "plan", "gather", "synthesize", "narrate"]
