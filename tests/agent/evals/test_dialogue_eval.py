"""Tests for the QNT-214 dialogue eval runner."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from agent.evals import dialogue_eval
from agent.evals.dialogue_eval import (
    DIALOGUE_AXES,
    DialogueFixture,
    DialogueOutcome,
    aggregate,
    append_dialogue_history,
    contamination_warning,
    gate_passed,
    load_dialogue_scores,
    load_dialogues,
    paired_delta_gate,
    precheck_environment,
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
            "answer": self.thesis,
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
    rendered = dialogue_eval._render_payload({"answer": thesis})
    assert "Rendered company line." in rendered


# ─── QNT-218: determinism, error bars, paired gate, resilience ───────────────


def _outcome(
    judge: DialogueJudgeScore | None, *, fid: str = "f", elapsed_ms: int = 25_000
) -> DialogueOutcome:
    fixture = DialogueFixture(
        id=fid, ticker="AAPL", turns=("q",), expected_signals=("analyst_likeness",), anchors={}
    )
    return DialogueOutcome(
        fixture=fixture,
        transcript="user: q",
        narrative="a",
        structured_payload="",
        numeric_support=dialogue_eval.check_hallucination("", []),
        judge_score=judge,
        trace_id=None,
        elapsed_ms=elapsed_ms,
    )


def test_run_fixture_pins_temp_during_invoke_and_resets(monkeypatch: Any) -> None:
    """The agent-under-test is temp-pinned to 0 while invoking, then restored."""
    from agent import llm as llm_mod

    seen: list[float | None] = []

    class _RecordingGraph:
        def invoke(
            self, state: dict[str, Any], config: RunnableConfig | None = None
        ) -> dict[str, Any]:
            del config
            seen.append(llm_mod._TEMPERATURE_OVERRIDE)
            return {
                "ticker": "AAPL",
                "question": state["question"],
                "narrative": "ok",
                "reports": {},
            }

    monkeypatch.setattr(dialogue_eval, "build_graph", lambda *a, **kw: _RecordingGraph())
    monkeypatch.setattr(dialogue_eval, "default_report_tools", lambda: {})
    monkeypatch.setattr(dialogue_eval, "judge_score_fn", lambda **_kw: _judge(0.8))

    fixture = DialogueFixture(
        id="pin", ticker="AAPL", turns=("q",), expected_signals=("voice_match",), anchors={}
    )
    run_fixture(fixture)

    assert seen == [dialogue_eval.EVAL_AGENT_TEMPERATURE]  # pinned to 0.0 during invoke
    # reset afterwards so a later non-eval get_llm() is unaffected
    assert llm_mod._TEMPERATURE_OVERRIDE is None


def test_aggregate_reports_mean_and_standard_error() -> None:
    agg = aggregate([_outcome(_judge(0.6)), _outcome(_judge(0.8))])
    assert agg is not None
    assert agg.n == 2
    for axis in DIALOGUE_AXES:
        assert agg.axis_mean[axis] == pytest.approx(0.7)
        assert agg.axis_se[axis] == pytest.approx(0.1)  # stdev([.6,.8])/sqrt(2)
    assert agg.composite_mean == pytest.approx(0.7)
    assert agg.composite_se == pytest.approx(0.1)


def test_aggregate_se_is_zero_for_single_fixture() -> None:
    agg = aggregate([_outcome(_judge(0.7))])
    assert agg is not None and agg.axis_se["analyst_likeness"] == 0.0


def test_append_dialogue_history_writes_summary_row(tmp_path: Path) -> None:
    history = tmp_path / "history.csv"
    append_dialogue_history(
        [_outcome(_judge(0.6), fid="a"), _outcome(_judge(0.8), fid="b")],
        run_id="r1",
        history_path=history,
    )
    rows = list(csv.DictReader(history.open()))
    summary = [r for r in rows if r["eval_type"] == "dialogue_summary"]
    assert len(summary) == 1
    assert summary[0]["dialogue_n"] == "2"
    assert summary[0]["analyst_likeness"] == "0.7"
    assert summary[0]["analyst_likeness_se"] == "0.1"


def test_load_dialogue_scores_skips_summary_row(tmp_path: Path) -> None:
    history = tmp_path / "history.csv"
    append_dialogue_history(
        [_outcome(_judge(0.6), fid="a"), _outcome(_judge(0.8), fid="b")],
        run_id="r1",
        history_path=history,
    )
    scores = load_dialogue_scores("r1", history_path=history)
    assert set(scores) == {"a", "b"}  # only per-fixture rows, not "ALL"
    assert scores["a"]["analyst_likeness"] == pytest.approx(0.6)


def test_paired_gate_passes_on_uniform_lift_and_holds_guardrail() -> None:
    fixtures = ("f1", "f2", "f3")
    baseline = {
        f: {"analyst_likeness": 0.5, "exploration_quality": 0.5, "non_hallucination": 1.0}
        for f in fixtures
    }
    candidate = {
        f: {"analyst_likeness": 0.7, "exploration_quality": 0.7, "non_hallucination": 1.0}
        for f in fixtures
    }
    results = paired_delta_gate(baseline, candidate)
    assert gate_passed(results)
    by_axis = {r.axis: r for r in results}
    assert by_axis["analyst_likeness"].kind == "lift" and by_axis["analyst_likeness"].passed
    assert by_axis["non_hallucination"].kind == "guardrail" and by_axis["non_hallucination"].passed


def test_paired_gate_fails_when_lift_is_inside_the_noise() -> None:
    # deltas of +0.3 and -0.3 -> mean 0, large SE -> not a significant lift
    baseline = {
        "f1": {"analyst_likeness": 0.5, "exploration_quality": 0.5, "non_hallucination": 1.0},
        "f2": {"analyst_likeness": 0.5, "exploration_quality": 0.5, "non_hallucination": 1.0},
    }
    candidate = {
        "f1": {"analyst_likeness": 0.8, "exploration_quality": 0.8, "non_hallucination": 1.0},
        "f2": {"analyst_likeness": 0.2, "exploration_quality": 0.2, "non_hallucination": 1.0},
    }
    results = paired_delta_gate(baseline, candidate)
    assert not gate_passed(results)


def test_paired_gate_fails_on_guardrail_regression() -> None:
    baseline = {
        f: {"analyst_likeness": 0.5, "exploration_quality": 0.5, "non_hallucination": 1.0}
        for f in ("f1", "f2")
    }
    candidate = {
        f: {"analyst_likeness": 0.9, "exploration_quality": 0.9, "non_hallucination": 0.7}
        for f in ("f1", "f2")
    }
    results = paired_delta_gate(baseline, candidate)
    by_axis = {r.axis: r for r in results}
    assert not by_axis["non_hallucination"].passed
    assert not gate_passed(results)


def test_paired_gate_catches_regression_elsewhere() -> None:
    # QNT-215 "no regression elsewhere": helpfulness/voice_match are guardrails.
    axes = (
        "analyst_likeness",
        "exploration_quality",
        "non_hallucination",
        "helpfulness",
        "voice_match",
    )
    baseline: dict[str, dict[str, float]] = {f: {a: 0.6 for a in axes} for f in ("f1", "f2")}
    candidate: dict[str, dict[str, float]] = {
        f: {a: (0.3 if a == "voice_match" else 0.9) for a in axes} for f in ("f1", "f2")
    }
    results = paired_delta_gate(baseline, candidate)
    by_axis = {r.axis: r for r in results}
    assert by_axis["voice_match"].kind == "guardrail"
    assert not by_axis["voice_match"].passed  # regressed despite the lifts
    assert not gate_passed(results)


def test_paired_gate_lift_needs_two_fixtures() -> None:
    # a single shared fixture cannot establish significance -> lift must not pass
    baseline = {"f1": {"analyst_likeness": 0.5, "exploration_quality": 0.5}}
    candidate = {"f1": {"analyst_likeness": 0.9, "exploration_quality": 0.9}}
    results = paired_delta_gate(baseline, candidate)
    by_axis = {r.axis: r for r in results}
    assert by_axis["analyst_likeness"].n == 1
    assert not by_axis["analyst_likeness"].passed
    assert not gate_passed(results)


def test_precheck_raises_when_a_service_is_unreachable(monkeypatch: Any) -> None:
    def _boom(url: str, timeout: float = 5.0) -> Any:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(dialogue_eval.httpx, "get", _boom)
    with pytest.raises(RuntimeError, match="precheck failed"):
        precheck_environment()


def test_precheck_passes_when_services_reachable(monkeypatch: Any) -> None:
    monkeypatch.setattr(dialogue_eval.httpx, "get", lambda url, timeout=5.0: MagicMock())
    precheck_environment()  # no raise


def test_contamination_warning_flags_high_latency() -> None:
    warning = contamination_warning([_outcome(_judge(0.8), elapsed_ms=55_000)])
    assert warning is not None and "CONTAMINATED" in warning


def test_contamination_warning_flags_judge_failure() -> None:
    warning = contamination_warning([_outcome(None, elapsed_ms=25_000)])
    assert warning is not None and "judge failure" in warning


def test_contamination_warning_clean_run_is_none() -> None:
    assert contamination_warning([_outcome(_judge(0.8), elapsed_ms=25_000)]) is None
