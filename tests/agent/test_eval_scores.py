"""Unit tests for ``agent.eval_scores`` (QNT-182).

Pure tests against ``compute_scores`` — no Langfuse client, no SSE handler.
Integration coverage of the trace push lives in ``tests/api/test_agent_chat.py``.
"""

from __future__ import annotations

from agent.comparison import ComparisonAnswer, ComparisonSection, ComparisonValue
from agent.conversational import ConversationalAnswer
from agent.eval_scores import compute_scores
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis


def _thesis(setup: str = "Setup paragraph.", *, citations: list[str] | None = None) -> Thesis:
    return Thesis(
        setup=setup,
        bull_case=citations or ["RSI 65 (source: technical)"],
        bear_case=["Multiple compression (source: fundamental)"],
        verdict_stance="constructive",
        verdict_action="Trim above SMA50 (source: technical).",
    )


def test_compute_scores_clean_thesis_passes_both() -> None:
    state = {
        "thesis": _thesis(),
        "plan": ["technical", "fundamental"],
        "reports": {
            "technical": "RSI 65 reading. SMA50 cited.",
            "fundamental": "Multiple compression discussion.",
        },
    }
    hallucination, missing = compute_scores(state)
    assert hallucination.ok is True
    assert missing == set()


def test_compute_scores_flags_fabricated_number() -> None:
    """A thesis number not in any report fails the hallucination check."""
    state = {
        "thesis": _thesis(citations=["RSI 99 (source: technical)"]),
        "plan": ["technical", "fundamental"],
        "reports": {
            "technical": "RSI 65 reading.",
            "fundamental": "Multiple compression.",
        },
    }
    hallucination, missing = compute_scores(state)
    assert hallucination.ok is False
    assert "99" in hallucination.reason()
    assert missing == set()  # tools were gathered, only the number is the issue


def test_compute_scores_flags_missing_planned_tool() -> None:
    """Plan adherence fails when gather skipped a planned tool."""
    state = {
        "thesis": _thesis(),
        "plan": ["technical", "fundamental", "news"],
        "reports": {
            "technical": "RSI 65 reading. SMA50 cited.",
            "fundamental": "Multiple compression.",
        },
    }
    _, missing = compute_scores(state)
    assert missing == {"news"}


def test_compute_scores_empty_plan_satisfies_adherence() -> None:
    """Conversational redirects produce empty plans; adherence is trivially OK."""
    state = {
        "conversational": ConversationalAnswer(
            answer="I focus on equity research questions.",
            suggestions=["Try a thesis question."],
        ),
        "plan": [],
        "reports": {},
    }
    _, missing = compute_scores(state)
    assert missing == set()


def test_compute_scores_renders_quick_fact_shape() -> None:
    """Quick-fact answers route through to_markdown for the hallucination check."""
    state = {
        "quick_fact": QuickFactAnswer(
            answer="The RSI is 65.",
            cited_value="65",
            source="technical",
        ),
        "plan": ["technical"],
        "reports": {"technical": "RSI 65 reading."},
    }
    hallucination, missing = compute_scores(state)
    assert hallucination.ok is True
    assert missing == set()


def test_compute_scores_renders_comparison_shape() -> None:
    """Comparison answers flatten reports_by_ticker into the report corpus."""
    state = {
        "comparison": ComparisonAnswer(
            sections=[
                ComparisonSection(
                    ticker="NVDA",
                    summary="P/E 50 currently (source: fundamental).",
                    key_values=[
                        ComparisonValue(label="P/E", value="50", source="fundamental"),
                    ],
                ),
                ComparisonSection(
                    ticker="AAPL",
                    summary="P/E 30 currently (source: fundamental).",
                    key_values=[
                        ComparisonValue(label="P/E", value="30", source="fundamental"),
                    ],
                ),
            ],
            differences="NVDA trades at a richer multiple.",
        ),
        "plan": ["fundamental"],
        "reports_by_ticker": {
            "NVDA": {"fundamental": "P/E 50 currently."},
            "AAPL": {"fundamental": "P/E 30 currently."},
        },
    }
    hallucination, missing = compute_scores(state)
    assert hallucination.ok is True
    assert missing == set()


def test_compute_scores_comparison_partial_gather_flags_missing() -> None:
    """Comparison runs use per-ticker (intersection) adherence: a planned
    tool that one ticker fetched but the other didn't is still flagged."""
    state = {
        "comparison": ComparisonAnswer(
            sections=[
                ComparisonSection(
                    ticker="NVDA",
                    summary="P/E 50 (source: fundamental).",
                    key_values=[ComparisonValue(label="P/E", value="50", source="fundamental")],
                ),
                ComparisonSection(
                    ticker="AAPL",
                    summary="No fundamental data available.",
                    key_values=[],
                ),
            ],
            differences="NVDA has more data.",
        ),
        "plan": ["fundamental"],
        "reports_by_ticker": {
            "NVDA": {"fundamental": "P/E 50 currently."},
            "AAPL": {},  # gather failed for AAPL
        },
    }
    _, missing = compute_scores(state)
    assert missing == {"fundamental"}


def test_compute_scores_empty_state_returns_clean() -> None:
    """An empty state (graph crashed pre-synthesize) renders no answer; the
    hallucination check sees an empty thesis and trivially passes (nothing
    to fabricate). plan_adherence is trivially OK because plan is empty."""
    hallucination, missing = compute_scores({})
    assert hallucination.ok is True
    assert missing == set()
