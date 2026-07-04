"""Tests for QNT-213 dynamic thesis planning.

The graph should let the LLM choose a narrow thesis report set with a short
rationale, while preserving the old deterministic all-tools fallback if that
planning call fails.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from agent import graph as graph_module
from agent.graph import REPORT_TOOLS, ThesisPlan, build_graph
from agent.prompts import build_synthesis_prompt
from agent.thesis import Thesis
from langchain_core.messages import AIMessage

from ._thesis_factory import make_thesis


class _Runnable:
    def __init__(self, result: object | None = None, error: Exception | None = None) -> None:
        self.invoke = MagicMock(side_effect=error if error is not None else None)
        if error is None:
            self.invoke.return_value = result
        self.with_retry = MagicMock()
        self.with_retry.return_value = self


class _PlanAwareLLM:
    def __init__(self, plan: ThesisPlan | None = None, plan_error: Exception | None = None) -> None:
        self.plan = plan
        self.plan_error = plan_error
        self.thesis = make_thesis()
        self.invoke = MagicMock(return_value=AIMessage(content="technical"))
        self.plan_runnable: _Runnable | None = None
        self.thesis_runnable: _Runnable | None = None

    def with_structured_output(self, schema: type, **_kwargs: object) -> _Runnable:
        if schema is ThesisPlan:
            self.plan_runnable = _Runnable(self.plan, self.plan_error)
            return self.plan_runnable
        if schema is Thesis:
            self.thesis_runnable = _Runnable(self.thesis)
            return self.thesis_runnable
        return _Runnable(None)

    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        return iter([AIMessage(content="Narrative.")])


def _tools() -> dict[str, MagicMock]:
    return {
        "company": MagicMock(return_value="## company\nBusiness profile\n"),
        "technical": MagicMock(return_value="## technical\nChart setup\n"),
        "fundamental": MagicMock(return_value="## fundamental\nValuation\n"),
        "news": MagicMock(return_value="## news\nHeadlines\n"),
    }


@pytest.fixture(autouse=True)
def force_thesis_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda *_args, **_kwargs: ("thesis", "heuristic", False, False, ""),
    )


def _patch_llm(monkeypatch: pytest.MonkeyPatch, llm: _PlanAwareLLM) -> None:
    monkeypatch.setattr(graph_module, "get_llm", lambda *_args, **_kwargs: llm)


def test_thesis_plan_schema_round_trips() -> None:
    plan = ThesisPlan(
        tools=["company", "fundamental"],
        rationale="Your question is about valuation, so fundamentals carry the read.",
    )

    assert ThesisPlan.model_validate_json(plan.model_dump_json()) == plan


def test_valuation_question_fetches_company_and_fundamental_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rationale = (
        "Your question is about valuation, so I will lean on fundamentals and company context."
    )
    llm = _PlanAwareLLM(ThesisPlan(tools=["company", "fundamental"], rationale=rationale))
    _patch_llm(monkeypatch, llm)
    tools = _tools()

    result = build_graph(tools).invoke({"ticker": "AAPL", "question": "Is AAPL overvalued?"})

    assert result["plan"] == ["company", "fundamental"]
    assert set(result["reports"]) == {"company", "fundamental"}
    assert tools["company"].call_count == 1
    assert tools["fundamental"].call_count == 1
    assert tools["technical"].call_count == 0
    assert tools["news"].call_count == 0
    assert "valuation" in result["plan_rationale"].lower()


def test_chart_question_fetches_company_and_technical_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _PlanAwareLLM(
        ThesisPlan(
            tools=["company", "technical"],
            rationale=(
                "Your question is about the chart setup, so technicals and "
                "company context are enough."
            ),
        )
    )
    _patch_llm(monkeypatch, llm)

    result = build_graph(_tools()).invoke(
        {"ticker": "TSLA", "question": "What's the chart setup on TSLA?"}
    )

    assert result["plan"] == ["company", "technical"]
    assert set(result["reports"]) == {"company", "technical"}


def test_thesis_plan_prompt_treats_broad_thesis_as_full_scope() -> None:
    prompt = graph_module._build_thesis_plan_prompt(  # noqa: SLF001
        "AAPL",
        "what is AAPL thesis",
        list(REPORT_TOOLS),
    )

    assert "broad thesis request" in prompt
    assert "select every available report" in prompt
    assert "company, fundamental, technical, and news" in prompt
    assert "Do not narrow a broad thesis" in prompt


def test_thesis_plan_failure_falls_back_to_all_tools_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    question = "Is AAPL overvalued?"
    llm = _PlanAwareLLM(plan_error=RuntimeError("planner down"))
    _patch_llm(monkeypatch, llm)

    with caplog.at_level(logging.WARNING, logger="agent.graph"):
        result = build_graph(_tools()).invoke({"ticker": "AAPL", "question": question})

    assert result["plan"] == list(REPORT_TOOLS)
    assert set(result["reports"]) == set(REPORT_TOOLS)
    assert result.get("plan_rationale") is None
    assert question in caplog.text
    assert "falling back to all tools" in caplog.text


def test_plan_rationale_reaches_narrate_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rationale = "Your question is about valuation, so fundamentals carry the read."
    llm = _PlanAwareLLM(ThesisPlan(tools=["company", "fundamental"], rationale=rationale))
    _patch_llm(monkeypatch, llm)

    from agent.prompts import build_narrate_prompt as real_build_narrate_prompt

    captured: dict[str, list[Any]] = {}

    def _capture_prompt(*args: Any, **kwargs: Any) -> list[Any]:
        prompt = real_build_narrate_prompt(*args, **kwargs)
        captured["prompt"] = prompt
        return prompt

    monkeypatch.setattr(graph_module, "build_narrate_prompt", _capture_prompt)

    result = build_graph(_tools()).invoke({"ticker": "AAPL", "question": "Is AAPL overvalued?"})

    assert result["plan_rationale"] == rationale
    user_message = captured["prompt"][1]
    assert rationale in str(user_message.content)


def test_thesis_plan_uses_small_alias_synthesis_keeps_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-220 (#7): the plan call resolves the small tiering alias while the
    synthesize call keeps the default 70b -- asserts the per-node alias mapping."""
    from agent.llm import SMALL_NODE_ALIAS

    bindings: list[tuple[type, str | None]] = []

    class _SpyLLM(_PlanAwareLLM):
        def __init__(self, alias: str | None) -> None:
            super().__init__(
                ThesisPlan(tools=["company", "fundamental"], rationale="Valuation read.")
            )
            self._alias = alias

        def with_structured_output(self, schema: type, **_kwargs: object) -> _Runnable:
            bindings.append((schema, self._alias))
            return super().with_structured_output(schema)

    def _factory(*_args: Any, model_alias: str | None = None, **_kwargs: Any) -> _SpyLLM:
        return _SpyLLM(model_alias)

    monkeypatch.setattr(graph_module, "get_llm", _factory)

    build_graph(_tools()).invoke({"ticker": "AAPL", "question": "Is AAPL overvalued?"})

    assert [a for s, a in bindings if s is ThesisPlan] == [SMALL_NODE_ALIAS]
    assert [a for s, a in bindings if s is Thesis] == [None]


def test_partial_thesis_prompt_names_supplied_reports_and_missing_aspect_rule() -> None:
    prompt = build_synthesis_prompt(
        "AAPL",
        "Is AAPL overvalued?",
        {"company": "company body", "fundamental": "fundamental body"},
    )

    system_message = str(prompt[0].content)
    user_message = str(prompt[1].content)
    assert 'summary`` to "Not fetched for this question."' in system_message
    assert "Base the verdict only on the supplied reports." in system_message
    assert "Supplied reports: company, fundamental" in user_message
    assert "technical report" not in user_message
