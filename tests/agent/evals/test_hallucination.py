"""Tests for the numeric-claim hallucination detector (QNT-67 eval (a))."""

from __future__ import annotations

import pytest
from agent.evals.hallucination import (
    HallucinationResult,
    _extract_scaled,
    check,
    extract_numbers,
)


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

    def test_trailing_fractional_zeros_are_formatting(self) -> None:
        # QNT-361: 16.60 and 16.6 are the same value — a trailing fractional
        # zero is formatting, not arithmetic, so both canonicalise to 16.6.
        # (Pre-361 this was the inverse: the trailing zero was preserved and a
        # report "+16.60%" false-flagged a narrator's spoken "16.6".)
        assert extract_numbers("price 16.60") == extract_numbers("price 16.6")
        assert extract_numbers("12.30") == extract_numbers("12.3")
        assert extract_numbers("grew 5.00%") == extract_numbers("grew 5")
        # Integer zeros are value, not formatting — never stripped.
        assert "100" in extract_numbers("total of 100")
        assert "20" in extract_numbers("about 20")

    def test_genuine_rounding_stays_distinct(self) -> None:
        # Exact value equality, NOT rounding tolerance: 19.36 vs 19.4 are
        # different values. If this test fails, hallucination detection has
        # been weakened against a real ADR-003 violation class.
        assert extract_numbers("19.36") != extract_numbers("19.4")
        assert extract_numbers("12.345") != extract_numbers("12.3")

    def test_markdown_scaffolding_is_ignored(self) -> None:
        # "## 1." is a heading number, not a claim about NVDA's RSI.
        text = "## 1. Overview\n## 2. Technical\nThe RSI is 72.5."
        assert extract_numbers(text) == frozenset({"72.5"})

    def test_citation_anchor_id_is_not_a_number(self) -> None:
        # QNT-301: an anchored retrieved-source citation "(source: news R1)" glues
        # the id digit to the "R", so the left-boundary lookbehind rejects it --
        # the anchor id must never read as a numeric claim (which would flag a
        # clean answer as hallucinated). Docstring's "letters, not digits" note
        # now covers the R-prefixed id form too.
        assert extract_numbers("Buyback expanded (source: news R1).") == frozenset()
        # The bare bracketed form the narrate voice emits ("...deal [R2]...") is
        # equally safe -- the digit is still glued to the R.
        assert extract_numbers("The Firmus deal expanded reach [R2].") == frozenset()
        # And the anchor doesn't swallow or taint a real number beside it.
        assert extract_numbers("RSI is 72.5 (source: technical R2).") == frozenset({"72.5"})


