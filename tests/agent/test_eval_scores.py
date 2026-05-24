"""Unit tests for ``agent.eval_scores`` (QNT-182, reshaped in QNT-208).

Pure tests against ``compute_scores`` — no Langfuse client, no SSE handler.
Integration coverage of the trace push lives in ``tests/api/test_agent_chat.py``.
"""

from __future__ import annotations

from agent.comparison import ComparisonAnswer
from agent.conversational import ConversationalAnswer
from agent.eval_scores import compute_scores
from agent.quick_fact import QuickFactAnswer

from ._thesis_factory import make_comparison_section, make_thesis


def test_compute_scores_clean_thesis_passes_both() -> None:
    state = {
        "thesis": make_thesis(
            supports=["RSI 65 (source: technical)"],
            challenges=["Multiple compression (source: fundamental)"],
        ),
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
        "thesis": make_thesis(supports=["RSI 99 (source: technical)"]),
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
        "thesis": make_thesis(),
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
                make_comparison_section("NVDA", "Premium", "Uptrend"),
                make_comparison_section("AAPL", "Inline", "Sideways"),
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
                make_comparison_section("NVDA", "Premium", "Uptrend"),
                make_comparison_section("AAPL", "Inline", "Sideways"),
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
