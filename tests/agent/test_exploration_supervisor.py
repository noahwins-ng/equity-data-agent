"""Tests for the QNT-215 exploration-supervisor route."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from agent import graph as graph_module
from agent.focused import FocusedAnalysis
from agent.graph import REPORT_TOOLS, ExplorationDecision, build_graph
from agent.thesis import Thesis
from langchain_core.messages import AIMessage

from ._thesis_factory import make_thesis


class _Runnable:
    def __init__(self, result: object | None = None, error: Exception | None = None) -> None:
        self.invoke = MagicMock(side_effect=error if error is not None else None)
        if error is None:
            self.invoke.return_value = result
        self.with_retry = MagicMock(return_value=self)


class _ExplorationLLM:
    def __init__(
        self,
        decisions: list[ExplorationDecision] | None = None,
        *,
        decision_error: Exception | None = None,
    ) -> None:
        self.decisions = list(decisions or [])
        self.decision_error = decision_error
        self.invoke = MagicMock(return_value=AIMessage(content="technical"))
        self.thesis = make_thesis()
        self.exploration_runnable = _Runnable(error=decision_error)
        if decision_error is None:
            self.exploration_runnable.invoke.side_effect = self._next_decision
        self.focused = FocusedAnalysis(
            focus="news",
            summary="News flow is the relevant angle (source: news).",
            key_points=["Latest headlines anchor the follow-up (source: news)."],
            cited_values=[],
            verdict=None,
            existing_development="The running story is news-led (source: news).",
            positive_catalysts=[],
            negative_catalysts=[],
        )

    def _next_decision(self, *_args: Any, **_kwargs: Any) -> ExplorationDecision:
        if self.decisions:
            return self.decisions.pop(0)
        return ExplorationDecision(action="finish", rationale="Done.")

    def with_structured_output(self, schema: type) -> _Runnable:
        if schema is ExplorationDecision:
            return self.exploration_runnable
        if schema is Thesis:
            return _Runnable(result=self.thesis)
        if schema is FocusedAnalysis:
            return _Runnable(result=self.focused)
        return _Runnable(result=None)

    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        return iter([AIMessage(content="Exploration narrative.")])


def _tools() -> dict[str, MagicMock]:
    return {
        "company": MagicMock(return_value="## company\nBusiness profile\n"),
        "technical": MagicMock(return_value="## technical\nChart setup\n"),
        "fundamental": MagicMock(return_value="## fundamental\nValuation\n"),
        "news": MagicMock(return_value="## news\nHeadlines\n"),
    }


def _patch_llm(monkeypatch: pytest.MonkeyPatch, llm: _ExplorationLLM) -> None:
    monkeypatch.setattr(graph_module, "get_llm", lambda *_args, **_kwargs: llm)


def _patch_intent(monkeypatch: pytest.MonkeyPatch, intent: str = "thesis") -> None:
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda *_args, **_kwargs: (intent, "heuristic"),
    )


def test_non_exploratory_thesis_keeps_existing_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_intent(monkeypatch, "thesis")
    _patch_llm(
        monkeypatch,
        _ExplorationLLM([ExplorationDecision(action="news", rationale="Should not run.")]),
    )
    tools = _tools()

    result = build_graph(tools).invoke({"ticker": "AAPL", "question": "Give me an AAPL thesis."})

    assert result["intent_path"] == ["classify", "plan", "gather", "synthesize", "narrate"]
    assert "supervisor_iterations" not in result


def test_exploratory_question_routes_to_supervisor_and_calls_multiple_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_intent(monkeypatch, "thesis")
    _patch_llm(
        monkeypatch,
        _ExplorationLLM(
            [
                ExplorationDecision(action="company", rationale="Start with business context."),
                ExplorationDecision(action="news", rationale="Then inspect recent headlines."),
                ExplorationDecision(action="finish", rationale="Enough context."),
            ]
        ),
    )
    tools = _tools()

    result = build_graph(tools).invoke(
        {"ticker": "AAPL", "question": "What's interesting about AAPL this week?"}
    )

    assert result["intent_path"] == ["classify", "explore_supervisor", "synthesize", "narrate"]
    assert result["intent"] == "thesis"
    assert result["plan"] == ["news", "technical"]
    assert result["supervisor_iterations"] == 3
    assert set(result["reports"]) == {"news", "technical"}
    assert tools["company"].call_count == 0
    assert tools["news"].call_count == 1
    assert tools["technical"].call_count == 1
    assert tools["fundamental"].call_count == 0


def test_broad_exploration_still_routes_when_classifier_labels_news(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_intent(monkeypatch, "news")
    _patch_llm(
        monkeypatch,
        _ExplorationLLM(
            [
                ExplorationDecision(action="news", rationale="Start with current headlines."),
                ExplorationDecision(action="technical", rationale="Check the market setup."),
                ExplorationDecision(action="finish", rationale="Enough context."),
            ]
        ),
    )
    tools = _tools()

    result = build_graph(tools).invoke(
        {"ticker": "AAPL", "question": "What is interesting about AAPL this week?"}
    )

    assert result["intent_path"] == ["classify", "explore_supervisor", "synthesize", "narrate"]
    assert result["plan"] == ["news", "technical"]
    assert result["supervisor_iterations"] == 3
    assert tools["news"].call_count == 1
    assert tools["technical"].call_count == 1
    assert tools["company"].call_count == 0
    assert tools["fundamental"].call_count == 0


def test_news_led_broad_exploration_finish_gets_complementary_lens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_intent(monkeypatch, "thesis")
    _patch_llm(
        monkeypatch,
        _ExplorationLLM(
            [
                ExplorationDecision(action="company", rationale="Start with business context."),
                ExplorationDecision(action="finish", rationale="Enough context."),
            ]
        ),
    )
    tools = _tools()

    result = build_graph(tools).invoke(
        {"ticker": "AAPL", "question": "What should I watch on AAPL next week?"}
    )

    assert result["plan"] == ["news", "technical"]
    assert tools["company"].call_count == 0
    assert tools["news"].call_count == 1
    assert tools["technical"].call_count == 1
    assert tools["fundamental"].call_count == 0


def test_warm_thread_news_angle_skips_exploration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_intent(monkeypatch, "followup")
    _patch_llm(
        monkeypatch,
        _ExplorationLLM(
            [
                ExplorationDecision(action="news", rationale="The user asked for the news angle."),
                ExplorationDecision(action="finish", rationale="News is enough."),
            ]
        ),
    )
    tools = _tools()

    result = build_graph(tools).invoke(
        {
            "ticker": "AAPL",
            "question": "drill into the news angle",
            "messages": [
                {"role": "user", "content": "Give me an AAPL thesis."},
                {"role": "assistant", "content": "Structured payload: thesis verdict=Neutral"},
            ],
            "reports": {"company": "prior company report"},
        }
    )

    assert result["intent_path"] == ["classify", "synthesize", "narrate"]
    assert result["intent"] == "followup"
    assert result.get("plan", []) == []
    assert "supervisor_iterations" not in result
    assert tools["news"].call_count == 0
    assert tools["company"].call_count == 0
    assert tools["technical"].call_count == 0
    assert tools["fundamental"].call_count == 0


def test_warm_thread_broad_interesting_followup_skips_exploration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_intent(monkeypatch, "followup")
    _patch_llm(
        monkeypatch,
        _ExplorationLLM([ExplorationDecision(action="news", rationale="Should not run.")]),
    )
    tools = _tools()

    result = build_graph(tools).invoke(
        {
            "ticker": "AAPL",
            "question": "what's interesting here?",
            "messages": [
                {"role": "user", "content": "Give me an AAPL thesis."},
                {"role": "assistant", "content": "Structured payload: thesis verdict=Neutral"},
            ],
            "reports": {"company": "prior company report"},
        }
    )

    assert result["intent_path"] == ["classify", "synthesize", "narrate"]
    assert result["intent"] == "followup"
    assert "supervisor_iterations" not in result
    assert tools["news"].call_count == 0
    assert tools["company"].call_count == 0
    assert tools["technical"].call_count == 0
    assert tools["fundamental"].call_count == 0


def test_named_lens_with_ticker_uses_focused_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_intent(monkeypatch, "news")
    _patch_llm(
        monkeypatch,
        _ExplorationLLM([ExplorationDecision(action="news", rationale="Should not run.")]),
    )
    tools = _tools()

    result = build_graph(tools).invoke(
        {"ticker": "AAPL", "question": "Drill into the news angle for AAPL."}
    )

    assert result["intent_path"] == ["classify", "plan", "gather", "synthesize", "narrate"]
    assert result["intent"] == "news"
    assert result["plan"] == ["company", "news"]
    assert "supervisor_iterations" not in result
    assert tools["company"].call_count == 1
    assert tools["news"].call_count == 1
    assert tools["technical"].call_count == 0
    assert tools["fundamental"].call_count == 0


def test_named_lens_watch_prompt_uses_focused_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_intent(monkeypatch, "technical")
    _patch_llm(
        monkeypatch,
        _ExplorationLLM([ExplorationDecision(action="technical", rationale="Should not run.")]),
    )
    tools = _tools()

    result = build_graph(tools).invoke(
        {"ticker": "NVDA", "question": "What should I watch technically on NVDA?"}
    )

    assert result["intent_path"] == ["classify", "plan", "gather", "synthesize", "narrate"]
    assert result["intent"] == "technical"
    assert result["plan"] == ["company", "technical"]
    assert "supervisor_iterations" not in result
    assert tools["company"].call_count == 1
    assert tools["technical"].call_count == 1
    assert tools["news"].call_count == 0
    assert tools["fundamental"].call_count == 0


def test_exploration_iteration_cap_finishes_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_intent(monkeypatch, "thesis")
    _patch_llm(
        monkeypatch,
        _ExplorationLLM(
            [
                ExplorationDecision(action="company", rationale="Context first."),
                ExplorationDecision(action="technical", rationale="Check the setup."),
                ExplorationDecision(action="fundamental", rationale="Check valuation."),
                ExplorationDecision(action="news", rationale="Should not be reached."),
            ]
        ),
    )
    tools = _tools()

    result = build_graph(tools).invoke({"ticker": "AAPL", "question": "What stands out on AAPL?"})

    assert result["plan"] == ["company", "technical", "fundamental"]
    assert result["supervisor_iterations"] == 3
    assert tools["news"].call_count == 0


def test_exploration_failure_falls_back_to_all_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_intent(monkeypatch, "thesis")
    _patch_llm(monkeypatch, _ExplorationLLM(decision_error=RuntimeError("planner down")))
    tools = _tools()

    result = build_graph(tools).invoke(
        {"ticker": "AAPL", "question": "What's interesting about AAPL this week?"}
    )

    assert result["intent_path"] == ["classify", "explore_supervisor", "synthesize", "narrate"]
    assert result["intent"] == "thesis"
    assert result["plan"] == list(REPORT_TOOLS)
    assert set(result["reports"]) == set(REPORT_TOOLS)
    assert result["supervisor_iterations"] == 0
