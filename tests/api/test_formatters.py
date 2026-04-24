"""Unit tests for the central format helpers.

These are the rules all report templates inherit — every null/N/M value must
render with a readable explanation, never blank, never the string "None",
never a misleading 0. Covers the QNT-87 P/E rule end-to-end.
"""

from __future__ import annotations

import math

from api.formatters import (
    format_currency,
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
        assert format_signed_pct(5.25) == "+5.25%"

    def test_negative_value_keeps_its_minus(self) -> None:
        assert format_signed_pct(-3.5) == "-3.50%"

    def test_zero_uses_plus_sign(self) -> None:
        # Zero is non-negative, so the positive branch applies — harmless.
        assert format_signed_pct(0.0) == "+0.00%"

    def test_none_gets_na_reason(self) -> None:
        assert format_signed_pct(None) == "N/M (data unavailable)"


class TestFormatCurrency:
    def test_formats_with_dollar_and_thousands(self) -> None:
        assert format_currency(1234567.89) == "$1,234,567.89"

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