class TestPeriodIdiom:
    """Time-window labels ("5-year low", "52-week high") are not numeric claims.

    Regression for the QNT-221 advisory guard: the report writes the lookback
    window in a compact form ("over last 5y", "near 5y low") that the regex's
    word boundary already ignores, but the model paraphrases it to "5-year",
    where the hyphen lets a bare "5" leak through as a counted number. On the
    real TSLA trace b7c2187 (2026-06-09) that single phantom number dropped a
    clean thesis's grounding from 1.0 to 0.91.
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("near the 5-year low", set()),
            ("52-week high of 408.95", {"408.95"}),
            ("the 200-day moving average is 875", {"875"}),
            ("over the past 3 months", set()),
            ("a 6-month high", set()),
        ],
    )
    def test_window_labels_are_dropped(self, text: str, expected: set[str]) -> None:
        assert set(extract_numbers(text)) == expected

    def test_non_period_hyphen_compound_still_counts(self) -> None:
        # Defence: the unit list is closed to period words. A valuation idiom
        # like "20-times earnings" is a real numeric claim, not a window label,
        # so its number must still be checked.
        assert "20" in extract_numbers("trading at 20-times earnings")

    def test_five_year_low_idiom_not_flagged(self) -> None:
        # Real TSLA trace b7c2187: report wrote "near 5y low", model wrote
        # "near the 5-year low" — the bare "5" was the sole unsupported number.
        thesis = "P/E of 397.70 is near the 5-year low (source: fundamental)."
        reports = ["P/E (quarterly): 397.70 (range 397.70-404.82 over last 5y, near 5y low)"]
        result = check(thesis, reports)
        assert result.ok
        assert result.unsupported == ()


class TestMagnitudeUnit:
    """Numbers glued to a magnitude unit ($2.5T, $14B, 20k) are value-equivalent
    to their expanded form.

    QNT-255 clean-window finding: news reports quote market caps / deal sizes
    with a glued scale suffix ("Breaches $2.5T", "$14B AI Push"). The model
    expands them ("$2.5 trillion", "$14 billion") and writes the bare mantissa.
    Before the fix the report's "$2.5T" extracted NO "2.5" (the right boundary
    rejected the glued "T"), so a grounded answer read as hallucinated — the
    sole cause of the reproducible tsla-news-sentiment / meta-news-sentiment
    flags on the otherwise-clean QNT-255 sweep.
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("market cap of $2.5T", {"2.5"}),
            ("a $14B AI push", {"14"}),
            ("$2.52 trillion valuation", {"2.52"}),
            ("raised 20k units", {"20"}),
            ("$1.05B buyback", {"1.05"}),
        ],
    )
    def test_glued_unit_reduces_to_mantissa(self, text: str, expected: set[str]) -> None:
        assert set(extract_numbers(text)) == expected

    @pytest.mark.parametrize(
        "text",
        ["Q3 earnings", "3D printing", "running 5km", "spread of 2bps", "the 5x multiple"],
    )
    def test_non_magnitude_letter_suffix_not_stripped(self, text: str) -> None:
        # Defence: the unit set is closed to k/m/b/t/bn/tn/mn. A trailing letter
        # that is not a magnitude unit must NOT cause a spurious bare-number
        # extraction (Q3/3D/5km/2bps/5x are not "3"/"5"/"2" claims).
        assert extract_numbers(text) == frozenset()

    def test_real_tsla_market_cap_finding_passes(self) -> None:
        # Real QNT-255 trace: report headline "Breaches $2.5T", model wrote the
        # expanded "$2.5 trillion". The bare 2.5 was the sole unsupported number.
        thesis = "SpaceX hit a $2.5 trillion market cap, past Tesla (source: news)."
        reports = ["SpaceX Market Cap Breaches $2.5T: Goes Past Tesla, TSMC And Broadcom"]
        result = check(thesis, reports)
        assert result.ok
        assert result.unsupported == ()

    def test_real_meta_deal_size_finding_passes(self) -> None:
        # Real QNT-255 trace: report headline "$14B AI Push", model wrote
        # "$14 billion". The bare 14 was the sole unsupported number.
        thesis = "Meta is making a $14 billion AI push beyond advertising (source: news)."
        reports = ["Meta's $14B AI Push Faces Growing Pressure to Deliver Results"]
        result = check(thesis, reports)
        assert result.ok
        assert result.unsupported == ()

    def test_rounded_mantissa_with_unit_still_flagged(self) -> None:
        # Defence: stripping the unit must not also forgive rounding. Report
        # says $2.52T; a thesis writing $2.5T is a rounded mantissa (2.5 vs
        # 2.52) and stays flagged — the unit strip is orthogonal to precision.
        thesis = "valued at $2.5T (source: news)."
        reports = ["hitting a market cap of $2.52T"]
        result = check(thesis, reports)
        assert not result.ok
        assert "2.5" in result.unsupported


