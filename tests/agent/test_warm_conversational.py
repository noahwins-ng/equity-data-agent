"""QNT-217: warm-thread conversational fallback should not show the cold-start card.

A low-information acknowledgement inside an active analysis thread
("Great, im align with you bro" after an NVDA read) must NOT reset the user
to the cold-start capability card. The fix is context-driven, not a phrase
list: ``build_conversational_prompt`` selects a warm system prompt whenever
the thread carries prior turns, and the cold capability prompt only on a
fresh thread.

The prompt-level tests freeze the selection logic; the graph-level
regression replays the observed sequence end-to-end and asserts the warm
prompt is used with zero tool calls.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import MagicMock

import pytest
from agent import graph as graph_module
from agent.conversational import ConversationalAnswer
from agent.graph import build_graph
from agent.intent import IntentDecision
from agent.prompts import (
    ANALYST_VOICE_ADR,
    NEUTRAL_GREETING_SYSTEM_PROMPT,
    ConversationMessage,
    build_conversational_prompt,
)
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver

from ._thesis_factory import make_thesis

# The distinctive header the cold-start capability prompt prints. Its absence
# is the regression guard: a warm reply must never enumerate capabilities.
_COLD_CARD_MARKER = "# What the agent CAN do"


def _system_text(messages: list[Any]) -> str:
    for message in messages:
        if isinstance(message, SystemMessage):
            return str(message.content)
    raise AssertionError("no SystemMessage in prompt")


# ─── Prompt-level selection (AC1, AC2, AC4, AC7) ───────────────────────────


def test_fresh_thread_uses_cold_capability_prompt() -> None:
    """AC1: with no history, the capability card prompt is used."""
    prompt = build_conversational_prompt("what can you do?")
    system = _system_text(prompt)
    assert _COLD_CARD_MARKER in system
    assert ANALYST_VOICE_ADR in system


def test_cold_capability_prompt_asks_for_next_step() -> None:
    """Capability asks should invite the next ticker/angle, not stop at a generic card."""
    prompt = build_conversational_prompt("Hi, what can you help me with?")
    system = _system_text(prompt).lower()

    assert "ask one direct next-step question" in system
    assert "exactly 3 concrete starter questions" in system
    assert "avoid generic" in system


def test_warm_thread_suppresses_cold_capability_prompt() -> None:
    """AC2 + AC4: with prior analysis context, the warm prompt is used --
    no cold capability copy, and the prior ticker/stance is threaded in."""
    history: list[ConversationMessage] = [
        {"role": "user", "content": "how bullish are you in NVDA"},
        {"role": "assistant", "content": "The read on NVDA stays cautious given the setup."},
    ]
    prompt = build_conversational_prompt("Great, im align with you bro", history=history)

    system = _system_text(prompt)
    assert _COLD_CARD_MARKER not in system, "warm reply must not emit the cold capability card"
    assert "cold-start" in system.lower(), "warm prompt should name the cold-start it avoids"
    assert ANALYST_VOICE_ADR in system, "warm prompt keeps the analyst voice"

    prefix_text = "\n".join(str(m.content) for m in prompt[:-1])
    assert "NVDA" in prefix_text, "prior NVDA context must be threaded into the prefix"


def test_bare_greeting_on_warm_thread_uses_neutral_prompt() -> None:
    """A plain 'hi' after analysis is a greeting, not agreement with the read."""
    history: list[ConversationMessage] = [
        {"role": "user", "content": "Should I be cautious about META here?"},
        {"role": "assistant", "content": "The read on META stays cautious."},
    ]
    prompt = build_conversational_prompt("hi", history=history)

    system = _system_text(prompt)
    assert system == NEUTRAL_GREETING_SYSTEM_PROMPT
    assert _COLD_CARD_MARKER not in system
    assert "continuing an in-progress equity-research conversation" not in system
    assert "do not reference the prior ticker" in system.lower()
    prefix_text = "\n".join(str(m.content) for m in prompt[:-1])
    assert "META" not in prefix_text


@pytest.mark.parametrize("greeting", ["halo", "hallow", "Hiya!", "sup", "hello there"])
def test_misspelled_greeting_on_warm_thread_uses_neutral_prompt(greeting: str) -> None:
    """Regression: a mistyped/variant hello on a warm thread is a greeting, not
    an off-domain ask. Previously these fell through the recognized-greeting set
    and the warm prompt bounced them as 'I don't know that'; now they get the
    neutral greeting prompt like a correctly-spelled 'hi'."""
    history: list[ConversationMessage] = [
        {"role": "user", "content": "Should I be cautious about NVDA here?"},
        {"role": "assistant", "content": "The read on NVDA stays cautious."},
    ]
    system = _system_text(build_conversational_prompt(greeting, history=history))
    assert system == NEUTRAL_GREETING_SYSTEM_PROMPT
    assert "continuing an in-progress equity-research conversation" not in system


def test_warm_prompt_keeps_digit_free_guardrail() -> None:
    """AC: the warm prompt preserves the no-digits hallucination guardrail."""
    history: list[ConversationMessage] = [{"role": "user", "content": "NVDA thesis?"}]
    system = _system_text(build_conversational_prompt("thanks", history=history))
    assert "never include numbers" in system.lower()


def test_warm_prompt_keeps_off_domain_redirect_instruction() -> None:
    """AC5: even on a warm thread, an off-domain ask must still redirect to
    equities. The warm prompt carries that instruction so the domain-redirect
    behavior is preserved, not silently dropped for warm threads."""
    history: list[ConversationMessage] = [{"role": "user", "content": "NVDA thesis?"}]
    system = _system_text(
        build_conversational_prompt("what's the weather?", history=history)
    ).lower()
    assert "off-domain" in system
    assert "redirect" in system
    assert "weather" in system


# ─── Graph-level regression (AC3, AC6) ─────────────────────────────────────


class _SequenceStubLLM:
    """Stub LLM for the NVDA -> acknowledgement sequence.

    * IntentDecision -> conversational. Only the ack turn reaches this: the
      opening "Give me an NVDA thesis." matches the "thesis" heuristic token
      and never consults the LLM classifier, while "Great, im align with you
      bro" abstains from the heuristic and defers here.
    * Thesis -> a stub thesis for the opening turn.
    * ConversationalAnswer -> a successful warm reply (so the run does NOT
      exercise the deterministic cold-card fallback).

    Captures every prompt handed to the ConversationalAnswer runnable so the
    test can assert the warm system prompt was selected.
    """

    def __init__(self) -> None:
        self.invoke = MagicMock(return_value=AIMessage(content="company, technical"))
        self.conversational_prompts: list[Any] = []
        thesis = make_thesis(
            company_summary="NVDA read stays cautious (source: company).",
            verdict="Neutral",
            verdict_rationale="Premium valuation and Sideways trend keep the read balanced.",
        )
        warm_reply = ConversationalAnswer(
            answer="Glad the cautious NVDA read lands with you.",
            suggestions=[],
        )

        def make_structured(schema: type) -> MagicMock:
            runnable = MagicMock()

            def invoke(prompt: Any, *_args: Any, **_kwargs: Any) -> Any:
                if schema is IntentDecision:
                    return IntentDecision(intent="conversational")
                if schema is Thesis:
                    return thesis
                if schema is ConversationalAnswer:
                    self.conversational_prompts.append(prompt)
                    return warm_reply
                if schema is QuickFactAnswer:
                    return QuickFactAnswer(answer="n/a", cited_value="", source=None)
                return None

            runnable.invoke = MagicMock(side_effect=invoke)
            runnable.with_retry.return_value = runnable
            return runnable

        self._make_structured = make_structured

    def with_structured_output(self, schema: type) -> MagicMock:
        return self._make_structured(schema)

    def stream(self, prompt: Any, *_args: Any, **_kwargs: Any) -> Any:
        return iter([AIMessage(content="NVDA read stays cautious.")])


def _tools() -> dict[str, MagicMock]:
    return {
        "company": MagicMock(return_value="## company\nNVDA business\n"),
        "technical": MagicMock(return_value="## technical\nSideways\n"),
        "fundamental": MagicMock(return_value="## fundamental\nP/E Premium\n"),
    }


def test_warm_ack_after_nvda_thesis_uses_warm_prompt_no_tools(
    monkeypatch: Any,
) -> None:
    """AC3 + AC6: prior NVDA analysis -> 'Great, im align with you bro' ->
    contextual no-tool conversational reply built from the warm prompt, with
    no cold capability copy and zero tool calls on the follow-up turn."""
    stub = _SequenceStubLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)

    tools = _tools()
    graph = build_graph(
        tools,
        checkpointer=SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)),
    )
    config: RunnableConfig = {"configurable": {"thread_id": "warm:NVDA"}}

    # Turn 1: a real NVDA thesis establishes prior context.
    graph.invoke({"ticker": "NVDA", "question": "Give me an NVDA thesis."}, config=config)
    for tool in tools.values():
        tool.reset_mock()

    # Turn 2: a low-information acknowledgement.
    second = graph.invoke(
        {"ticker": "NVDA", "question": "Great, im align with you bro"},
        config=config,
    )

    assert second["intent"] == "conversational"
    assert isinstance(second["answer"], ConversationalAnswer)
    # AC3: no tools gathered on the warm conversational turn.
    assert sum(tool.call_count for tool in tools.values()) == 0

    # AC6: the conversational reply was built from the warm prompt, not the
    # cold capability card, and it carries the prior NVDA context.
    assert stub.conversational_prompts, "conversational synthesize must have fired"
    last_prompt = stub.conversational_prompts[-1]
    system = _system_text(last_prompt)
    assert _COLD_CARD_MARKER not in system
    prefix_text = "\n".join(str(m.content) for m in last_prompt[:-1])
    assert "NVDA" in prefix_text


def test_bare_hi_after_nvda_thesis_uses_neutral_prompt_no_tools(
    monkeypatch: Any,
) -> None:
    """Regression for warm-thread 'hi' being interpreted as agreement."""
    stub = _SequenceStubLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)

    tools = _tools()
    graph = build_graph(
        tools,
        checkpointer=SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)),
    )
    config: RunnableConfig = {"configurable": {"thread_id": "warm-hi:NVDA"}}

    graph.invoke({"ticker": "NVDA", "question": "Give me an NVDA thesis."}, config=config)
    for tool in tools.values():
        tool.reset_mock()

    second = graph.invoke({"ticker": "NVDA", "question": "hi"}, config=config)

    assert second["intent"] == "conversational"
    assert isinstance(second["answer"], ConversationalAnswer)
    assert sum(tool.call_count for tool in tools.values()) == 0

    assert stub.conversational_prompts, "conversational synthesize must have fired"
    last_prompt = stub.conversational_prompts[-1]
    assert _system_text(last_prompt) == NEUTRAL_GREETING_SYSTEM_PROMPT
