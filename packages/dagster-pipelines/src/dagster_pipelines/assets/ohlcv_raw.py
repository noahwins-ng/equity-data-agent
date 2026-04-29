import logging
import time
from datetime import datetime

import pandas as pd
import yfinance as yf
from dagster import (
    AssetExecutionContext,
    Backoff,
    Config,
    RetryPolicy,
    StaticPartitionsDefinition,
    asset,
)
from shared.tickers import ALL_OHLCV_TICKERS

from dagster_pipelines.resources.clickhouse import ClickHouseResource

logger = logging.getLogger(__name__)

# Includes BENCHMARK_TICKERS (SPY) so the design v2 watchlist row can render
# against the index without each ticker page paying for an extra fetch.
ohlcv_partitions = StaticPartitionsDefinition(ALL_OHLCV_TICKERS)


class OHLCVConfig(Config):
    period: str = "2y"  # backfill default; use "5d" for incremental runs


@asset(
    partitions_def=ohlcv_partitions,
    retry_policy=RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL),
    group_name="ingestion",
)
def ohlcv_raw(
    context: AssetExecutionContext,
    config: OHLCVConfig,
    clickhouse: ClickHouseResource,
) -> None:
    """Fetch daily OHLCV from yfinance and upsert into equity_raw.ohlcv_raw.

    Partitioned by ticker. ReplacingMergeTree deduplicates on re-run.
    Backfill: period="2y". Incremental (daily schedule): period="5d".
    """
    ticker = context.partition_key

    context.log.info("Fetching OHLCV for %s with period=%s", ticker, config.period)

    try:
        raw = yf.download(ticker, period=config.period, auto_adjust=False, progress=False)
    except Exception as exc:
        msg = str(exc).lower()
        if "429" in msg or "too many requests" in msg or "rate limit" in msg:
            raise  # bubble up to trigger Dagster retry with exponential backoff
        context.log.warning("yfinance fetch failed for %s: %s — skipping", ticker, exc)
        return

    if raw is None or raw.empty:
        context.log.warning("yfinance returned empty DataFrame for %s — skipping", ticker)
        return

    df: pd.DataFrame = raw.copy()

    # Flatten MultiIndex columns (yfinance ≥ 0.2.x single-ticker download)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] for col in df.columns]

    df = df.reset_index()
    df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]
    # Columns after normalisation: date, open, high, low, close, adj_close, volume

    df["ticker"] = ticker
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["fetched_at"] = datetime.utcnow()
    df["volume"] = df["volume"].astype("int64")

    cols = ["ticker", "date", "open", "high", "low", "close", "adj_close", "volume", "fetched_at"]
    df = pd.DataFrame(df[cols])

    clickhouse.insert_df("equity_raw.ohlcv_raw", df)
    context.log.info("Inserted %d rows for %s", len(df), ticker)

    # Rate limiting: avoid hammering yfinance between partition runs
    time.sleep(1.5)
