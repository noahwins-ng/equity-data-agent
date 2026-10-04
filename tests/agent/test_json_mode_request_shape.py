"""QNT-493 (AC2): every default-alias structured call goes out as json_mode.

First-party DeepSeek -- the only implicit-caching OpenRouter endpoint for the
primary -- advertises ``response_format`` but NOT ``structured_outputs`` and fails
forced ``tool_choice``. Under the alias's ``require_parameters: true`` both the
``json_schema`` and ``function_calling`` request shapes filter it out (404 ->
silent Nemotron fallback, the QNT-492 class). ``response_format: json_object``
reaches it. These tests pin the request payload that actually leaves
``ChatOpenAI`` for every answer shape: json_object, no tools / tool_choice /
parallel_tool_calls, and the schema carried in the system prompt (json_mode
sends no schema on the wire, so validation is client-side only).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from agent import graph as graph_module
from agent.llm import get_llm
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

_SHAPES = [s for s, budget in graph_module._OUTPUT_BUDGET.items() if budget is not None]


def _capture_payload(monkeypatch: pytest.MonkeyPatch, reply: str) -> list[dict[str, Any]]:
    """Patch ChatOpenAI._generate to record the request payload and return ``reply``."""
    captured: list[dict[str, Any]] = []

    def _fake_generate(self: ChatOpenAI, messages: Any, stop: Any = None, **kwargs: Any) -> Any:
        captured.append(self._get_request_payload(messages, stop=stop, **kwargs))  # noqa: SLF001
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=reply))])

    monkeypatch.setattr(ChatOpenAI, "_generate", _fake_generate)
    return captured


@pytest.mark.parametrize("schema", _SHAPES, ids=lambda s: s.__name__)
def test_structured_call_sends_json_object_without_tools(
    monkeypatch: pytest.MonkeyPatch, schema: type[BaseModel]
) -> None:
    captured = _capture_payload(monkeypatch, reply="{}")
    prompt = [SystemMessage(content="You are an analyst."), HumanMessage(content="hi")]

    graph_module._structured_call(schema, prompt, {}, "test-prompt", linked=False)

    assert captured, "the structured call never reached ChatOpenAI"
    payload = captured[0]
    assert payload["response_format"] == {"type": "json_object"}
    for key in ("tools", "tool_choice", "parallel_tool_calls"):
        assert key not in payload, f"{schema.__name__}: {key} must not be sent"


@pytest.mark.parametrize("schema", _SHAPES, ids=lambda s: s.__name__)
def test_json_mode_prompt_carries_the_schema(
    monkeypatch: pytest.MonkeyPatch, schema: type[BaseModel]
) -> None:
    """json_mode puts no schema on the wire, so the system prompt must carry it --
    merged into the FIRST system message so it sits in the cacheable prefix."""
    captured = _capture_payload(monkeypatch, reply="{}")
    prompt = [SystemMessage(content="You are an analyst."), HumanMessage(content="hi")]

    graph_module._structured_call(schema, prompt, {}, "test-prompt", linked=False)

    messages = captured[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    system = messages[0]["content"]
    assert system.startswith("You are an analyst.")
    assert json.dumps(schema.model_json_schema(), separators=(",", ":")) in system
    # DeepSeek json_object mode requires the literal word "json" in the prompt.
    assert "JSON" in system


def test_string_prompt_gets_a_schema_system_message(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_payload(monkeypatch, reply="{}")
    schema = graph_module.QuickFactAnswer

    graph_module._structured_call(schema, "what is the price?", {}, "test-prompt", linked=False)

    messages = captured[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert schema.__name__ in messages[0]["content"]
    assert messages[1]["content"] == "what is the price?"


def test_caller_supplied_llm_keeps_its_own_method(monkeypatch: pytest.MonkeyPatch) -> None:
    """The small-alias planner passes its own llm; json_mode is the DEFAULT-alias
    contract only, so the planner's request shape must not change."""
    captured = _capture_payload(monkeypatch, reply="{}")
    small = get_llm(temperature=0.0, model_alias="equity-agent/small")

    graph_module._structured_call(
        graph_module.ThesisPlan, "plan it", {}, "plan", llm=small, linked=False
    )

    assert captured[0]["response_format"] != {"type": "json_object"}