class TestScaleAwareGrounding:
    """QNT-297: a claimed scale is checked against the report, so a misquoted
    magnitude ("$5M" from a report's "$5B") is flagged rather than accepted.

    Background: the QNT-255 unit-strip made "$5M" and "$5B" both canonicalise
    to "5", collapsing scale. Comparing (mantissa, scale) pairs restores the
    scale discipline WITHOUT reviving the QNT-255 false positive — a spelled-out
    "$2.5 trillion" is folded to the same tag as a report's "$2.5T", and a bare
    mantissa on either side never conflicts.
    """

    def test_wrong_scale_is_flagged(self) -> None:
        # AC1: answer says millions, report only ever said billions.
        result = check("Deal size was $5M (source: news).", ["Firmus $5B AI deal"])
        assert not result.ok
        assert "5" in result.unsupported

    def test_spelled_trillion_matches_glued_report(self) -> None:
        # AC1: "$2.5 trillion" (expanded) is supported by a report "$2.5T".
        result = check("Market cap of $2.5 trillion (source: news).", ["Breaches $2.5T"])
        assert result.ok
        assert result.unsupported == ()

    def test_spelled_billion_matches_glued_report(self) -> None:
        # AC1: "$14 billion" is supported by a report "$14B".
        result = check("A $14 billion AI push (source: news).", ["$14B AI Push"])
        assert result.ok
        assert result.unsupported == ()

    def test_bare_mantissa_matches_any_report_scale(self) -> None:
        # AC1: a bare "5" (no scale) is unchanged behaviour — supported by a
        # report's "$5B". This is the QNT-255-preserving branch.
        result = check("The figure was 5 (source: news).", ["valued at $5B"])
        assert result.ok
        assert result.unsupported == ()

    def test_bare_report_mantissa_supports_any_claimed_scale(self) -> None:
        # A report that wrote the mantissa bare (no unit) gives no evidence of
        # a scale mismatch, so a claimed "$5M" is NOT flagged against it.
        result = check("Deal size was $5M (source: news).", ["the figure was 5"])
        assert result.ok
        assert result.unsupported == ()

    def test_matching_scale_is_supported(self) -> None:
        result = check("A $5B round (source: news).", ["closed a $5B round"])
        assert result.ok

    def test_absent_value_still_flagged_regardless_of_scale(self) -> None:
        # Defence: the scale layer is additive — a mantissa absent from every
        # report is still flagged by the existing magnitude gate.
        result = check("Worth $9B (source: news).", ["only ever mentions $5B"])
        assert not result.ok
        assert "9" in result.unsupported

    def test_report_with_both_bare_and_scaled_does_not_conflict(self) -> None:
        # If the corpus wrote the mantissa both bare and scaled, the bare form
        # clears any claimed scale (no evidence of mismatch).
        result = check("Deal was $5M (source: news).", ["a $5B deal", "roughly 5 units"])
        assert result.ok

    def test_extract_scaled_tags_units(self) -> None:
        assert _extract_scaled("$5M") == frozenset({("5", "M")})
        assert _extract_scaled("$2.5 trillion") == frozenset({("2.5", "T")})
        assert _extract_scaled("$14 billion") == frozenset({("14", "B")})
        assert _extract_scaled("20k units") == frozenset({("20", "K")})
        assert _extract_scaled("the figure 5") == frozenset({("5", None)})


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

    def test_incident_trailing_zero_quote_is_supported(self) -> None:
        # Real QNT-361 incident (trace d9bbf008, 2026-07-11 AAPL thesis): the
        # report wrote "+16.60% YoY", the narrator spoke "16.6%" — same value,
        # was redacted as [unsupported number]. Must pass now.
        thesis = "Revenue grew 16.6% YoY (source: fundamental)."
        reports = ["Revenue (quarterly): +16.60% YoY"]
        result = check(thesis, reports)
        assert result.ok
        assert result.unsupported == ()

    def test_scale_block_compact_dollars_ground_spoken_form(self) -> None:
        # QNT-361 follow-up: SCALE prints "$129.2B" (was $129,174,000,000,
        # which the narrator rounded to an ungrounded "$129.2B"). The spoken
        # expansion "$129.2 billion" folds to the same (mantissa, scale) pair
        # via the QNT-297 machinery, so the loop closes.
        thesis = "Free cash flow of $129.2 billion TTM supports buybacks (source: fundamental)."
        reports = ["## SCALE\nFree cash flow (TTM): $129.2B"]
        result = check(thesis, reports)
        assert result.ok
        assert result.unsupported == ()

    def test_incident_rounded_quote_still_flagged(self) -> None:
        # The other half of the incident: report "+19.36% YoY", narrator spoke
        # "19.4%" — genuine rounding, stays flagged (out-of-scope by design:
        # tolerance here would weaken the no-arithmetic contract).
        thesis = "Net income grew 19.4% YoY (source: fundamental)."
        reports = ["Net income (quarterly): +19.36% YoY"]
        result = check(thesis, reports)
        assert not result.ok
        assert "19.4" in result.unsupported

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
