import logging
from datetime import datetime
from typing import Any, cast

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
    """Compute fundamental ratios from raw financial data.

    Price-based ratios (P/E, P/B, P/S, FCF yield) use latest_close from ohlcv_raw
    to ensure current market pricing. Beyond the original 15 ratios this also
    emits ``ebitda_margin_pct`` (substitute for operating margin since yfinance
    doesn't expose EBIT), bps deltas for gross/net margin YoY, and rolling-4Q
    TTM rows under ``period_type='ttm'`` carrying revenue/net_income/fcf/eps as
    raw sums plus the same ratio set computed off the rolled-up base.
    """
    df = fundamentals_df.copy().sort_values("period_end")

    shares = df["shares_outstanding"].replace(0, np.nan)
    market_cap = latest_close * shares
    book_value = df["total_assets"] - df["total_liabilities"]
    equity = book_value.replace(0, np.nan)

    # Valuation
    df["eps"] = _safe_divide(df["net_income"], shares)
    # P/E uses TTM (trailing twelve months) earnings on quarterly rows — a
    # single quarter's net_income divided by full market cap inflates the
    # ratio ~4x. Annual rows already carry full-year net_income. Asset is
    # ticker-partitioned, so df is one ticker and rolling-sum over the
    # quarterly slice is the correct TTM.
    q_mask = df["period_type"] == "quarterly"
    ni_for_pe = df["net_income"].copy()
    ni_for_pe.loc[q_mask] = df.loc[q_mask, "net_income"].rolling(window=4, min_periods=4).sum()
    df["pe_ratio"] = _safe_divide(market_cap, ni_for_pe.replace(0, np.nan))
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
    # EBITDA margin: yfinance only exposes a single point-in-time TTM value
    # at `info.ebitda` (same number stamped onto every row by the ingest), so
    # dividing by a single quarter's revenue produces a ~4× inflated ratio.
    # We only emit ebitda_margin_pct on TTM rows where the denominator is
    # also TTM revenue (see _build_ttm_rows). Quarterly + annual rows leave
    # this column NULL — semantically ambiguous values are worse than absent
    # ones for a downstream agent reading the report.
    df["ebitda_margin_pct"] = np.nan
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
    for _src_col, dst_col in yoy_cols:
        df[dst_col] = np.nan
    df["gross_margin_bps_yoy"] = np.nan
    df["net_margin_bps_yoy"] = np.nan

    for period_type, group in df.groupby("period_type"):
        periods = 4 if period_type == "quarterly" else 1
        for src_col, dst_col in yoy_cols:
            yoy = _yoy_growth(group, src_col, periods)
            df.loc[group.index, dst_col] = yoy
        # Margin deltas in basis points: 1 percentage point = 100 bps. Take
        # absolute differences vs prior-year same period, no division.
        for margin_col, dst_col in (
            ("gross_margin_pct", "gross_margin_bps_yoy"),
            ("net_margin_pct", "net_margin_bps_yoy"),
        ):
            prior = group[margin_col].shift(periods)
            df.loc[group.index, dst_col] = (group[margin_col] - prior) * 100

    # TTM rows — emit one synthetic row per quarterly period_end carrying the
    # rolling-4Q sums of revenue/net_income/fcf and ratios computed on those
    # sums. Annual rows already represent a 4Q snapshot, so TTM is generated
    # only from the quarterly slice.
    ttm = _build_ttm_rows(df, latest_close)
    if not ttm.empty:
        df = pd.concat([df, ttm], ignore_index=True)

    return df


