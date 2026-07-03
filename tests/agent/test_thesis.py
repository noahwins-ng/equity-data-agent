"""Tests for the structured ``Thesis`` model + markdown renderer (QNT-208).

The synthesize node returns a ``Thesis`` (Pydantic) instance via
``with_structured_output``; the CLI / eval harness re-render it to markdown.
These tests freeze the schema shape, the four-aspect output contract, the
asymmetry-allowed semantics, and the verdict closed set.
"""

from __future__ import annotations

import pytest
from agent.thesis import (
    AspectView,
    Thesis,
    expected_verdict_from_labels,
    normalize_aspect_label,
)
from pydantic import ValidationError

from ._thesis_factory import make_thesis


def _aspect(label: str | None = None, summary: str = "x") -> AspectView:
    return AspectView(label=label, summary=summary, supports=[], challenges=[])  # pyright: ignore[reportArgumentType]


def _full_thesis() -> Thesis:
    return Thesis(
        company=AspectView(
            label=None,
            summary=(
                "NVDA designs GPUs used in most large-scale AI training runs (source: company)."
            ),
            supports=["Hyperscaler demand sustains data-center mix (source: company)."],
            challenges=["Customer concentration in top three hyperscalers (source: company)."],
        ),
        fundamental=AspectView(
            label="Premium",
            summary="Multiple sits in the report's Premium bucket (source: fundamental).",
            supports=["P/E 28 vs. 30%+ revenue growth (source: fundamental)."],
            challenges=["EV/EBITDA Premium relative to 3y range (source: fundamental)."],
        ),
        technical=AspectView(
            label="Uptrend",
            summary="Daily TREND label Uptrend with SMA stack aligned (source: technical).",
            supports=["RSI 62 with SMA50 sloping up (source: technical)."],
            challenges=[],
        ),
        news=AspectView(
            label=None,
            summary="Recent coverage is constructive on AI-platform demand (source: news).",
            supports=["Analyst upgrade citing data-center mix (source: news)."],
            challenges=[],
        ),
        verdict="Overweight",
        verdict_rationale=(
            "Premium multiple and Uptrend trend label agree on demand strength; "
            "verdict Overweight (source: technical, fundamental)."
        ),
    )


# ───────────────────────── Schema shape ──────────────────────────────────────


def test_field_descriptions_contain_no_multi_digit_literals() -> None:
    """QNT-136 regression guard, schema side: ``with_structured_output``
    injects every field's ``description`` into the JSON schema the LLM sees
    when producing structured output. A literal multi-digit number in a
    description bleeds into the model's output the same way the SYSTEM_PROMPT
    body would."""
    import re

    offenders: list[tuple[str, list[str]]] = []
    for name, info in Thesis.model_fields.items():
        desc = info.description or ""
        multi = re.findall(r"(?<!\w)\d{2,}(?!\w)", desc)
        if multi:
            offenders.append((name, multi))
    for name, info in AspectView.model_fields.items():
        desc = info.description or ""
        multi = re.findall(r"(?<!\w)\d{2,}(?!\w)", desc)
        if multi:
            offenders.append((f"AspectView.{name}", multi))
    assert offenders == [], (
        "Schema field descriptions contain literal multi-digit numbers that "
        "will bleed into structured-output theses and trip the hallucination "
        f"check: {offenders}. Use words or report-relative phrases instead."
    )


def test_thesis_has_four_aspects_plus_verdict() -> None:
    """Schema-level guard: the QNT-208 Thesis model exposes four aspect
    blocks + verdict + verdict_rationale — and no v1 fields a future drift
    could exploit."""
    fields = set(Thesis.model_fields.keys())
    assert fields == {
        "company",
        "fundamental",
        "technical",
        "news",
        "verdict",
        "verdict_rationale",
    }


def test_v1_fields_are_gone() -> None:
    """AC9: bull_case / bear_case / verdict_stance / verdict_action no
    longer exist on the schema."""
    fields = set(Thesis.model_fields.keys())
    for legacy in ("bull_case", "bear_case", "verdict_stance", "verdict_action", "setup"):
        assert legacy not in fields, f"legacy v1 field {legacy!r} still on Thesis"


def test_verdict_is_a_closed_set() -> None:
    """Frontend pill colour-codes on verdict; an open string would let the
    model invent shapes the UI doesn't render."""
    for valid in ("Overweight", "Neutral", "Underweight"):
        Thesis(
            company=_aspect(),
            fundamental=_aspect("Premium"),
            technical=_aspect("Uptrend"),
            news=_aspect(),
            verdict=valid,  # pyright: ignore[reportArgumentType]
            verdict_rationale="cites Premium and Uptrend (source: fundamental, technical).",
        )

    with pytest.raises(ValidationError):
        Thesis(
            company=_aspect(),
            fundamental=_aspect("Premium"),
            technical=_aspect("Uptrend"),
            news=_aspect(),
            verdict="Bullish",  # pyright: ignore[reportArgumentType]
            verdict_rationale="x",
        )


def test_aspect_supports_and_challenges_default_to_empty_list() -> None:
    """Asymmetry must be representable in the schema without forcing the
    caller to pass ``[]`` explicitly."""
    a = AspectView(label=None, summary="x")
    assert a.supports == []
    assert a.challenges == []


def test_thesis_round_trips_through_json() -> None:
    """API path: the structured form must serialise to JSON without losing
    fields, so the SSE endpoint can stream it to the frontend verbatim."""
    original = _full_thesis()
    payload = original.model_dump_json()
    restored = Thesis.model_validate_json(payload)
    assert restored == original


# ───────────────────────── Markdown renderer ─────────────────────────────────


