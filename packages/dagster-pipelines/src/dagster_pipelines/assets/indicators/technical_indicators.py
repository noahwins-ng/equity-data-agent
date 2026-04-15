import logging
from datetime import datetime

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


def compute_indicators(df: pd.DataFrame, price_col: str = "adj_close") -> pd.DataFrame:
    """Compute technical indicators from OHLCV data.

    All calculations use adj_close to avoid false signals at stock split boundaries.
    Returns a DataFrame with indicator columns added. NaN values represent warm-up
    periods where insufficient data exists for the indicator.

    Indicators: RSI-14, SMA-20, SMA-50, EMA-12, EMA-26, MACD(12/26/9), BB(20,2).
    """
    price = df[price_col]

    # SMA
    df["sma_20"] = price.rolling(window=20, min_periods=20).mean()
    df["sma_50"] = price.rolling(window=50, min_periods=50).mean()

    # EMA
    df["ema_12"] = price.ewm(span=12, min_periods=12, adjust=False).mean()
    df["ema_26"] = price.ewm(span=26, min_periods=26, adjust=False).mean()

    # RSI-14 (Wilder's smoothing: alpha = 1/14)
    delta = price.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi_14"] = 100 - (100 / (1 + rs))

    # MACD (12/26/9)
    df["macd"] = df["ema_12"] - df["ema_26"]
    df["macd_signal"] = df["macd"].ewm(span=9, min_periods=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # Bollinger Bands (20, 2)
    df["bb_middle"] = df["sma_20"]
    bb_std = price.rolling(window=20, min_periods=20).std()
    df["bb_upper"] = df["bb_middle"] + 2 * bb_std
    df["bb_lower"] = df["bb_middle"] - 2 * bb_std

    return df


@asset(
    deps=["ohlcv_raw"],
    partitions_def=ticker_partitions,
    retry_policy=RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL),
    group_name="derived",
)
def technical_indicators_daily(
    context: AssetExecutionContext,
    clickhouse: ClickHouseResource,
) -> None:
    """Compute technical indicators from daily OHLCV bars.

    Reads adj_close from equity_raw.ohlcv_raw, computes RSI-14, SMA-20/50,
    EMA-12/26, MACD(12/26/9), BB(20,2). Writes to equity_derived.technical_indicators_daily.
    """
    ticker = context.partition_key

    df = clickhouse.query_df(
        "SELECT date, adj_close "
        "FROM equity_raw.ohlcv_raw FINAL "
        "WHERE ticker = {ticker:String} "
        "ORDER BY date",
        parameters={"ticker": ticker},
    )

    if df.empty:
        context.log.warning("No ohlcv_raw data for %s — skipping daily indicators", ticker)
        return

    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = compute_indicators(df)

    df["ticker"] = ticker
    df["computed_at"] = datetime.utcnow()

    indicator_cols = [
        "ticker",
        "date",
        "sma_20",
        "sma_50",
        "ema_12",
        "ema_26",
        "rsi_14",
        "macd",
        "macd_signal",
        "macd_hist",
        "bb_upper",
        "bb_middle",
        "bb_lower",
        "computed_at",
    ]
    result = pd.DataFrame(df[indicator_cols])

    clickhouse.insert_df("equity_derived.technical_indicators_daily", result)
    context.log.info("Inserted %d daily indicator rows for %s", len(result), ticker)


@asset(
    deps=["ohlcv_weekly"],
    partitions_def=ticker_partitions,
    retry_policy=RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL),
    group_name="derived",
)
def technical_indicators_weekly(
    context: AssetExecutionContext,
    clickhouse: ClickHouseResource,
) -> None:
    """Compute technical indicators from weekly OHLCV bars.

    Reads adj_close from equity_derived.ohlcv_weekly, writes to
    equity_derived.technical_indicators_weekly.
    """
    ticker = context.partition_key

    df = clickhouse.query_df(
        "SELECT week_start, adj_close "
        "FROM equity_derived.ohlcv_weekly FINAL "
        "WHERE ticker = {ticker:String} "
        "ORDER BY week_start",
        parameters={"ticker": ticker},
    )

    if df.empty:
        context.log.warning("No ohlcv_weekly data for %s — skipping weekly indicators", ticker)
        return

    df["week_start"] = pd.to_datetime(df["week_start"]).dt.date
    df = compute_indicators(df)

    df["ticker"] = ticker
    df["computed_at"] = datetime.utcnow()

    indicator_cols = [
        "ticker",
        "week_start",
        "sma_20",
        "sma_50",
        "ema_12",
        "ema_26",
        "rsi_14",
        "macd",
        "macd_signal",
        "macd_hist",
        "bb_upper",
        "bb_middle",
        "bb_lower",
        "computed_at",
    ]
    result = pd.DataFrame(df[indicator_cols])

    clickhouse.insert_df("equity_derived.technical_indicators_weekly", result)
    context.log.info("Inserted %d weekly indicator rows for %s", len(result), ticker)


@asset(
    deps=["ohlcv_monthly"],
    partitions_def=ticker_partitions,
    retry_policy=RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL),
    group_name="derived",
)
def technical_indicators_monthly(
    context: AssetExecutionContext,
    clickhouse: ClickHouseResource,
) -> None:
    """Compute technical indicators from monthly OHLCV bars.

    Reads adj_close from equity_derived.ohlcv_monthly, writes to
    equity_derived.technical_indicators_monthly.
    """
    ticker = context.partition_key

    df = clickhouse.query_df(
        "SELECT month_start, adj_close "
        "FROM equity_derived.ohlcv_monthly FINAL "
        "WHERE ticker = {ticker:String} "
        "ORDER BY month_start",
        parameters={"ticker": ticker},
    )

    if df.empty:
        context.log.warning("No ohlcv_monthly data for %s — skipping monthly indicators", ticker)
        return

    df["month_start"] = pd.to_datetime(df["month_start"]).dt.date
    df = compute_indicators(df)

    df["ticker"] = ticker
    df["computed_at"] = datetime.utcnow()

    indicator_cols = [
        "ticker",
        "month_start",
        "sma_20",
        "sma_50",
        "ema_12",
        "ema_26",
        "rsi_14",
        "macd",
        "macd_signal",
        "macd_hist",
        "bb_upper",
        "bb_middle",
        "bb_lower",
        "computed_at",
    ]
    result = pd.DataFrame(df[indicator_cols])

    clickhouse.insert_df("equity_derived.technical_indicators_monthly", result)
    context.log.info("Inserted %d monthly indicator rows for %s", len(result), ticker)
