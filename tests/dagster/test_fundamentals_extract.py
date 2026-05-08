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
