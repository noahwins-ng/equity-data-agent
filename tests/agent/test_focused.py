"""Tests for the FocusedAnalysis focus-consistency normalizer (QNT-302, AC2).

The synthesize node forces the LLM through ``FocusedAnalysis`` via
``with_structured_output``. The merged ``FocusedVerdict`` Literal (one enum,
because Groq rejects a two-branch anyOf) lets the schema accept a verdict from
the wrong family for the focus, and the news-only catalyst fields are
structurally valid on any focus. The model_validator normalizes both — nulling
wrong-family verdicts and emptying news fields on non-news focuses — WITHOUT
raising, so a cosmetic mismatch never trips the structured-output retry.
"""

from __future__ import annotations

from agent.focused import FocusedAnalysis


def _focused(focus: str, **overrides: object) -> FocusedAnalysis:
    base: dict[str, object] = {"focus": focus, "summary": "s (source: fundamental)."}
    base.update(overrides)
    return FocusedAnalysis(**base)  # type: ignore[arg-type]


# ─────────────────────────── verdict family ──────────────────────────────────


def test_fundamental_keeps_fundamental_verdict() -> None:
    assert _focused("fundamental", verdict="Premium").verdict == "Premium"


def test_fundamental_nulls_technical_verdict() -> None:
    """focus=fundamental + verdict=Uptrend is cross-family — normalize to None."""
    assert _focused("fundamental", verdict="Uptrend").verdict is None


def test_technical_keeps_technical_verdict() -> None:
    assert _focused("technical", verdict="Downtrend").verdict == "Downtrend"


def test_technical_nulls_fundamental_verdict() -> None:
    assert _focused("technical", verdict="Premium").verdict is None


def test_news_nulls_any_verdict() -> None:
    """News carries no verdict — every FocusedVerdict value is wrong-family."""
    assert _focused("news", verdict="Premium").verdict is None
    assert _focused("news", verdict="Uptrend").verdict is None


# ───────────────────────── news-only fields ──────────────────────────────────


def test_news_fields_cleared_on_technical_focus() -> None:
    f = _focused(
        "technical",
        verdict="Uptrend",
        existing_development="running story",
        positive_catalysts=["good (source: news)"],
        negative_catalysts=["bad (source: news)"],
    )
    assert f.existing_development is None
    assert f.positive_catalysts == []
    assert f.negative_catalysts == []
    assert f.verdict == "Uptrend"  # the valid technical verdict survives


def test_news_fields_cleared_on_fundamental_focus() -> None:
    f = _focused(
        "fundamental",
        existing_development="story",
        positive_catalysts=["p (source: news)"],
    )
    assert f.existing_development is None
    assert f.positive_catalysts == []


def test_news_fields_preserved_on_news_focus() -> None:
    f = _focused(
        "news",
        existing_development="the running story (source: news)",
        positive_catalysts=["upgrade (source: news)"],
        negative_catalysts=["downgrade (source: news)"],
    )
    assert f.existing_development == "the running story (source: news)"
    assert f.positive_catalysts == ["upgrade (source: news)"]
    assert f.negative_catalysts == ["downgrade (source: news)"]


# ───────────────────────── normalize, never raise ────────────────────────────


def test_wrong_family_and_news_fields_together_do_not_raise() -> None:
    """The worst-case payload (wrong-family verdict AND stray news fields on a
    non-news focus) must normalize cleanly, never raise."""
    f = _focused(
        "technical",
        verdict="Discounted",  # wrong family
        existing_development="x",
        positive_catalysts=["y (source: news)"],
    )
    assert f.verdict is None
    assert f.existing_development is None
    assert f.positive_catalysts == []