def _build_ttm_rows(
    df: pd.DataFrame,
    latest_close: float,
) -> pd.DataFrame:
    """Build period_type='ttm' rows from the quarterly slice of ``df``.

    Sums revenue/net_income/free_cash_flow over a trailing-4-quarter window;
    derives eps_ttm from rolling NI. P/E (TTM) is recomputed against the same
    rolling sum to match what consumers will read off the TTM row directly.
    Returns the new rows; caller appends.
    """
    # Reset to a contiguous integer index so the rolling outputs and the boolean
    # `valid` mask align by position regardless of any reset_index a future
    # caller may apply upstream — the original code relied on q's inherited
    # df index, which would silently misalign if df were ever reset.
    q = df[df["period_type"] == "quarterly"].copy().reset_index(drop=True)
    if q.empty:
        return pd.DataFrame()

    rev_ttm = cast(pd.Series, cast(pd.Series, q["revenue"]).rolling(window=4, min_periods=4).sum())
    ni_ttm = cast(
        pd.Series, cast(pd.Series, q["net_income"]).rolling(window=4, min_periods=4).sum()
    )
    fcf_ttm = cast(
        pd.Series, cast(pd.Series, q["free_cash_flow"]).rolling(window=4, min_periods=4).sum()
    )

    valid = rev_ttm.notna() & ni_ttm.notna() & fcf_ttm.notna()
    if not bool(valid.any()):
        return pd.DataFrame()

    # Per-share rollup uses the matching quarter's shares_outstanding so EPS_TTM
    # tracks the share count at the period end (vs latest), matching the EPS
    # convention SEC filings use for "EPS, trailing twelve months".
    shares_q = cast(pd.Series, q["shares_outstanding"]).replace(0, np.nan)
    eps_ttm = cast(pd.Series, ni_ttm / shares_q)
    market_cap_q = cast(pd.Series, latest_close * shares_q)
    pe_ttm = cast(pd.Series, market_cap_q / ni_ttm.replace(0, np.nan))
    ps_ttm = cast(pd.Series, market_cap_q / rev_ttm.replace(0, np.nan))
    nm_ttm = cast(pd.Series, ni_ttm / rev_ttm.replace(0, np.nan) * 100)
    fcf_yield_ttm = cast(pd.Series, fcf_ttm / market_cap_q * 100)
    # EBITDA margin only makes sense as a TTM ratio because yfinance hands
    # us a single TTM EBITDA figure (point-in-time, same on every ingested
    # row). TTM revenue is the matching denominator, so this is the one
    # row_type where ebitda_margin_pct is semantically defined.
    ebitda_q = cast(pd.Series, q["ebitda"]).replace(0, np.nan)
    ebitda_margin_ttm = cast(pd.Series, ebitda_q / rev_ttm.replace(0, np.nan) * 100)

    payload: dict[str, Any] = {
        "period_end": q["period_end"],
        "period_type": "ttm",
        "revenue_ttm": rev_ttm,
        "net_income_ttm": ni_ttm,
        "fcf_ttm": fcf_ttm,
        "eps": eps_ttm,
        "pe_ratio": pe_ttm,
        "price_to_sales": ps_ttm,
        "net_margin_pct": nm_ttm,
        "fcf_yield": fcf_yield_ttm,
        "ebitda_margin_pct": ebitda_margin_ttm,
    }
    # The asset always sets `ticker` AFTER calling this helper (one-ticker per
    # partition), so the test fixture omits the column. Only carry it through
    # when it's already present in the input frame.
    if "ticker" in q.columns:
        payload["ticker"] = q["ticker"]
    out = pd.DataFrame(payload).loc[valid]

    # N/M when EPS_TTM is near-zero, mirroring the quarterly convention.
    out.loc[out["eps"].abs() < _EPS_NM_THRESHOLD, "pe_ratio"] = np.nan
    return out


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
        "gross_margin_bps_yoy",
        "net_margin_bps_yoy",
        "net_margin_pct",
        "gross_margin_pct",
        "ebitda_margin_pct",
        "roe",
        "roa",
        "fcf_yield",
        "debt_to_equity",
        "current_ratio",
        "revenue_ttm",
        "net_income_ttm",
        "fcf_ttm",
        "computed_at",
    ]
    # TTM rows append columns that don't exist on quarterly/annual rows; ensure
    # all output columns are present so the insert dataframe is well-formed.
    for col in output_cols:
        if col not in result.columns:
            result[col] = pd.NA
    output = pd.DataFrame(result[output_cols])

    clickhouse.insert_df("equity_derived.fundamental_summary", output)
    context.log.info("Inserted %d fundamental summary rows for %s", len(output), ticker)
