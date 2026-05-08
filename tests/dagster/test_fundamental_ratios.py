"""Exact-match validation for `compute_fundamental_ratios` (QNT-47).

The AC calls for *exact match* for fundamental ratios given the same inputs.
Tests load the synthetic `synthetic_fundamentals.csv` fixture (round-number
inputs chosen so every expected value is hand-derivable) and compare the
output of `compute_fundamental_ratios()` to the formulas documented inline.

`latest_close = 200.00`, `shares_outstanding = 1,000,000` → `market_cap = $200M`
in every row of the fixture, which keeps the hand arithmetic simple.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from dagster_pipelines.assets.fundamental_summary import compute_fundamental_ratios

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "fundamentals" / "synthetic_fundamentals.csv"
)

LATEST_CLOSE = 200.00
SHARES = 1_000_000
MARKET_CAP = LATEST_CLOSE * SHARES  # $200,000,000


@pytest.fixture
def ratios() -> pd.DataFrame:
    df = pd.read_csv(FIXTURE)
    df["period_end"] = pd.to_datetime(df["period_end"]).dt.date
    result = compute_fundamental_ratios(df, LATEST_CLOSE)
    # Index by (period_end, period_type) for easy row lookup in tests.
    return result.set_index(["period_end", "period_type"])


def _row(ratios: pd.DataFrame, period_end: str, period_type: str) -> pd.Series:
    key = (pd.to_datetime(period_end).date(), period_type)
    return ratios.loc[key]


# ─── Annual 2024 row (fully populated: all ratios + YoY available) ────────────
#
# Inputs: revenue=150M, gross_profit=75M, net_income=20M, total_assets=300M,
# total_liabilities=150M, current_assets=100M, current_liabilities=40M,
# free_cash_flow=30M, ebitda=40M, total_debt=75M, cash=25M, shares=1M
# Prior-year (2023) inputs: revenue=100M, net_income=10M, fcf=15M


def test_annual_2024_eps(ratios: pd.DataFrame) -> None:
    # EPS = net_income / shares = 20M / 1M = 20.0
    assert _row(ratios, "2024-12-31", "annual")["eps"] == 20.0


def test_annual_2024_pe_ratio(ratios: pd.DataFrame) -> None:
    # Annual P/E uses full-year net_income (no TTM rolling).
    # P/E = market_cap / net_income = 200M / 20M = 10.0
    assert _row(ratios, "2024-12-31", "annual")["pe_ratio"] == 10.0


def test_annual_2024_ev_ebitda(ratios: pd.DataFrame) -> None:
    # EV = market_cap + total_debt - cash = 200M + 75M - 25M = 250M
    # EV/EBITDA = 250M / 40M = 6.25
    assert _row(ratios, "2024-12-31", "annual")["ev_ebitda"] == 6.25


def test_annual_2024_price_to_book(ratios: pd.DataFrame) -> None:
    # Book value = total_assets - total_liabilities = 300M - 150M = 150M
    # P/B = market_cap / book_value = 200M / 150M = 4/3
    assert _row(ratios, "2024-12-31", "annual")["price_to_book"] == pytest.approx(4 / 3)


def test_annual_2024_price_to_sales(ratios: pd.DataFrame) -> None:
    # P/S = market_cap / revenue = 200M / 150M = 4/3
    assert _row(ratios, "2024-12-31", "annual")["price_to_sales"] == pytest.approx(4 / 3)


def test_annual_2024_margins(ratios: pd.DataFrame) -> None:
    row = _row(ratios, "2024-12-31", "annual")
    # net_margin = net_income / revenue * 100 = 20/150*100 = 40/3
    assert row["net_margin_pct"] == pytest.approx(40 / 3)
    # gross_margin = gross_profit / revenue * 100 = 75/150*100 = 50.0
    assert row["gross_margin_pct"] == 50.0


def test_annual_2024_roe_roa(ratios: pd.DataFrame) -> None:
    row = _row(ratios, "2024-12-31", "annual")
    # ROE = net_income / equity * 100 = 20/150*100 = 40/3
    assert row["roe"] == pytest.approx(40 / 3)
    # ROA = net_income / total_assets * 100 = 20/300*100 = 20/3
    assert row["roa"] == pytest.approx(20 / 3)


def test_annual_2024_fcf_yield(ratios: pd.DataFrame) -> None:
    # FCF yield = free_cash_flow / market_cap * 100 = 30M / 200M * 100 = 15.0
    assert _row(ratios, "2024-12-31", "annual")["fcf_yield"] == 15.0


def test_annual_2024_debt_to_equity(ratios: pd.DataFrame) -> None:
    # D/E = total_debt / equity = 75M / 150M = 0.5
    assert _row(ratios, "2024-12-31", "annual")["debt_to_equity"] == 0.5


def test_annual_2024_current_ratio(ratios: pd.DataFrame) -> None:
    # current_ratio = current_assets / current_liabilities = 100M / 40M = 2.5
    assert _row(ratios, "2024-12-31", "annual")["current_ratio"] == 2.5


def test_annual_2024_yoy_growth(ratios: pd.DataFrame) -> None:
    row = _row(ratios, "2024-12-31", "annual")
    # revenue YoY = (150M - 100M) / 100M * 100 = 50.0
    assert row["revenue_yoy_pct"] == 50.0
    # net_income YoY = (20M - 10M) / 10M * 100 = 100.0
    assert row["net_income_yoy_pct"] == 100.0
    # fcf YoY = (30M - 15M) / 15M * 100 = 100.0
    assert row["fcf_yoy_pct"] == 100.0


def test_annual_2023_yoy_growth_is_nan(ratios: pd.DataFrame) -> None:
    # First annual period has no prior → YoY columns must be NaN.
    row = _row(ratios, "2023-12-31", "annual")
    assert bool(pd.isna(row["revenue_yoy_pct"]))
    assert bool(pd.isna(row["net_income_yoy_pct"]))
    assert bool(pd.isna(row["fcf_yoy_pct"]))


# ─── Quarterly TTM behaviour ─────────────────────────────────────────────────
#
# P/E on quarterly rows uses TTM (trailing 4-quarter) net_income, not a single
# quarter, which is what Yahoo Finance / TradingView quote as "P/E (TTM)".


def test_quarterly_2024_q4_ttm_pe_ratio(ratios: pd.DataFrame) -> None:
    # 2024 quarterly net_income: 4M + 4.5M + 5M + 6.5M = 20M TTM
    # P/E (TTM) = market_cap / TTM_net_income = 200M / 20M = 10.0
    assert _row(ratios, "2024-12-31", "quarterly")["pe_ratio"] == 10.0


def test_quarterly_2024_q3_ttm_pe_ratio(ratios: pd.DataFrame) -> None:
    # TTM through 2024-Q3 = 3M (2023Q4) + 4M (2024Q1) + 4.5M (2024Q2) + 5M (2024Q3) = 16.5M
    # P/E = 200M / 16.5M = 12.121212...
    assert _row(ratios, "2024-09-30", "quarterly")["pe_ratio"] == pytest.approx(200 / 16.5)


def test_quarterly_first_three_quarters_have_nan_pe(ratios: pd.DataFrame) -> None:
    # TTM needs 4 consecutive quarters; first three quarters of the fixture
    # (2023 Q1/Q2/Q3) should produce NaN P/E.
    for period_end in ("2023-03-31", "2023-06-30", "2023-09-30"):
        assert bool(pd.isna(_row(ratios, period_end, "quarterly")["pe_ratio"]))


def test_quarterly_2023_q4_first_valid_ttm_pe(ratios: pd.DataFrame) -> None:
    # 2023 TTM (Q1-Q4) net_income = 2 + 2.5 + 2.5 + 3 = 10M
    # P/E = 200M / 10M = 20.0
    assert _row(ratios, "2023-12-31", "quarterly")["pe_ratio"] == 20.0


# ─── Near-zero EPS → P/E is N/M (not meaningful) ────────────────────────────


# ─── TTM balance-sheet ratios (QNT-179 round 2) ──────────────────────────────
#
# `_build_ttm_rows` pairs TTM net_income / gross_profit with the matching
# quarter's balance-sheet snapshot to surface roe / roa / gross_margin / D-E /
# current ratio on the TTM row. The fundamentals UI reads these off the latest
# TTM row to render the Quarterly tab's ROE/ROA — without them the cells were
# rendering "--" even though the data existed (AAPL spot-check, 2026-05-08).


def test_ttm_2024_q4_roe(ratios: pd.DataFrame) -> None:
    # ni_ttm (2024 Q1..Q4) = 4M + 4.5M + 5M + 6.5M = 20M
    # equity at 2024-Q4 = total_assets - total_liabilities = 300M - 150M = 150M
    # ROE_TTM = 20M / 150M * 100 = 13.333...%
    assert _row(ratios, "2024-12-31", "ttm")["roe"] == pytest.approx(20 / 150 * 100)


def test_ttm_2024_q4_roa(ratios: pd.DataFrame) -> None:
    # ROA_TTM = ni_ttm / total_assets_at_period_end = 20M / 300M * 100 = 6.666...%
    assert _row(ratios, "2024-12-31", "ttm")["roa"] == pytest.approx(20 / 300 * 100)


def test_ttm_2024_q4_gross_margin(ratios: pd.DataFrame) -> None:
    # gross_profit_ttm = 17.5M + 18.5M + 19M + 20M = 75M
    # revenue_ttm = 35M + 37M + 38M + 40M = 150M
    # gross_margin_ttm = 75 / 150 * 100 = 50%
    assert _row(ratios, "2024-12-31", "ttm")["gross_margin_pct"] == pytest.approx(50.0)


def test_ttm_2024_q4_debt_to_equity(ratios: pd.DataFrame) -> None:
    # debt at 2024-Q4 = 75M; equity = 150M; D/E = 0.5
    assert _row(ratios, "2024-12-31", "ttm")["debt_to_equity"] == pytest.approx(0.5)


def test_ttm_2024_q4_current_ratio(ratios: pd.DataFrame) -> None:
    # current_assets at 2024-Q4 = 100M; current_liabilities = 40M; ratio = 2.5
    assert _row(ratios, "2024-12-31", "ttm")["current_ratio"] == pytest.approx(2.5)


def test_pe_not_meaningful_below_eps_threshold() -> None:
    """When EPS < $0.10 (N/M threshold), pe_ratio must be NaN even though
    net_income and TTM sum are positive."""
    df = pd.DataFrame(
        [
            {
                "period_end": pd.to_datetime("2024-12-31").date(),
                "period_type": "annual",
                "revenue": 10_000_000,
                "gross_profit": 5_000_000,
                "net_income": 50_000,  # EPS = 50k/1M = $0.05 < $0.10
                "total_assets": 100_000_000,
                "total_liabilities": 50_000_000,
                "current_assets": 40_000_000,
                "current_liabilities": 20_000_000,
                "free_cash_flow": 1_000_000,
                "ebitda": 2_000_000,
                "total_debt": 25_000_000,
                "cash_and_equivalents": 5_000_000,
                "shares_outstanding": 1_000_000,
                "market_cap": 0,
            }
        ]
    )
    result = compute_fundamental_ratios(df, latest_close=200.0)
    assert bool(pd.isna(result["pe_ratio"].iloc[0]))
