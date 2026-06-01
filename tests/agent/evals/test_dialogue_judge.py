"""Tests for the QNT-214 dialogue judge plumbing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from agent.evals.dialogue_judge import (
    JUDGE_MODEL_ALIAS,
    JUDGE_RESOLVED_MODEL,
    DialogueAxisScore,
    DialogueJudgeScore,
    build_judge_llm,
    score,
)


def _axis(value: float, rationale: str = "clear signal") -> DialogueAxisScore:
    return DialogueAxisScore(score=value, rationale=rationale)


def _judge(value: float) -> DialogueJudgeScore:
    return DialogueJudgeScore(
        analyst_likeness=_axis(value),
        helpfulness=_axis(value),
        non_hallucination=_axis(value),
        exploration_quality=_axis(value),
        voice_match=_axis(value),
    )


def _make_llm(return_value: DialogueJudgeScore | None) -> MagicMock:
    structured = MagicMock()
    structured.invoke.return_value = return_value
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


def test_composite_averages_five_axes() -> None:
    js = DialogueJudgeScore(
        analyst_likeness=_axis(1.0),
        helpfulness=_axis(0.5),
        non_hallucination=_axis(1.0),
        exploration_quality=_axis(0.0),
        voice_match=_axis(0.5),
    )
    assert js.composite == 0.6


def test_score_returns_high_synthetic_pass() -> None:
    result = score(
        fixture_id="pass",
        transcript="user: thesis on AAPL\nassistant: The setup is balanced.",
        narrative="The setup is balanced.",
        structured_payload="verdict: Neutral",
        expected_signals=("analyst_likeness",),
        numeric_support="clean",
        llm=_make_llm(_judge(0.95)),
    )
    assert result is not None
    assert result.analyst_likeness.score >= 0.9
    assert result.helpfulness.score >= 0.9
    assert result.non_hallucination.score >= 0.9
    assert result.exploration_quality.score >= 0.9
    assert result.voice_match.score >= 0.9


def test_score_returns_low_synthetic_fail() -> None:
    result = score(
        fixture_id="fail",
        transcript="user: compare AAPL with them\nassistant: AAPL beats NVDA by 99%.",
        narrative="AAPL beats NVDA by 99%.",
        structured_payload="",
        expected_signals=("exploration_quality", "non_hallucination"),
        numeric_support="unsupported: 99",
        llm=_make_llm(_judge(0.1)),
    )
    assert result is not None
    assert result.analyst_likeness.score <= 0.2
    assert result.helpfulness.score <= 0.2
    assert result.non_hallucination.score <= 0.2
    assert result.exploration_quality.score <= 0.2
    assert result.voice_match.score <= 0.2


def test_uses_structured_output_schema() -> None:
    llm = _make_llm(_judge(0.7))
    score(
        fixture_id="schema",
        transcript="user: hi",
        narrative="I can help with the covered equities.",
        structured_payload="",
        expected_signals=("helpfulness",),
        numeric_support="clean",
        llm=llm,
    )
    llm.with_structured_output.assert_called_once_with(DialogueJudgeScore)


def test_returns_none_on_judge_error() -> None:
    structured = MagicMock()
    structured.invoke.side_effect = RuntimeError("judge unavailable")
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    result = score(
        fixture_id="error",
        transcript="user: hi",
        narrative="hello",
        structured_payload="",
        expected_signals=("voice_match",),
        numeric_support="clean",
        llm=llm,
    )
    assert result is None


def test_dedicated_judge_alias_rejects_self_judging() -> None:
    with pytest.raises(ValueError, match="must differ"):
        build_judge_llm(agent_model_alias=JUDGE_MODEL_ALIAS)


def test_dialogue_judge_uses_cerebras_gptoss120b_alias() -> None:
    assert JUDGE_MODEL_ALIAS == "equity-agent/bench-cerebras-gptoss120b"
    assert JUDGE_RESOLVED_MODEL == "cerebras/gpt-oss-120b"
