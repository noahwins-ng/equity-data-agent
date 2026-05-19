"""Regression tests: every .to_markdown() shape appends the disclaimer (QNT-195)."""

from __future__ import annotations

from agent.comparison import ComparisonAnswer, ComparisonSection, ComparisonValue
from agent.conversational import ConversationalAnswer
from agent.disclaimer import DISCLAIMER
from agent.focused import FocusedAnalysis, FocusedValue
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis

_NEEDLE = "not investment advice"


def test_thesis_to_markdown_contains_disclaimer() -> None:
    thesis = Thesis(
        setup="NVDA is at the centre of the AI capex cycle.",
        bull_case=["RSI is 62 (source: technical)."],
        bear_case=["Customer concentration risk (source: news)."],
        verdict_stance="constructive",
        verdict_action="Close above SMA-50 supports adding here.",
    )
    assert _NEEDLE in thesis.to_markdown()


def test_quick_fact_to_markdown_contains_disclaimer() -> None:
    qf = QuickFactAnswer(answer="RSI is 62.", cited_value="62", source="technical")
    assert _NEEDLE in qf.to_markdown()


def test_comparison_to_markdown_contains_disclaimer() -> None:
    ca = ComparisonAnswer(
        sections=[
            ComparisonSection(
                ticker="NVDA",
                summary="Momentum leader.",
                key_values=[ComparisonValue(label="RSI", value="62", source="technical")],
            ),
            ComparisonSection(ticker="AAPL", summary="Defensive play.", key_values=[]),
        ],
        differences="NVDA trades at a richer multiple than AAPL.",
    )
    assert _NEEDLE in ca.to_markdown()


def test_focused_to_markdown_contains_disclaimer() -> None:
    fa = FocusedAnalysis(
        focus="technical",
        summary="Momentum is positive.",
        key_points=["RSI above 50 is constructive."],
        cited_values=[FocusedValue(label="RSI", value="62", source="technical")],
    )
    assert _NEEDLE in fa.to_markdown()


def test_conversational_to_markdown_contains_disclaimer() -> None:
    ca = ConversationalAnswer(answer="I can help with that.", suggestions=["What is NVDA's RSI?"])
    assert _NEEDLE in ca.to_markdown()


def test_disclaimer_constant_matches_expectation() -> None:
    assert _NEEDLE in DISCLAIMER
    assert "Informational only" in DISCLAIMER
