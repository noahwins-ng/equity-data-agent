import logging
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from dagster import (
    AssetExecutionContext,
    Backoff,
    RetryPolicy,
    StaticPartitionsDefinition,
    asset,
)
from shared.tickers import TICKERS

from dagster_pipelines.resources.clickhouse import ClickHouseResource

logger = logging.getLogger(__name__)

ticker_partitions = StaticPartitionsDefinition(TICKERS)


# EPS floor below which P/E ratio is considered "not meaningful" (N/M).
# Chosen so max P/E for a $1,000 stock stays near the 10,000 check band.
_EPS_NM_THRESHOLD = 0.10


def _safe_divide(numerator: Any, denominator: Any) -> pd.Series:
    """Divide two Series, returning NaN where denominator is zero or NaN."""
    return numerator / denominator.replace(0, np.nan)


def _yoy_growth(df: pd.DataFrame, col: str, periods: int) -> pd.Series:
    """Compute year-over-year growth percentage.

    For quarterly data, periods=4 (same quarter last year).
    For annual data, periods=1 (previous year).
    Returns NaN when prior period is unavailable or zero.
    """
    prior = df[col].shift(periods)
    return _safe_divide(df[col] - prior, prior.abs()) * 100


def compute_fundamental_ratios(
    fundamentals_df: pd.DataFrame,
    latest_close: float,
) -> pd.DataFrame:
    """Compute 15 fundamental ratios from raw financial data.

    Price-based ratios (P/E, P/B, P/S, FCF yield) use latest_close from ohlcv_raw
    to ensure current market pricing.
    """
    df = fundamentals_df.copy().sort_values("period_end")

    shares = df["shares_outstanding"].replace(0, np.nan)
    market_cap = latest_close * shares
    book_value = df["total_assets"] - df["total_liabilities"]
    equity = book_value.replace(0, np.nan)

    # Valuation
    df["eps"] = _safe_divide(df["net_income"], shares)
    df["pe_ratio"] = _safe_divide(market_cap, df["net_income"])
    # Financial convention: P/E is "N/M" (not meaningful) when earnings are
    # near zero — the ratio is arithmetically valid but not comparable.
    df.loc[df["eps"].abs() < _EPS_NM_THRESHOLD, "pe_ratio"] = np.nan
    ev = market_cap + df["total_debt"] - df["cash_and_equivalents"]
    df["ev_ebitda"] = _safe_divide(ev, df["ebitda"].replace(0, np.nan))
    df["price_to_book"] = _safe_divide(market_cap, equity)
    df["price_to_sales"] = _safe_divide(market_cap, df["revenue"].replace(0, np.nan))

    # Profitability
    df["net_margin_pct"] = _safe_divide(df["net_income"], df["revenue"].replace(0, np.nan)) * 100
    df["gross_margin_pct"] = (
        _safe_divide(df["gross_profit"], df["revenue"].replace(0, np.nan)) * 100
    )
    df["roe"] = _safe_divide(df["net_income"], equity) * 100
    df["roa"] = _safe_divide(df["net_income"], df["total_assets"].replace(0, np.nan)) * 100

    # Cash
    df["fcf_yield"] = _safe_divide(df["free_cash_flow"], market_cap) * 100

    # Leverage
    df["debt_to_equity"] = _safe_divide(df["total_debt"], equity)

    # Liquidity
    df["current_ratio"] = _safe_divide(
        df["current_assets"], df["current_liabilities"].replace(0, np.nan)
    )

    # Growth (YoY) — compute per period_type group
    yoy_cols = [
        ("revenue", "revenue_yoy_pct"),
        ("net_income", "net_income_yoy_pct"),
        ("free_cash_flow", "fcf_yoy_pct"),
    ]
    for src_col, dst_col in yoy_cols:
        df[dst_col] = np.nan

    for period_type, group in df.groupby("period_type"):
        periods = 4 if period_type == "quarterly" else 1
        for src_col, dst_col in yoy_cols:
            yoy = _yoy_growth(group, src_col, periods)
            df.loc[group.index, dst_col] = yoy

    return df


@asset(
    deps=["fundamentals", "ohlcv_raw"],
    partitions_def=ticker_partitions,
    retry_policy=RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL),
    group_name="derived",
)
def fundamental_summary(
    context: AssetExecutionContext,
    clickhouse: ClickHouseResource,
) -> None:
    """Compute fundamental ratios from raw financial data + latest close price.

    Reads from equity_raw.fundamentals and equity_raw.ohlcv_raw.
    Writes to equity_derived.fundamental_summary.
    Cross-dependency: price-based ratios (P/E, P/B, P/S, FCF yield) require
    latest close price from ohlcv_raw.
    """
    ticker = context.partition_key

    # Fetch fundamentals
    fund_df = clickhouse.query_df(
        "SELECT period_end, period_type, revenue, gross_profit, net_income, "
        "total_assets, total_liabilities, current_assets, current_liabilities, "
        "free_cash_flow, ebitda, total_debt, cash_and_equivalents, "
        "shares_outstanding, market_cap "
        "FROM equity_raw.fundamentals FINAL "
        "WHERE ticker = {ticker:String} "
        "ORDER BY period_end",
        parameters={"ticker": ticker},
    )

    if fund_df.empty:
        context.log.warning("No fundamentals data for %s — skipping", ticker)
        return

    # Fetch latest close price from ohlcv_raw
    price_df = clickhouse.query_df(
        "SELECT close "
        "FROM equity_raw.ohlcv_raw FINAL "
        "WHERE ticker = {ticker:String} "
        "ORDER BY date DESC LIMIT 1",
        parameters={"ticker": ticker},
    )

    if price_df.empty:
        context.log.warning("No ohlcv_raw data for %s — skipping (need close price)", ticker)
        return

    latest_close = float(price_df["close"].iloc[0])
    context.log.info("Using latest close price %.2f for %s", latest_close, ticker)

    fund_df["period_end"] = pd.to_datetime(fund_df["period_end"]).dt.date

    result = compute_fundamental_ratios(fund_df, latest_close)
    result["ticker"] = ticker
    result["computed_at"] = datetime.utcnow()

    output_cols = [
        "ticker",
        "period_end",
        "period_type",
        "pe_ratio",
        "ev_ebitda",
        "price_to_book",
        "price_to_sales",
        "eps",
        "revenue_yoy_pct",
        "net_income_yoy_pct",
        "fcf_yoy_pct",
        "net_margin_pct",
        "gross_margin_pct",
        "roe",
        "roa",
        "fcf_yield",
        "debt_to_equity",
        "current_ratio",
        "computed_at",
    ]
    output = pd.DataFrame(result[output_cols])

    clickhouse.insert_df("equity_derived.fundamental_summary", output)
    context.log.info("Inserted %d fundamental summary rows for %s", len(output), ticker)