def test_to_markdown_contains_all_four_aspect_headings_and_verdict() -> None:
    rendered = _full_thesis().to_markdown()
    for heading in ("## Company", "## Fundamental", "## Technical", "## News", "## Verdict"):
        assert heading in rendered, f"missing heading: {heading}"
    indices = [
        rendered.index(heading)
        for heading in ("## Company", "## Fundamental", "## Technical", "## News", "## Verdict")
    ]
    assert indices == sorted(indices)


def test_to_markdown_renders_supports_and_challenges_bullets() -> None:
    rendered = _full_thesis().to_markdown()
    assert "+ RSI 62 with SMA50 sloping up (source: technical)." in rendered
    # QNT-294 (C-6): challenges glyph normalized to "- " (was "· "), matching comparison.
    assert "- Customer concentration in top three hyperscalers (source: company)." in rendered


def test_to_markdown_renders_aspect_label_when_present() -> None:
    rendered = _full_thesis().to_markdown()
    assert "**Label:** Premium" in rendered
    assert "**Label:** Uptrend" in rendered


def test_to_markdown_omits_label_line_when_label_is_none() -> None:
    """Company and News aspects pass label=None — the renderer must not
    print a ghost ``**Label:**`` line for them."""
    rendered = _full_thesis().to_markdown()
    # Two label lines should appear in total (fundamental + technical).
    assert rendered.count("**Label:**") == 2


def test_to_markdown_handles_empty_aspect_lists_gracefully() -> None:
    """Asymmetry: empty supports/challenges must not produce a ghost bullet."""
    t = Thesis(
        company=AspectView(label=None, summary="x", supports=[], challenges=[]),
        fundamental=AspectView(label="Inline", summary="x", supports=[], challenges=[]),
        technical=AspectView(label="Sideways", summary="x", supports=[], challenges=[]),
        news=AspectView(label=None, summary="x", supports=[], challenges=[]),
        verdict="Neutral",
        verdict_rationale="Inline + Sideways = Neutral",
    )
    rendered = t.to_markdown()
    # No ghost bullets
    assert "\n+ " not in rendered
    assert "\n- " not in rendered


def test_to_markdown_renders_verdict_visibly() -> None:
    """Verdict is the at-a-glance signal — must appear in the rendered text
    (the eval / hallucination scorer reads the markdown form)."""
    rendered = _full_thesis().to_markdown()
    assert "**Overweight**" in rendered


def test_to_markdown_preserves_verdict_rationale_text() -> None:
    t = _full_thesis()
    rendered = t.to_markdown()
    assert "verdict Overweight (source: technical, fundamental)" in rendered


# ─────────────── QNT-302: AspectView.label normalization (AC1) ────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Premium", "Premium"),
        ("premium", "Premium"),  # lower-case
        ("UPTREND", "Uptrend"),  # upper-case
        ("  Discounted  ", "Discounted"),  # surrounding whitespace
        ("Inline", "Inline"),
    ],
)
def test_aspect_label_normalizes_casing(raw: str, expected: str) -> None:
    """Off-casing must coerce to the canonical spelling so the frontend pill
    palette (indexed by the raw string) always gets a known key."""
    assert AspectView(label=raw, summary="x").label == expected  # pyright: ignore[reportArgumentType]
    assert normalize_aspect_label(raw) == expected


@pytest.mark.parametrize("junk", ["Bullish", "premuim", "N/M", "", "12", "Overweight"])
def test_aspect_label_junk_normalizes_to_none(junk: str) -> None:
    """Any off-vocabulary label maps to None (no chip) rather than raising —
    a raise inside with_structured_output would trip the retry/fallback."""
    assert AspectView(label=junk, summary="x").label is None  # pyright: ignore[reportArgumentType]
    assert normalize_aspect_label(junk) is None


def test_aspect_label_non_string_normalizes_to_none() -> None:
    assert normalize_aspect_label(42) is None
    assert normalize_aspect_label(None) is None


def test_aspect_label_omitted_defaults_to_none() -> None:
    assert AspectView(summary="x").label is None


# ─────────────── QNT-302: verdict-vs-labels tripwire (AC3) ────────────────────


@pytest.mark.parametrize(
    ("fundamental", "technical", "expected"),
    [
        ("Discounted", "Uptrend", "Overweight"),  # two favourable, none unfavourable
        ("Premium", "Downtrend", "Underweight"),  # two unfavourable
        ("Premium", "Uptrend", "Neutral"),  # one of each
        ("Inline", "Sideways", "Neutral"),  # both neutral
        ("Discounted", "Sideways", "Neutral"),  # one favourable is not enough
        (None, None, "Neutral"),  # narrative-only aspects
    ],
)
def test_expected_verdict_from_labels_rule(
    fundamental: str | None, technical: str | None, expected: str
) -> None:
    assert expected_verdict_from_labels([fundamental, technical]) == expected


def test_verdict_matches_labels_flags_contradiction() -> None:
    """A thesis whose verdict contradicts its labels is still constructible
    (advisory, never raises) but reports the mismatch via the flag."""
    inconsistent = make_thesis(verdict="Overweight")  # factory labels = Premium + Uptrend
    assert inconsistent.verdict_matches_labels() is False


def test_verdict_matches_labels_true_for_consistent_thesis() -> None:
    consistent = make_thesis(verdict="Neutral")  # Premium + Uptrend => Neutral
    assert consistent.verdict_matches_labels() is True


def test_factory_fixture_thesis_is_consistent() -> None:
    """Eval-layer guard: the shared thesis fixture the eval/test suite builds
    on must itself satisfy the label rule, so a future fixture edit that drifts
    the verdict off its labels is caught here."""
    assert make_thesis().verdict_matches_labels() is True
