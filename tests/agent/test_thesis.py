"""Tests for the structured ``Thesis`` model + markdown renderer (QNT-133).

The synthesize node returns a ``Thesis`` (Pydantic) instance via
``with_structured_output``; the CLI / eval harness re-render it to markdown.
These tests freeze the schema shape, the four-section output contract, the
asymmetry-allowed semantics, and the verdict-stance closed set.
"""

from __future__ import annotations

import pytest
from agent.thesis import Thesis
from pydantic import ValidationError


def _full_thesis() -> Thesis:
    return Thesis(
        setup=(
            "NVDA is at the centre of the AI capex cycle; the question is "
            "whether momentum justifies the multiple (source: technical, "
            "fundamental)."
        ),
        bull_case=[
            "RSI is 62 with SMA50 sloping up (source: technical).",
            "P/E 28 vs. 30%+ revenue growth (source: fundamental).",
            "Recent coverage is constructive (source: news).",
        ],
        bear_case=[
            "Concentration risk in hyperscaler customers (source: news).",
        ],
        verdict_stance="constructive",
        verdict_action=("Hold core position; trim above SMA50 + RSI > 75 (source: technical)."),
    )


# ───────────────────────── Schema shape ──────────────────────────────────────


def test_field_descriptions_contain_no_multi_digit_literals() -> None:
    """QNT-136 regression guard, schema side: ``with_structured_output``
    injects every field's ``description`` into the JSON schema the LLM sees
    when producing structured output. A literal multi-digit number in a
    description bleeds into the model's output the same way the SYSTEM_PROMPT
    body would. The original QNT-133 ``verdict_action`` description carried
    "RSI > 75" as an example and the model parroted "75" into 3/16 theses
    where the technical report did not contain 75.

    Pin all five field descriptions; a future "concrete example" edit has
    to use words or report-relative phrases ("overbought RSI threshold"),
    not literal digits."""
    import re

    offenders: list[tuple[str, list[str]]] = []
    for name, info in Thesis.model_fields.items():
        desc = info.description or ""
        multi = re.findall(r"(?<!\w)\d{2,}(?!\w)", desc)
        if multi:
            offenders.append((name, multi))
    assert offenders == [], (
        "Thesis field descriptions contain literal multi-digit numbers that "
        "will bleed into structured-output theses and trip the hallucination "
        f"check: {offenders}. Use words or report-relative phrases instead."
    )


def test_thesis_has_four_sections() -> None:
    """Schema-level guard: the QNT-133 Thesis model exposes Setup / Bull /
    Bear / Verdict-stance / Verdict-action — and no extra freeform fields
    a future drift could exploit."""
    fields = set(Thesis.model_fields.keys())
    assert fields == {
        "setup",
        "bull_case",
        "bear_case",
        "verdict_stance",
        "verdict_action",
    }


def test_verdict_stance_is_a_closed_set() -> None:
    """Frontend will colour-code on stance; an open string would let the
    model invent shapes the UI doesn't render. Schema must reject anything
    outside the four canonical values."""
    for valid in ("constructive", "cautious", "negative", "mixed"):
        Thesis(
            setup="x",
            bull_case=[],
            bear_case=[],
            verdict_stance=valid,  # pyright: ignore[reportArgumentType]
            verdict_action="x",
        )

    with pytest.raises(ValidationError):
        Thesis(
            setup="x",
            bull_case=[],
            bear_case=[],
            verdict_stance="bullish",  # pyright: ignore[reportArgumentType]
            verdict_action="x",
        )


def test_bull_and_bear_default_to_empty_list() -> None:
    """Asymmetry must be representable in the schema without forcing the
    caller to pass ``[]`` explicitly."""
    t = Thesis(
        setup="x",
        verdict_stance="cautious",
        verdict_action="x",
    )
    assert t.bull_case == []
    assert t.bear_case == []


def test_thesis_round_trips_through_json() -> None:
    """API path: the structured form must serialise to JSON without losing
    fields, so the SSE endpoint can stream it to the frontend verbatim."""
    original = _full_thesis()
    payload = original.model_dump_json()
    restored = Thesis.model_validate_json(payload)
    assert restored == original


# ───────────────────────── Markdown renderer ─────────────────────────────────


def test_to_markdown_contains_all_four_section_headings() -> None:
    """CLI / eval contract: the rendered markdown carries every section
    heading in order, regardless of asymmetry."""
    rendered = _full_thesis().to_markdown()
    for heading in ("## Setup", "## Bull Case", "## Bear Case", "## Verdict"):
        assert heading in rendered, f"missing heading: {heading}"
    indices = [
        rendered.index(heading)
        for heading in ("## Setup", "## Bull Case", "## Bear Case", "## Verdict")
    ]
    assert indices == sorted(indices)


def test_to_markdown_renders_bullets_for_supporting_points() -> None:
    """Each bull/bear point becomes a markdown bullet so downstream renderers
    (mkdocs, GitHub) display it as a list rather than a wall of prose."""
    rendered = _full_thesis().to_markdown()
    assert "- RSI is 62 with SMA50 sloping up (source: technical)." in rendered
    assert "- Concentration risk in hyperscaler customers (source: news)." in rendered


def test_to_markdown_handles_empty_bull_case_gracefully() -> None:
    """Asymmetry: an empty bull_case must not produce a ghost bullet line.
    Renderer falls back to a parenthetical note so the section is still
    visible (the design wants the heading even when empty) without
    fabricating content."""
    t = Thesis(
        setup="x",
        bull_case=[],
        bear_case=["bear point (source: technical)"],
        verdict_stance="negative",
        verdict_action="x",
    )
    rendered = t.to_markdown()
    assert "## Bull Case" in rendered
    assert "no bull case" in rendered.lower()
    # Empty section did not consume the bear bullet.
    assert "- bear point (source: technical)" in rendered


def test_to_markdown_handles_empty_bear_case_gracefully() -> None:
    t = Thesis(
        setup="x",
        bull_case=["bull point (source: technical)"],
        bear_case=[],
        verdict_stance="constructive",
        verdict_action="x",
    )
    rendered = t.to_markdown()
    assert "## Bear Case" in rendered
    assert "no bear case" in rendered.lower()


def test_to_markdown_renders_verdict_stance_visibly() -> None:
    """Stance is the at-a-glance signal — must appear in the rendered text
    (the eval / hallucination scorer reads the markdown form)."""
    rendered = _full_thesis().to_markdown()
    assert "constructive" in rendered.lower()


def test_to_markdown_preserves_verdict_action_text() -> None:
    """Action guidance must reach the rendered output verbatim (modulo
    whitespace stripping) — the QNT-67 hallucination check greps it for
    numeric claims."""
    t = _full_thesis()
    rendered = t.to_markdown()
    assert "Hold core position; trim above SMA50 + RSI > 75 (source: technical)." in rendered
