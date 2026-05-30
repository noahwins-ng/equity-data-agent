"""Tests for the QNT-214 dialogue eval runner."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from agent.evals import dialogue_eval
from agent.evals.dialogue_eval import (
    DIALOGUE_AXES,
    DialogueFixture,
    DialogueOutcome,
    append_dialogue_history,
    load_dialogues,
    run_fixture,
)
from agent.evals.dialogue_judge import DialogueAxisScore, DialogueJudgeScore
from agent.thesis import Thesis
from langchain_core.runnables import RunnableConfig

from .._thesis_factory import make_thesis


def _axis(value: float, rationale: str = "good") -> DialogueAxisScore:
    return DialogueAxisScore(score=value, rationale=rationale)


def _judge(value: float = 0.8) -> DialogueJudgeScore:
    return DialogueJudgeScore(
        analyst_likeness=_axis(value, "sounds like an analyst"),
        helpfulness=_axis(value, "answers the ask"),
        non_hallucination=_axis(value, "numbers are grounded"),
        exploration_quality=_axis(value, "asks back when needed"),
        voice_match=_axis(value, "matches ADR voice"),
    )


class _FakeGraph:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.thesis = make_thesis(
            company_summary="AAPL has a balanced setup (source: company).",
            verdict="Neutral",
            verdict_rationale="Inline valuation and Uptrend trend keep it balanced.",
        )

    def invoke(self, state: dict[str, Any], config: RunnableConfig | None = None) -> dict[str, Any]:
        del config
        self.calls.append(state)
        messages = []
        for call in self.calls:
            messages.append({"role": "user", "content": call["question"]})
            messages.append({"role": "assistant", "content": "Balanced AAPL read."})
        return {
            "ticker": "AAPL",
            "question": state["question"],
            "intent": "followup" if len(self.calls) > 1 else "thesis",
            "messages": messages,
            "narrative": "The read stays balanced.",
            "thesis": self.thesis,
            "reports": {"company": "AAPL has a balanced setup"},
        }


def test_dialogue_yaml_has_required_coverage() -> None:
    fixtures = load_dialogues()
    assert len(fixtures) >= 12
    covered = {axis for fixture in fixtures for axis in fixture.expected_signals}
    assert covered == set(DIALOGUE_AXES)


def test_run_fixture_replays_turns_and_scores(monkeypatch: Any) -> None:
    graph = _FakeGraph()
    monkeypatch.setattr(dialogue_eval, "build_graph", lambda *a, **kw: graph)
    monkeypatch.setattr(dialogue_eval, "default_report_tools", lambda: {})
    monkeypatch.setattr(dialogue_eval, "judge_score_fn", lambda **_kw: _judge(0.75))

    fixture = DialogueFixture(
        id="aapl-test",
        ticker="AAPL",
        turns=("Give me an AAPL thesis.", "why?"),
        expected_signals=("analyst_likeness",),
        anchors={},
    )
    outcome = run_fixture(fixture)

    assert [call["question"] for call in graph.calls] == list(fixture.turns)
    assert outcome.judge_score is not None
    assert outcome.judge_score.composite == 0.75
    assert outcome.numeric_support.ok
    assert "user: why?" in outcome.transcript


def test_deterministic_numeric_gate_overrides_judge(monkeypatch: Any) -> None:
    graph = _FakeGraph()
    original_invoke = graph.invoke

    def invoke_with_bad_number(
        state: dict[str, Any], config: RunnableConfig | None = None
    ) -> dict[str, Any]:
        result = original_invoke(state, config)
        result["narrative"] = "The upside is 999%."
        result["reports"] = {"company": "AAPL has a balanced setup"}
        return result

    graph.invoke = MagicMock(side_effect=invoke_with_bad_number)  # type: ignore[method-assign]
    monkeypatch.setattr(dialogue_eval, "build_graph", lambda *a, **kw: graph)
    monkeypatch.setattr(dialogue_eval, "default_report_tools", lambda: {})
    monkeypatch.setattr(dialogue_eval, "judge_score_fn", lambda **_kw: _judge(0.9))

    fixture = DialogueFixture(
        id="bad-number",
        ticker="AAPL",
        turns=("Give me AAPL.",),
        expected_signals=("non_hallucination",),
        anchors={},
    )
    outcome = run_fixture(fixture)

    assert not outcome.numeric_support.ok
    assert outcome.judge_score is not None
    assert outcome.judge_score.non_hallucination.score == 0.0
    assert "Deterministic numeric checker failed" in outcome.judge_score.non_hallucination.rationale


def test_append_dialogue_history_writes_additive_columns(tmp_path: Path) -> None:
    fixture = DialogueFixture(
        id="history-test",
        ticker="AAPL",
        turns=("q1", "q2"),
        expected_signals=("voice_match",),
        anchors={},
    )
    outcome = DialogueOutcome(
        fixture=fixture,
        transcript="user: q1\nassistant: a1",
        narrative="a2",
        structured_payload="",
        numeric_support=dialogue_eval.check_hallucination("", []),
        judge_score=_judge(0.6),
        trace_id=None,
        elapsed_ms=123,
    )
    history = tmp_path / "history.csv"
    append_dialogue_history([outcome], run_id="r-dialogue", history_path=history)

    rows = list(csv.DictReader(history.open()))
    assert rows[0]["eval_type"] == "dialogue"
    assert rows[0]["dialogue_fixture_id"] == "history-test"
    assert rows[0]["dialogue_turns"] == "2"
    assert rows[0]["analyst_likeness"] == "0.6"
    assert rows[0]["dialogue_composite"] == "0.6"


def test_render_payload_prefers_structured_markdown() -> None:
    thesis: Thesis = make_thesis(company_summary="Rendered company line.")
    rendered = dialogue_eval._render_payload({"thesis": thesis})
    assert "Rendered company line." in rendered
