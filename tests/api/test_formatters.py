"""Unit tests for the central format helpers.

These are the rules all report templates inherit — every null/N/M value must
render with a readable explanation, never blank, never the string "None",
never a misleading 0. Covers the QNT-87 P/E rule end-to-end.
"""

from __future__ import annotations

import math

from api.formatters import (
    format_currency,
    format_currency_compact,
    format_pct,
    format_ratio,
    format_signed_pct,
    pe_na_reason,
)


class TestFormatRatio:
    def test_formats_finite_value_to_precision(self) -> None:
        assert format_ratio(12.3456) == "12.35"
        assert format_ratio(12.3456, precision=4) == "12.3456"

    def test_none_renders_as_nm_with_default_reason(self) -> None:
        assert format_ratio(None) == "N/M (data unavailable)"

    def test_none_renders_as_nm_with_custom_reason(self) -> None:
        assert format_ratio(None, na_reason="near-zero earnings") == "N/M (near-zero earnings)"

    def test_nan_is_treated_as_missing(self) -> None:
        assert format_ratio(float("nan")) == "N/M (data unavailable)"

    def test_infinity_is_treated_as_missing(self) -> None:
        assert format_ratio(math.inf) == "N/M (data unavailable)"

    def test_suffix_is_appended(self) -> None:
        assert format_ratio(75.0, suffix="%") == "75.00%"


class TestFormatPct:
    def test_formats_percent_with_single_decimal(self) -> None:
        assert format_pct(73.214) == "73.2%"

    def test_none_gets_na_reason(self) -> None:
        assert format_pct(None, na_reason="not reported") == "N/M (not reported)"


class TestFormatSignedPct:
    def test_positive_value_gets_explicit_plus(self) -> None:
        # One decimal by default (QNT-361): percentages are quoted at 1dp.
        assert format_signed_pct(5.27) == "+5.3%"

    def test_negative_value_keeps_its_minus(self) -> None:
        assert format_signed_pct(-3.5) == "-3.5%"

    def test_zero_uses_plus_sign(self) -> None:
        # Zero is non-negative, so the positive branch applies — harmless.
        assert format_signed_pct(0.0) == "+0.0%"

    def test_none_gets_na_reason(self) -> None:
        assert format_signed_pct(None) == "N/M (data unavailable)"


class TestFormatCurrencyCompact:
    def test_billions_render_scale_suffixed_at_one_decimal(self) -> None:
        # The QNT-361 follow-up incident value: report printed
        # $129,174,000,000, narrator spoke $129.2B and got flagged. The
        # report now prints the speakable form.
        assert format_currency_compact(129_174_000_000.0) == "$129.2B"

    def test_trillions_and_millions(self) -> None:
        assert format_currency_compact(3_000_000_000_000.0) == "$3.0T"
        assert format_currency_compact(451_442_000_000.0) == "$451.4B"
        assert format_currency_compact(14_500_000.0) == "$14.5M"

    def test_negative_sign_leads_the_currency_symbol(self) -> None:
        assert format_currency_compact(-1_500_000_000.0) == "-$1.5B"

    def test_under_a_million_falls_back_to_exact(self) -> None:
        assert format_currency_compact(500_000.0) == "$500,000"

    def test_none_gets_na_reason(self) -> None:
        assert format_currency_compact(None) == "N/M (data unavailable)"


class TestFormatCurrency:
    def test_formats_with_dollar_and_thousands(self) -> None:
        assert format_currency(1234567.89) == "$1,234,567.89"

    def test_negative_sign_leads_the_currency_symbol(self) -> None:
        # -$500M, not $-500M (accounting placement; QNT-354 SCALE net income/FCF).
        assert format_currency(-500_000_000, precision=0) == "-$500,000,000"

    def test_none_gets_na_reason(self) -> None:
        assert format_currency(None) == "N/M (data unavailable)"


class TestPeNaReason:
    def test_eps_none_returns_unavailable(self) -> None:
        assert pe_na_reason(None) == "EPS unavailable"

    def test_eps_near_zero_returns_near_zero_earnings(self) -> None:
        assert pe_na_reason(0.05) == "near-zero earnings"
        assert pe_na_reason(-0.05) == "near-zero earnings"
        assert pe_na_reason(0.0) == "near-zero earnings"

    def test_eps_above_threshold_returns_data_unavailable(self) -> None:
        # When EPS is healthy but P/E is still null, reason is "data unavailable".
        # QNT-87 threshold is $0.10 exactly.
        assert pe_na_reason(0.10) == "data unavailable"
        assert pe_na_reason(2.87) == "data unavailable"
