"""Tests for the numeric-claim hallucination detector (QNT-67 eval (a))."""

from __future__ import annotations

import pytest
from agent.evals.hallucination import HallucinationResult, check, extract_numbers


class TestExtractNumbers:
    """Coverage for the regex + canonicalisation surface.

    Important to lock down: every legitimate report number form (decimals,
    percents, dollars, comma-thousands) must be caught by the SAME regex
    used on the thesis, otherwise a thesis quoting "$1,234" against a
    report saying "1,234" would be flagged as hallucinated.
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("RSI is 72.5 today.", {"72.5"}),
            ("Revenue grew to $1,234.56 million", {"1234.56"}),
            ("Up 25% YoY", {"25"}),
            ("Negative margin of -3.5%", {"-3.5"}),
            ("Multiple values: 10, 20.5, $30", {"10", "20.5", "30"}),
            ("No numbers here at all.", set()),
            ("", set()),
        ],
    )
    def test_extracts_canonicalised_tokens(self, text: str, expected: set[str]) -> None:
        assert set(extract_numbers(text)) == expected

    def test_dollar_and_percent_normalise_to_value(self) -> None:
        # $1,234 in thesis must be considered "the same number" as 1234 in
        # report — formatting is not arithmetic.
        assert extract_numbers("$1,234") == extract_numbers("1234")
        assert extract_numbers("25%") == extract_numbers("25")

    def test_decimal_precision_is_preserved(self) -> None:
        # 12.30 vs 12.3 — preserving the trailing zero is the design choice
        # that catches the "model rounded a number" form of arithmetic.
        # If this test fails, hallucination detection has been weakened
        # against a real ADR-003 violation class.
        assert "12.30" in extract_numbers("price 12.30")
        assert "12.3" in extract_numbers("price 12.3")
        assert extract_numbers("12.30") != extract_numbers("12.3")

    def test_markdown_scaffolding_is_ignored(self) -> None:
        # "## 1." is a heading number, not a claim about NVDA's RSI.
        text = "## 1. Overview\n## 2. Technical\nThe RSI is 72.5."
        assert extract_numbers(text) == frozenset({"72.5"})


class TestCheck:
    """End-to-end behaviour of the hallucination detector."""

    def test_clean_thesis_returns_ok(self) -> None:
        thesis = "P/E is 25 (source: fundamental). RSI 72.5 (source: technical)."
        reports = ["...P/E is 25 currently...", "...RSI 72.5 today..."]
        result = check(thesis, reports)
        assert isinstance(result, HallucinationResult)
        assert result.ok
        assert result.unsupported == ()
        assert result.reason() == "clean"

    def test_thesis_with_unsupported_number_is_flagged(self) -> None:
        # Report says nothing about 99 — that's a hallucination per ADR-003.
        thesis = "P/E is 99."
        reports = ["P/E is 25"]
        result = check(thesis, reports)
        assert not result.ok
        assert "99" in result.unsupported
        assert "99" in result.reason()

    def test_thesis_with_rounded_number_is_flagged(self) -> None:
        # Real ADR-003 violation: model rounded 12.345 to 12.3 — that's
        # arithmetic, not citation. Hallucination check must catch this.
        thesis = "Margin is 12.3%."
        reports = ["Margin is 12.345"]
        result = check(thesis, reports)
        assert not result.ok
        assert "12.3" in result.unsupported

    def test_no_numbers_in_thesis_passes_trivially(self) -> None:
        thesis = "Constructive overall (source: technical)."
        reports = ["irrelevant report"]
        assert check(thesis, reports).ok

    def test_number_in_any_report_counts_as_supported(self) -> None:
        # Thesis cites 25 — appears in fundamental, not technical. Either
        # report covers the citation under ADR-003.
        thesis = "P/E is 25."
        reports = ["technical: trend up", "fundamental: P/E is 25"]
        assert check(thesis, reports).ok


class TestSignMagnitudeSupport:
    """QNT-128 fix: a thesis quoting an unsigned magnitude should be considered
    supported when the report wrote the same magnitude with an explicit sign.

    Background: report templates render YoY changes with explicit ± signs
    (``Free cash flow: -16.09% YoY``). The model phrases those naturally in
    English (``free cash flow declined 16.09%``). The pre-fix regex flagged
    that as a hallucination because ``-16.09`` and ``16.09`` were distinct
    canonical tokens — false positive on three QNT-67 baseline records.
    """

    def test_unsigned_thesis_supported_by_negative_report(self) -> None:
        # Real AMZN baseline finding (commit 1b66e7b, run 20260425T092008Z).
        thesis = "Free cash flow declined 16.09% YoY (source: fundamental)."
        reports = ["Free cash flow: -16.09% YoY"]
        assert check(thesis, reports).ok

    def test_negative_thesis_supported_by_unsigned_report(self) -> None:
        # Inverse of the above — kept symmetric so a model that DOES preserve
        # the sign verbatim is also accepted against an unsigned report token.
        thesis = "Margin contraction of -3.5% (source: fundamental)."
        reports = ["Net margin change: 3.5%"]
        assert check(thesis, reports).ok

    def test_real_unh_baseline_finding_passes(self) -> None:
        # Real UNH baseline findings (89.02 + 99.82). One test exercises both
        # in the same shape they appeared on 2026-04-25.
        thesis = (
            "UNH revenue grew 12.31% YoY but net income and free cash flow "
            "declined 99.82% YoY and 89.02% YoY (source: fundamental)."
        )
        reports = [
            "## GROWTH (YoY)\n"
            "Revenue: +12.31% YoY\n"
            "Net income: -99.82% YoY\n"
            "Free cash flow: -89.02% YoY\n"
        ]
        assert check(thesis, reports).ok

    def test_rounding_still_flagged(self) -> None:
        # Defence: relaxing sign comparison must NOT relax precision. The
        # rounding case (12.3 from 12.345) is the canonical ADR-003 violation
        # the harness exists to catch — this test fails loudly if a "simplify"
        # refactor accidentally drops the precision discipline alongside the
        # sign relaxation.
        thesis = "Margin is 12.3% (source: fundamental)."
        reports = ["Margin is 12.345"]
        assert not check(thesis, reports).ok

    def test_made_up_magnitude_still_flagged(self) -> None:
        # Defence: a fabricated number with a sign is NOT supported just
        # because some other unrelated number in the report shares its
        # magnitude — magnitude support requires the magnitude to actually
        # appear in the report corpus.
        thesis = "P/E is -25 (source: fundamental)."
        reports = ["P/E is 30"]
        result = check(thesis, reports)
        assert not result.ok
        assert "-25" in result.unsupported

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Known + intentional blind spot: magnitude-support comparison "
            "accepts a sign-flipped thesis number as 'cited'. The trade-off "
            "is documented in hallucination.py's module docstring "
            "('Sign-magnitude support'). If a future fix re-introduces "
            "asymmetric sign comparison, this test will START PASSING and "
            "xfail(strict=True) will fail the suite — a load-bearing "
            "tripwire that the trade-off was made deliberately, not by "
            "accident. To intentionally remove the blind spot, delete this "
            "test alongside the docstring section."
        ),
    )
    def test_inverted_sign_thesis_should_be_flagged_but_is_not(self) -> None:
        # Report says +5%; thesis claims -5%. Semantically opposite — a model
        # error worth catching in principle, but the magnitude-support fix
        # accepts it. The LLM-as-judge / per-section structure are expected
        # to surface sign-direction errors at a higher level than the
        # numeric-citation regex.
        thesis = "Margin contracted -5% (source: fundamental)."
        reports = ["Net margin: +5%"]
        assert not check(thesis, reports).ok


class TestDeliberateFakeNumber:
    """AC: 'Hallucination check reliably flags a deliberately-introduced
    fake number in a test fixture.' This is the harness's own smoke test:
    if hallucination.check ever silently passes a known fake, the eval has
    lost its load-bearing guarantee."""

    def test_deliberately_introduced_fake_is_flagged(self) -> None:
        # Mirrors a real synthesize output but with one number swapped for
        # a value never present in any report.
        reports = [
            "NVDA technical report: RSI is 72.5, MACD positive, SMA_50 at 875.",
            "NVDA fundamental report: P/E is 65.2, latest EPS 0.81.",
        ]
        thesis_clean = (
            "NVDA RSI 72.5 (source: technical). P/E 65.2 (source: fundamental). Constructive."
        )
        thesis_dirty = (
            "NVDA RSI 72.5 (source: technical). P/E 999.9 (source: fundamental). Constructive."
        )

        # Sanity: clean thesis passes.
        assert check(thesis_clean, reports).ok

        # Load-bearing: dirty thesis MUST fail with the fake number named.
        result = check(thesis_dirty, reports)
        assert not result.ok
        assert "999.9" in result.unsupported
