"""Tests for the QNT-215 exploration route, rewritten for the QNT-220 (#4)
deterministic policy.

The exploration supervisor no longer asks the LLM for one tool decision at a
time. It runs a deterministic broad-scan policy (min-two complementary lenses,
news-first when the ask is timely) at **zero LLM calls**, then hands the
gathered reports to the normal synthesize/narrate tail. These tests assert the
deterministic plans match the guardrail the old loop encoded (plan-parity) and
that the supervisor never binds an exploration-decision schema (AC5).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from agent import graph as graph_module
from agent.exploration import ExplorationAnswer
from agent.focused import FocusedAnalysis
from agent.graph import REPORT_TOOLS, _deterministic_exploration_plan, build_graph
from agent.thesis import Thesis
from langchain_core.messages import AIMessage

from ._thesis_factory import make_thesis


class _Runnable:
    def __init__(self, result: object | None = None) -> None:
        self.invoke = MagicMock(return_value=result)
        self.with_retry = MagicMock(return_value=self)


class _SynthLLM:
    """Synthesis/narrate LLM stub. Records every schema bound via
    ``with_structured_output`` so a test can assert the supervisor never asks
    for an exploration decision (it makes no LLM call at all)."""

    def __init__(self) -> None:
        self.bound_schemas: list[type] = []
        self.thesis = make_thesis()
        self.focused = FocusedAnalysis(
            focus="news",
            summary="News flow is the relevant angle (source: news).",
            key_points=["Latest headlines anchor the read (source: news)."],
            cited_values=[],
            verdict=None,
            existing_development="The running story is news-led (source: news).",
            positive_catalysts=[],
            negative_catalysts=[],
        )
        self.exploration = ExplorationAnswer(
            headline="Headlines and the chart both stand out (source: news, source: technical).",
            observations=[
                "Fresh headlines are driving the tape (source: news).",
                "Momentum is stretched (source: technical).",
            ],
            cited_values=[],
        )

    def with_structured_output(self, schema: type) -> _Runnable:
        self.bound_schemas.append(schema)
        if schema is Thesis:
            return _Runnable(result=self.thesis)
        if schema is FocusedAnalysis:
            return _Runnable(result=self.focused)
        if schema is ExplorationAnswer:
            return _Runnable(result=self.exploration)
        return _Runnable(result=None)

    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        return iter([AIMessage(content="Exploration narrative.")])

    # plan/quick-fact comma-list path (unused on exploration turns)
    def invoke(self, *_args: Any, **_kwargs: Any) -> Any:
        return AIMessage(content="company")


def _tools() -> dict[str, MagicMock]:
    return {
        "company": MagicMock(return_value="## company\nBusiness profile\n"),
        "technical": MagicMock(return_value="## technical\nChart setup\n"),
        "fundamental": MagicMock(return_value="## fundamental\nValuation\n"),
        "news": MagicMock(return_value="## news\nHeadlines\n"),
    }


def _patch_llm(monkeypatch: pytest.MonkeyPatch, llm: _SynthLLM) -> None:
    monkeypatch.setattr(graph_module, "get_llm", lambda *_args, **_kwargs: llm)


def _patch_intent(monkeypatch: pytest.MonkeyPatch, intent: str = "thesis") -> None:
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda *_args, **_kwargs: (intent, "heuristic", False),
    )


# ─── _deterministic_exploration_plan parity (pure, 0 LLM) ────────────────────


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What's interesting about AAPL this week?", ["news", "technical"]),
        ("What is interesting about AAPL this week?", ["news", "technical"]),
        ("What should I watch on AAPL next week?", ["news", "technical"]),
        ("What stands out on AAPL?", ["company", "news"]),
        ("Anything interesting about AAPL?", ["news", "technical"]),
    ],
)
def test_deterministic_plan_matches_guardrail(question: str, expected: list[str]) -> None:
    assert _deterministic_exploration_plan(question, list(REPORT_TOOLS)) == expected


def test_deterministic_plan_empty_when_no_tools() -> None:
    assert _deterministic_exploration_plan("What's interesting about AAPL?", []) == []


# ─── Graph-level routing ─────────────────────────────────────────────────────


def test_non_exploratory_thesis_keeps_existing_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_intent(monkeypatch, "thesis")
    _patch_llm(monkeypatch, _SynthLLM())
    tools = _tools()

    result = build_graph(tools).invoke({"ticker": "AAPL", "question": "Give me an AAPL thesis."})

    assert result["intent_path"] == ["classify", "plan", "gather", "synthesize", "narrate"]
    assert "supervisor_iterations" not in result


def test_exploration_route_emits_one_stable_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-233: classify commits to exploration before emitting intent."""
    _patch_intent(monkeypatch, "thesis")
    _patch_llm(monkeypatch, _SynthLLM())
    tools = _tools()
    events: list[tuple[str, dict[str, object]]] = []

    build_graph(tools, event_emitter=lambda name, payload: events.append((name, payload))).invoke(
        {"ticker": "AAPL", "question": "What's interesting about AAPL this week?"}
    )

    intent_events = [payload for name, payload in events if name == "intent"]
    assert intent_events == [{"intent": "exploration"}]


