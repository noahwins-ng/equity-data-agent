"""QNT-216 conversation history + cache-friendly prompt assembly tests."""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import MagicMock

from agent import graph as graph_module
from agent.graph import build_graph
from agent.intent import _heuristic_intent
from agent.prompts import ConversationMessage, build_narrate_prompt, build_synthesis_prompt
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver

from ._thesis_factory import make_thesis


class _StubLLM:
    def __init__(self) -> None:
        self.invoke = MagicMock(return_value=AIMessage(content="company, technical, fundamental"))
        self.structured_prompts: dict[type, list[Any]] = {}
        self.stream_prompts: list[Any] = []
        thesis = make_thesis(
            company_summary="AAPL thesis with RETAIL_CONTEXT_TOKEN (source: company).",
            verdict="Neutral",
            verdict_rationale="Inline valuation and Uptrend trend keep the read balanced.",
        )
        quick_fact = QuickFactAnswer(
            answer="The prior retail framing still points back to AAPL.",
            cited_value="",
            source=None,
        )

        def make_structured(schema: type) -> MagicMock:
            runnable = MagicMock()
            if schema is Thesis:
                response = thesis
            elif schema is QuickFactAnswer:
                response = quick_fact
            else:
                response = None

            def invoke(prompt: Any, *_args: Any, **_kwargs: Any) -> Any:
                self.structured_prompts.setdefault(schema, []).append(prompt)
                return response

            runnable.invoke = MagicMock(side_effect=invoke)
            runnable.with_retry.return_value = runnable
            return runnable

        self._make_structured = make_structured

    def with_structured_output(self, schema: type) -> MagicMock:
        return self._make_structured(schema)

    def stream(self, prompt: Any, *_args: Any, **_kwargs: Any) -> Any:
        self.stream_prompts.append(prompt)
        return iter([AIMessage(content="Retail read stays tied to AAPL.")])


def _tools() -> dict[str, MagicMock]:
    return {
        "company": MagicMock(return_value="## company\nAAPL retail exposure\n"),
        "technical": MagicMock(return_value="## technical\nRSI 55\n"),
        "fundamental": MagicMock(return_value="## fundamental\nP/E Inline\n"),
    }


def test_message_history_round_trips_through_checkpointer(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    """AgentState.messages persists user + assistant turns across invocations."""
    stub = _StubLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)

    db_path = tmp_path / "agent-history.db"
    config: RunnableConfig = {"configurable": {"thread_id": "history:AAPL"}}

    conn1 = sqlite3.connect(str(db_path), check_same_thread=False)
    graph1 = build_graph(_tools(), checkpointer=SqliteSaver(conn1))
    first = graph1.invoke({"ticker": "AAPL", "question": "Give me an AAPL thesis."}, config=config)
    assert [m["role"] for m in first["messages"]] == ["user", "assistant"]
    assert "Structured payload: thesis" in first["messages"][-1]["content"]
    conn1.close()

    conn2 = sqlite3.connect(str(db_path), check_same_thread=False)
    graph2 = build_graph(_tools(), checkpointer=SqliteSaver(conn2))
    second = graph2.invoke(
        {"ticker": "AAPL", "question": "what does that mean for retail?"},
        config=config,
    )
    assert [m["role"] for m in second["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert second["messages"][-2]["content"] == "what does that mean for retail?"
    assert len(second["messages"]) <= 20
    conn2.close()


def test_retail_followup_routes_to_followup_not_conversational(
    monkeypatch: Any,
) -> None:
    """Regression for the observed AAPL -> retail continuation misroute."""
    assert _heuristic_intent("what does that mean for retail?", has_prior_turn=True) == "followup"

    stub = _StubLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)
    tools = _tools()
    graph = build_graph(
        tools,
        checkpointer=SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)),
    )
    config: RunnableConfig = {"configurable": {"thread_id": "retail:AAPL"}}

    graph.invoke({"ticker": "AAPL", "question": "Give me an AAPL thesis."}, config=config)
    for tool in tools.values():
        tool.reset_mock()

    second = graph.invoke(
        {"ticker": "AAPL", "question": "what does that mean for retail?"},
        config=config,
    )
    assert second["intent"] == "followup"
    assert second.get("conversational") is None
    assert sum(tool.call_count for tool in tools.values()) == 0


