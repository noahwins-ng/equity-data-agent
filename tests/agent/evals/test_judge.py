"""Unit tests for the per-axis judge (QNT-191).

Tests use a mock LLM injected via the ``llm`` parameter of ``score()`` so no
live LiteLLM proxy is needed. Each test verifies the plumbing from the
``score()`` call through ``with_structured_output`` to the returned
``JudgeScore`` object.

The analyst_logic axis tests (AC #6) confirm that the judge schema accepts a
low score for the overbought-in-bull-bullet case (B-1 rule) and a high score
for the clean case — verifying the round-trip through the structured-output
chain, not the LLM's actual reasoning.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agent.evals.judge import JudgeScore, score


def _make_llm(return_value: JudgeScore | None) -> MagicMock:
    """Build a mock LLM whose with_structured_output chain returns ``return_value``."""
    mock_structured = MagicMock()
    mock_structured.invoke.return_value = return_value
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_structured
    return mock_llm


class TestJudgeScore:
    def test_composite_is_average_of_four_axes(self) -> None:
        js = JudgeScore(faithfulness=8, structure=6, correctness=7, analyst_logic=5)
        assert js.composite == round((8 + 6 + 7 + 5) / 4)

    def test_composite_rounds_correctly(self) -> None:
        js = JudgeScore(faithfulness=7, structure=7, correctness=7, analyst_logic=8)
        # (7+7+7+8)/4 = 7.25 → rounds to 7
        assert js.composite == 7

    def test_zero_scores_composite(self) -> None:
        js = JudgeScore(faithfulness=0, structure=0, correctness=0, analyst_logic=0)
        assert js.composite == 0

    def test_perfect_scores_composite(self) -> None:
        js = JudgeScore(faithfulness=10, structure=10, correctness=10, analyst_logic=10)
        assert js.composite == 10


class TestScoreFunction:
    def test_returns_judge_score_on_success(self) -> None:
        js = JudgeScore(faithfulness=8, structure=7, correctness=9, analyst_logic=6)
        result = score("Is NVDA a buy?", "generated thesis", "reference thesis", llm=_make_llm(js))
        assert result == js

    def test_returns_none_on_llm_exception(self) -> None:
        mock_structured = MagicMock()
        mock_structured.invoke.side_effect = RuntimeError("LLM unavailable")
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        result = score("q", "gen", "ref", llm=mock_llm)
        assert result is None

    def test_uses_with_structured_output_on_judge_score_schema(self) -> None:
        js = JudgeScore(faithfulness=5, structure=5, correctness=5, analyst_logic=5)
        mock_llm = _make_llm(js)
        score("q", "gen", "ref", llm=mock_llm)
        mock_llm.with_structured_output.assert_called_once_with(JudgeScore)

    def test_returns_none_on_unexpected_shape(self) -> None:
        mock_structured = MagicMock()
        mock_structured.invoke.return_value = "not a JudgeScore"
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        result = score("q", "gen", "ref", llm=mock_llm)
        assert result is None


class TestAnalystLogicAxis:
    """AC #6: analyst_logic scores for overbought-in-bull-bullet (B-1 rule)."""

    def test_overbought_rsi_in_bull_bullet_receives_low_analyst_logic(self) -> None:
        """A thesis placing RSI >= 70 as a bull bullet violates B-1; the judge
        must return analyst_logic <= 3 for such a response."""
        low_score = JudgeScore(faithfulness=7, structure=7, correctness=7, analyst_logic=2)
        overbought_thesis = (
            "## Bull case\n"
            "- RSI at 78 — overbought momentum confirms bullish continuation (source: technical)\n"
            "## Bear case\n"
            "- Multiple compression risk.\n"
        )
        result = score(
            "Is NVDA a buy?",
            overbought_thesis,
            "Reference thesis with correct analyst logic.",
            llm=_make_llm(low_score),
        )
        assert result is not None
        assert result.analyst_logic <= 3

    def test_clean_bull_bullets_receive_high_analyst_logic(self) -> None:
        """A thesis that moves the overbought RSI to the bear case (or omits it
        from the bull bullets) must receive analyst_logic >= 7."""
        high_score = JudgeScore(faithfulness=8, structure=8, correctness=8, analyst_logic=9)
        clean_thesis = (
            "## Bull case\n"
            "- Momentum building on above-average volume (source: technical)\n"
            "## Bear case\n"
            "- RSI at 78 — overbought; pullback risk elevated (source: technical)\n"
        )
        result = score(
            "Is NVDA a buy?",
            clean_thesis,
            "Reference thesis with correct analyst logic.",
            llm=_make_llm(high_score),
        )
        assert result is not None
        assert result.analyst_logic >= 7
