"""Unit tests for ``_extract_periods`` in the fundamentals asset (QNT-179).

The bug under test: yfinance lists a just-reported quarter as a column
header before its values are hydrated. Old behaviour zeroed every line
item via ``_safe_get`` and inserted an all-zero stub row that broke
downstream ratios (AAPL Q2 FY2026 race; 8 historical stubs across
GOOGL/JPM/META/TSLA/UNH never overwritten). New behaviour: skip the
period entirely when Total Revenue is missing.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dagster_pipelines.assets.fundamentals import _extract_periods, _safe_get


def _frame(line_items: dict[str, list[float]], periods: list[str]) -> pd.DataFrame:
    """Helper: yfinance frames have line items as rows, period_ends as columns."""
    cols = pd.to_datetime(periods)
    return pd.DataFrame(line_items, index=cols).T


_INFO = {"ebitda": 1_000_000.0, "sharesOutstanding": 100, "marketCap": 50_000_000.0}


def test_skips_period_with_missing_revenue() -> None:
    """A period whose Total Revenue cell is NaN must be dropped, not zero-filled."""
    income = _frame(
        {
            "Total Revenue": [100.0, np.nan],  # second period has no revenue
            "Gross Profit": [60.0, np.nan],
            "Net Income": [20.0, np.nan],
        },
        ["2025-12-31", "2026-03-31"],
    )
    balance = _frame(
        {"Total Assets": [500.0, np.nan]},
        ["2025-12-31", "2026-03-31"],
    )
    cashflow = _frame(
        {"Free Cash Flow": [40.0, np.nan]},
        ["2025-12-31", "2026-03-31"],
    )

    rows = _extract_periods(income, balance, cashflow, _INFO, "AAPL", "quarterly")

    assert [r["period_end"].isoformat() for r in rows] == ["2025-12-31"]
    assert rows[0]["revenue"] == 100.0
    assert rows[0]["net_income"] == 20.0


def test_keeps_period_with_real_zero_net_income() -> None:
    """A loss-making quarter with revenue but zero net_income stays. The fix
    must not over-aggressively prune real zeros — only stub rows whose
    spine (revenue) is missing."""
    income = _frame(
        {
            "Total Revenue": [200.0],
            "Gross Profit": [80.0],
            "Net Income": [0.0],
        },
        ["2025-12-31"],
    )
    balance = _frame({"Total Assets": [600.0]}, ["2025-12-31"])
    cashflow = _frame({"Free Cash Flow": [10.0]}, ["2025-12-31"])

    rows = _extract_periods(income, balance, cashflow, _INFO, "AAPL", "quarterly")

    assert len(rows) == 1
    assert rows[0]["revenue"] == 200.0
    assert rows[0]["net_income"] == 0.0


def test_safe_get_returns_none_on_missing() -> None:
    df = _frame({"Total Revenue": [np.nan]}, ["2025-12-31"])
    period = pd.to_datetime("2025-12-31")
    assert _safe_get(df, "Total Revenue", period) is None
    assert _safe_get(df, "Nonexistent Field", period) is None


def test_safe_get_returns_value_when_present() -> None:
    df = _frame({"Total Revenue": [123.45]}, ["2025-12-31"])
    period = pd.to_datetime("2025-12-31")
    assert _safe_get(df, "Total Revenue", period) == 123.45


def test_safe_get_returns_zero_when_value_is_real_zero() -> None:
    df = _frame({"Net Income": [0.0]}, ["2025-12-31"])
    period = pd.to_datetime("2025-12-31")
    assert _safe_get(df, "Net Income", period) == 0.0


def test_handles_empty_income_statement() -> None:
    rows = _extract_periods(
        pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), _INFO, "AAPL", "quarterly"
    )
    assert rows == []


def test_implied_shares_prefers_all_class_count() -> None:
    """Dual-class names (GOOGL): implied_shares_outstanding must use yfinance's
    all-class impliedSharesOutstanding, not the per-class sharesOutstanding that
    halves the market cap."""
    income = _frame({"Total Revenue": [100.0]}, ["2025-12-31"])
    info = {**_INFO, "sharesOutstanding": 5_863_336_837, "impliedSharesOutstanding": 12_194_935_223}

    rows = _extract_periods(income, pd.DataFrame(), pd.DataFrame(), info, "GOOGL", "quarterly")

    assert rows[0]["shares_outstanding"] == 5_863_336_837
    assert rows[0]["implied_shares_outstanding"] == 12_194_935_223


def test_implied_shares_falls_back_to_per_class_when_absent() -> None:
    """Single-class names omit impliedSharesOutstanding — fall back so the count
    is never zero."""
    income = _frame({"Total Revenue": [100.0]}, ["2025-12-31"])
    # _INFO has sharesOutstanding=100 and no impliedSharesOutstanding.
    rows = _extract_periods(income, pd.DataFrame(), pd.DataFrame(), _INFO, "MSFT", "quarterly")

    assert rows[0]["implied_shares_outstanding"] == 100


def test_shares_outstanding_per_period_from_balance_sheet() -> None:
    """QNT-382: each period carries the balance-sheet "Ordinary Shares Number"
    as of that period end — buybacks stay visible in the history — never the
    current info.sharesOutstanding snapshot stamped across every row."""
    periods = ["2025-12-31", "2026-03-31"]
    income = _frame({"Total Revenue": [100.0, 110.0]}, periods)
    balance = _frame({"Ordinary Shares Number": [15_500.0, 15_000.0]}, periods)

    rows = _extract_periods(income, balance, pd.DataFrame(), _INFO, "AAPL", "quarterly")

    by_period = {r["period_end"].isoformat(): r for r in rows}
    assert by_period["2025-12-31"]["shares_outstanding"] == 15_500
    assert by_period["2026-03-31"]["shares_outstanding"] == 15_000


def test_shares_snapshot_fallback_only_on_newest_period() -> None:
    """A period without a balance-sheet share count lands None (NULL), except
    the newest period, where the current snapshot is a faithful stand-in."""
    periods = ["2025-12-31", "2026-03-31"]
    income = _frame({"Total Revenue": [100.0, 110.0]}, periods)

    rows = _extract_periods(income, pd.DataFrame(), pd.DataFrame(), _INFO, "AAPL", "quarterly")

    by_period = {r["period_end"].isoformat(): r for r in rows}
    assert by_period["2025-12-31"]["shares_outstanding"] is None
    assert by_period["2026-03-31"]["shares_outstanding"] == 100  # _INFO snapshot


def test_implied_shares_null_on_historical_periods() -> None:
    """implied_shares_outstanding is a point-in-time snapshot: stamped only on
    the newest period, NULL on history (QNT-382)."""
    periods = ["2025-12-31", "2026-03-31"]
    income = _frame({"Total Revenue": [100.0, 110.0]}, periods)

    rows = _extract_periods(income, pd.DataFrame(), pd.DataFrame(), _INFO, "AAPL", "quarterly")

    by_period = {r["period_end"].isoformat(): r for r in rows}
    assert by_period["2025-12-31"]["implied_shares_outstanding"] is None
    assert by_period["2026-03-31"]["implied_shares_outstanding"] == 100


def test_missing_debt_and_cash_land_none_not_zero() -> None:
    """QNT-382: a period where yfinance omits Total Debt / Cash must carry None
    (NULL downstream), not a fake debt-free 0.0. Present values pass through and
    the other balance-sheet fields keep their zero-coercion."""
    periods = ["2025-12-31", "2026-03-31"]
    income = _frame({"Total Revenue": [100.0, 110.0]}, periods)
    balance = _frame(
        {
            "Total Assets": [500.0, 520.0],
            "Total Debt": [np.nan, 80.0],
            "Cash And Cash Equivalents": [np.nan, 30.0],
        },
        periods,
    )

    rows = _extract_periods(income, balance, pd.DataFrame(), _INFO, "AAPL", "quarterly")

    by_period = {r["period_end"].isoformat(): r for r in rows}
    assert by_period["2025-12-31"]["total_debt"] is None
    assert by_period["2025-12-31"]["cash_and_equivalents"] is None
    assert by_period["2026-03-31"]["total_debt"] == 80.0
    assert by_period["2026-03-31"]["cash_and_equivalents"] == 30.0
    # Non-debt/cash fields are unchanged by QNT-382: still zero-coerced.
    assert by_period["2025-12-31"]["total_liabilities"] == 0.0