def test_rebased_turn_sets_followup_ticker_context(
    monkeypatch: Any,
) -> None:
    """A bare followup after a rebased turn follows the rebased ticker."""
    stub = _StubLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)
    tools = _tools()
    graph = build_graph(
        tools,
        checkpointer=SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)),
    )
    config: RunnableConfig = {"configurable": {"thread_id": "rebase-followup:NVDA"}}

    first = graph.invoke({"ticker": "NVDA", "question": "Give me an NVDA thesis."}, config=config)
    assert first["ticker"] == "NVDA"
    assert first["analysis_ticker"] == "NVDA"

    for tool in tools.values():
        tool.reset_mock()
    second = graph.invoke({"ticker": "NVDA", "question": "What's AAPL's P/E?"}, config=config)
    assert second["ticker"] == "AAPL"
    assert second["analysis_ticker"] == "AAPL"
    assert {call.args[0] for tool in tools.values() for call in tool.call_args_list} == {"AAPL"}

    for tool in tools.values():
        tool.reset_mock()
    third = graph.invoke({"ticker": "NVDA", "question": "why?"}, config=config)
    assert third["intent"] == "followup"
    assert third["ticker"] == "AAPL"
    assert third["analysis_ticker"] == "AAPL"
    assert sum(tool.call_count for tool in tools.values()) == 0


def test_history_prompt_prefix_is_byte_stable() -> None:
    history: list[ConversationMessage] = [
        {"role": "user", "content": "Give me an AAPL thesis."},
        {"role": "assistant", "content": "RETAIL_CONTEXT_TOKEN\nStructured payload: thesis"},
    ]
    first = build_synthesis_prompt(
        "AAPL",
        "what does that mean for retail?",
        {"company": "first report"},
        history=history,
    )
    second = build_synthesis_prompt(
        "AAPL",
        "what changes if demand softens?",
        {"company": "second report"},
        history=history,
    )

    first_prefix = [message.model_dump() for message in first[:-1]]
    second_prefix = [message.model_dump() for message in second[:-1]]
    assert first_prefix == second_prefix
    assert "RETAIL_CONTEXT_TOKEN" in first[2].content
    assert "first report" not in "\n".join(str(message.content) for message in first[:-1])


def test_graph_prompt_prefix_is_append_only_across_thread_turns(
    monkeypatch: Any,
) -> None:
    """Graph-built prompts preserve earlier prefix bytes as history grows."""
    stub = _StubLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)

    graph = build_graph(
        _tools(),
        checkpointer=SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)),
    )
    config: RunnableConfig = {"configurable": {"thread_id": "cache-prefix:AAPL"}}

    graph.invoke({"ticker": "AAPL", "question": "Give me an AAPL thesis."}, config=config)
    graph.invoke({"ticker": "AAPL", "question": "Give me another AAPL thesis."}, config=config)
    graph.invoke({"ticker": "AAPL", "question": "Give me one more AAPL thesis."}, config=config)

    thesis_prompts = stub.structured_prompts[Thesis]
    assert len(thesis_prompts) == 3

    second_prefix = [message.model_dump() for message in thesis_prompts[1][:-1]]
    third_prefix = [message.model_dump() for message in thesis_prompts[2][:-1]]
    assert third_prefix[: len(second_prefix)] == second_prefix
    second_prefix_text = "\n".join(str(message.content) for message in thesis_prompts[1][:-1])
    assert "Give me another AAPL thesis." not in second_prefix_text
    assert "Give me another AAPL thesis." in str(thesis_prompts[2][3].content)
    assert "company" in str(thesis_prompts[2][-1].content)


