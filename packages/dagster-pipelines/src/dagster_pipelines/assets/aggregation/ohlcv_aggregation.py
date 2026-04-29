import logging
from datetime import date, datetime, timedelta

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

# Aggregation stays on TICKERS — Dagster requires every asset in
# ``ohlcv_downstream_job`` to share a partition def, and indicators /
# fundamental_summary partition on TICKERS. Benchmark tickers (SPY) are
# OHLCV-only per the QNT-134 spec, so weekly/monthly is intentionally not
# computed for them.
ticker_partitions = StaticPartitionsDefinition(TICKERS)


def _aggregate_ohlcv(df: pd.DataFrame, period_col: str) -> pd.DataFrame:
    """Aggregate daily OHLCV bars into a coarser timeframe.

    Expects df sorted by date with a pre-computed period column (week_start or month_start).
    Returns one row per period: open=first, high=max, low=min, close=last,
    adj_close=last, volume=sum.
    """
    df = df.sort_values("date")

    agg = (
        df.groupby(period_col, sort=True)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            adj_close=("adj_close", "last"),
            volume=("volume", "sum"),
        )
        .reset_index()
    )

    return agg


@asset(
    deps=["ohlcv_raw"],
    partitions_def=ticker_partitions,
    retry_policy=RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL),
    group_name="derived",
)
def ohlcv_weekly(
    context: AssetExecutionContext,
    clickhouse: ClickHouseResource,
) -> None:
    """Aggregate daily OHLCV bars into weekly (Monday-based) bars.

    Reads from equity_raw.ohlcv_raw, writes to equity_derived.ohlcv_weekly.
    Skips the current incomplete week to avoid partial bars.
    """
    ticker = context.partition_key

    df = clickhouse.query_df(
        "SELECT date, open, high, low, close, adj_close, volume "
        "FROM equity_raw.ohlcv_raw FINAL "
        "WHERE ticker = {ticker:String} "
        "ORDER BY date",
        parameters={"ticker": ticker},
    )

    if df.empty:
        context.log.warning("No ohlcv_raw data for %s — skipping weekly aggregation", ticker)
        return

    df["date"] = pd.to_datetime(df["date"]).dt.date

    # Compute Monday of each week
    df["week_start"] = df["date"].apply(lambda d: d - timedelta(days=d.weekday()))

    # Skip the current incomplete week
    today = date.today()
    current_monday = today - timedelta(days=today.weekday())
    df = pd.DataFrame(df[df["week_start"] < current_monday])

    if df.empty:
        context.log.warning("No complete weeks for %s — skipping", ticker)
        return

    weekly = _aggregate_ohlcv(df, "week_start")
    weekly["ticker"] = ticker
    weekly["computed_at"] = datetime.utcnow()
    weekly["volume"] = weekly["volume"].astype("int64")

    cols = [
        "ticker",
        "week_start",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
        "computed_at",
    ]
    weekly = pd.DataFrame(weekly[cols])

    clickhouse.insert_df("equity_derived.ohlcv_weekly", weekly)
    context.log.info("Inserted %d weekly bars for %s", len(weekly), ticker)


@asset(
    deps=["ohlcv_raw"],
    partitions_def=ticker_partitions,
    retry_policy=RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL),
    group_name="derived",
)
def ohlcv_monthly(
    context: AssetExecutionContext,
    clickhouse: ClickHouseResource,
) -> None:
    """Aggregate daily OHLCV bars into monthly bars.

    Reads from equity_raw.ohlcv_raw, writes to equity_derived.ohlcv_monthly.
    Skips the current incomplete month to avoid partial bars.
    """
    ticker = context.partition_key

    df = clickhouse.query_df(
        "SELECT date, open, high, low, close, adj_close, volume "
        "FROM equity_raw.ohlcv_raw FINAL "
        "WHERE ticker = {ticker:String} "
        "ORDER BY date",
        parameters={"ticker": ticker},
    )

    if df.empty:
        context.log.warning("No ohlcv_raw data for %s — skipping monthly aggregation", ticker)
        return

    df["date"] = pd.to_datetime(df["date"]).dt.date

    # Compute first day of each month
    df["month_start"] = df["date"].apply(lambda d: d.replace(day=1))

    # Skip the current incomplete month
    today = date.today()
    current_month_start = today.replace(day=1)
    df = pd.DataFrame(df[df["month_start"] < current_month_start])

    if df.empty:
        context.log.warning("No complete months for %s — skipping", ticker)
        return

    monthly = _aggregate_ohlcv(df, "month_start")
    monthly["ticker"] = ticker
    monthly["computed_at"] = datetime.utcnow()
    monthly["volume"] = monthly["volume"].astype("int64")

    cols = [
        "ticker",
        "month_start",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
        "computed_at",
    ]
    monthly = pd.DataFrame(monthly[cols])

    clickhouse.insert_df("equity_derived.ohlcv_monthly", monthly)
    context.log.info("Inserted %d monthly bars for %s", len(monthly), ticker)