def test_news_led_exploration_routes_and_gathers_two_lenses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_intent(monkeypatch, "thesis")
    llm = _SynthLLM()
    _patch_llm(monkeypatch, llm)
    tools = _tools()

    result = build_graph(tools).invoke(
        {"ticker": "AAPL", "question": "What's interesting about AAPL this week?"}
    )

    assert result["intent_path"] == ["classify", "explore_supervisor", "synthesize", "narrate"]
    # QNT-220 follow-up: a broad scan always renders as the dedicated
    # exploration card, regardless of the classifier's original label.
    assert result["intent"] == "exploration"
    assert isinstance(result["exploration"], ExplorationAnswer)
    assert result["plan"] == ["news", "technical"]
    assert result["supervisor_iterations"] == 2
    assert set(result["reports"]) == {"news", "technical"}
    assert tools["news"].call_count == 1
    assert tools["technical"].call_count == 1
    assert tools["company"].call_count == 0
    assert tools["fundamental"].call_count == 0
    # AC5: the supervisor made no LLM call -- the only schema bound this turn is
    # the ExplorationAnswer card from synthesize, never an exploration-decision.
    assert llm.bound_schemas == [ExplorationAnswer]


def test_broad_exploration_still_routes_when_classifier_labels_news(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_intent(monkeypatch, "news")
    _patch_llm(monkeypatch, _SynthLLM())
    tools = _tools()

    result = build_graph(tools).invoke(
        {"ticker": "AAPL", "question": "What is interesting about AAPL this week?"}
    )

    assert result["intent_path"] == ["classify", "explore_supervisor", "synthesize", "narrate"]
    # A "news"-labeled broad scan still resolves to the exploration card, not
    # the single-lens focused-news card.
    assert result["intent"] == "exploration"
    assert result["plan"] == ["news", "technical"]
    assert result["supervisor_iterations"] == 2
    assert tools["news"].call_count == 1
    assert tools["technical"].call_count == 1
    assert tools["company"].call_count == 0
    assert tools["fundamental"].call_count == 0


def test_non_timely_exploration_uses_compact_company_when_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-220 (#8) invariant: exploration is in _COMPACT_COMPANY_INTENTS, so a
    non-news-led [company, news] scan pulls the COMPACT company report, not the
    full one."""
    _patch_intent(monkeypatch, "thesis")
    _patch_llm(monkeypatch, _SynthLLM())
    tools = _tools()
    compact_company = MagicMock(return_value="## company\n(compact)\n")

    result = build_graph(tools, compact_company_tool=compact_company).invoke(
        {"ticker": "AAPL", "question": "What stands out on AAPL?"}
    )

    assert result["intent"] == "exploration"
    assert result["plan"] == ["company", "news"]
    # The compact variant was used for the company slot; the full tool was not.
    assert compact_company.call_count == 1
    assert tools["company"].call_count == 0
    assert tools["news"].call_count == 1


def test_news_led_watch_prompt_gets_complementary_lens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_intent(monkeypatch, "thesis")
    _patch_llm(monkeypatch, _SynthLLM())
    tools = _tools()

    result = build_graph(tools).invoke(
        {"ticker": "AAPL", "question": "What should I watch on AAPL next week?"}
    )

    assert result["plan"] == ["news", "technical"]
    assert tools["news"].call_count == 1
    assert tools["technical"].call_count == 1
    assert tools["company"].call_count == 0
    assert tools["fundamental"].call_count == 0


def test_non_timely_broad_exploration_leads_with_company_and_news(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_intent(monkeypatch, "thesis")
    _patch_llm(monkeypatch, _SynthLLM())
    tools = _tools()

    result = build_graph(tools).invoke({"ticker": "AAPL", "question": "What stands out on AAPL?"})

    assert result["plan"] == ["company", "news"]
    assert result["supervisor_iterations"] == 2
    assert tools["company"].call_count == 1
    assert tools["news"].call_count == 1
    assert tools["technical"].call_count == 0
    assert tools["fundamental"].call_count == 0


def test_warm_thread_news_angle_skips_exploration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_intent(monkeypatch, "followup")
    _patch_llm(monkeypatch, _SynthLLM())
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
    _patch_llm(monkeypatch, _SynthLLM())
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
    _patch_llm(monkeypatch, _SynthLLM())
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
    _patch_llm(monkeypatch, _SynthLLM())
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


def test_exploration_with_empty_tools_falls_back_to_thesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_intent(monkeypatch, "thesis")
    _patch_llm(monkeypatch, _SynthLLM())

    result = build_graph({}).invoke(
        {"ticker": "AAPL", "question": "What's interesting about AAPL this week?"}
    )

    assert result["intent"] == "thesis"
    assert result.get("plan", []) == []
    assert result["supervisor_iterations"] == 0