def test_history_budget_is_intent_aware() -> None:
    """QNT-232 #13: fresh analytical intents trim to the small budget; only
    continuations (followup / conversational / clarify) keep the full limit."""
    from agent.graph import (
        _FRESH_ANALYTICAL_HISTORY_TURNS,
        _history_budget,
    )
    from agent.prompts import HISTORY_TURN_LIMIT

    for fresh in (
        "thesis",
        "quick_fact",
        "fundamental",
        "technical",
        "news",
        "comparison",
        "exploration",
    ):
        assert _history_budget(fresh) == _FRESH_ANALYTICAL_HISTORY_TURNS
    for deep in ("followup", "conversational", "clarify"):
        assert _history_budget(deep) == HISTORY_TURN_LIMIT


def _seeded_history(n_turns: int) -> list[ConversationMessage]:
    """n_turns of user/assistant pairs, each tagged with a unique token."""
    msgs: list[ConversationMessage] = []
    for i in range(n_turns):
        msgs.append({"role": "user", "content": f"TURN{i} question"})
        msgs.append({"role": "assistant", "content": f"TURN{i} answer\nStructured payload: thesis"})
    return msgs


def test_fresh_intent_trims_assembled_prompt_history(monkeypatch: Any) -> None:
    """QNT-232 #13 (AC3): with a deep transcript seeded, a fresh thesis turn's
    assembled synthesize prompt carries only the trimmed budget of history,
    while a followup turn keeps the full depth."""
    from agent.prompts import HISTORY_TURN_LIMIT

    fresh_budget_msgs = 3 * 2  # _FRESH_ANALYTICAL_HISTORY_TURNS turns

    # --- fresh thesis: heuristic-forced, deep seeded history ---
    stub = _StubLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr(
        graph_module, "classify_intent_with_source", lambda _q, **_: ("thesis", "heuristic", False)
    )
    graph = build_graph(_tools())
    graph.invoke(
        {
            "ticker": "AAPL",
            "question": "Give me an AAPL thesis.",
            "messages": _seeded_history(8),
        }
    )
    thesis_prompt = stub.structured_prompts[Thesis][-1]
    # Prompt = [system, *history, current_user]; history sits in the middle.
    thesis_history = thesis_prompt[1:-1]
    assert len(thesis_history) <= fresh_budget_msgs, (
        f"fresh thesis must trim history to <= {fresh_budget_msgs} messages, "
        f"got {len(thesis_history)}"
    )

    # --- continuation followup: keeps the deep budget ---
    stub2 = _StubLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub2)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub2)
    monkeypatch.setattr(
        graph_module, "classify_intent_with_source", lambda _q, **_: ("followup", "heuristic", True)
    )
    graph2 = build_graph(_tools())
    graph2.invoke(
        {
            "ticker": "AAPL",
            "question": "what's the P/E?",  # metric ask -> build_followup_prompt
            "messages": _seeded_history(8),
            "reports": {"fundamental": "## fundamental\nP/E 31\n"},
            "thesis": make_thesis(),
        }
    )
    followup_prompt = stub2.structured_prompts[QuickFactAnswer][-1]
    followup_history = followup_prompt[1:-1]
    assert len(followup_history) > fresh_budget_msgs, (
        "followup must keep more than the fresh budget of history"
    )
    assert len(followup_history) <= HISTORY_TURN_LIMIT * 2


def test_narrate_prompt_includes_history_and_cache_boundary() -> None:
    history: list[ConversationMessage] = [{"role": "assistant", "content": "PRIOR_TURN_TOKEN"}]
    prompt = build_narrate_prompt(
        intent="followup",
        ticker="AAPL",
        question="what does that mean for retail?",
        payload_markdown="",
        prior_thesis_markdown="AAPL thesis markdown",
        history=history,
    )
    assert any("PRIOR_TURN_TOKEN" in str(message.content) for message in prompt)
