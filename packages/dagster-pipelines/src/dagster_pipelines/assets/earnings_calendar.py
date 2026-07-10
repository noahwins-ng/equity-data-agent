"""Dagster asset: next-earnings-date ingest per ticker via the yfinance calendar (QNT-357).

The warehouse's only dated forward catalyst. For each portfolio ticker, reads
``yf.Ticker(ticker).calendar['Earnings Date']`` (a list of one-or-two estimated
dates) and upserts the earliest still-future date into
equity_raw.earnings_calendar. ReplacingMergeTree on ``ticker`` (versioned by
``fetched_at``) keeps re-runs idempotent — each weekly poll replaces the single
row with the freshest estimate. Downstream, the company report's CONTEXT NOW
block renders the date verbatim (ADR-012) so the exploration path can name the
one upcoming event an analyst would lead with.

Note: this module deliberately omits ``from __future__ import annotations`` so
Dagster's runtime introspection works, matching the sibling ingest assets.
"""

import logging
import time
from datetime import UTC, date, datetime

import pandas as pd
import yfinance as yf
from dagster import (
    AssetExecutionContext,
    Backoff,
    Jitter,
    RetryPolicy,
    StaticPartitionsDefinition,
    asset,
)
from shared.tickers import TICKERS

from dagster_pipelines.resources.clickhouse import ClickHouseResource
from dagster_pipelines.retry_helpers import retry_after_seconds_from_exception

logger = logging.getLogger(__name__)

earnings_calendar_partitions = StaticPartitionsDefinition(TICKERS)


def _next_future_earnings_date(earnings_dates: object, today: date) -> date | None:
    """Return the earliest earnings date >= today, or None.

    yfinance's ``calendar['Earnings Date']`` is a list of ``datetime.date`` — one
    date when the release is confirmed, or two bounding an estimated window. We
    keep the earliest date that has not already passed so the report surfaces the
    genuinely upcoming catalyst; an all-past list (stale calendar between the
    release and yfinance refreshing its estimate) yields None and the ticker is
    skipped rather than landing a past date the asset check would reject.
    """
    if not isinstance(earnings_dates, list):
        return None
    future = sorted(d for d in earnings_dates if isinstance(d, date) and d >= today)
    return future[0] if future else None


@asset(
    partitions_def=earnings_calendar_partitions,
    retry_policy=RetryPolicy(
        max_retries=3,
        delay=30,
        backoff=Backoff.EXPONENTIAL,
        jitter=Jitter.PLUS_MINUS,
    ),
    group_name="ingestion",
)
def earnings_calendar(
    context: AssetExecutionContext,
    clickhouse: ClickHouseResource,
) -> None:
    """Fetch the next scheduled earnings date from yfinance into equity_raw.earnings_calendar.

    Partitioned by ticker. ReplacingMergeTree deduplicates on re-run. A ticker
    whose calendar has no future earnings date is skipped (no row written).
    """
    ticker = context.partition_key

    context.log.info("Fetching earnings calendar for %s", ticker)

    try:
        calendar = yf.Ticker(ticker).calendar or {}
    except Exception as exc:
        msg = str(exc).lower()
        if "429" in msg or "too many requests" in msg or "rate limit" in msg:
            # Same Retry-After handling as the other yfinance call sites
            # (ohlcv_raw / fundamentals) so all three back off identically.
            wait = retry_after_seconds_from_exception(exc)
            if wait is not None and wait > 0:
                context.log.info(
                    "yfinance 429 for %s — Retry-After=%.1fs; sleeping before re-raising",
                    ticker,
                    wait,
                )
                time.sleep(wait)
            raise
        context.log.warning("yfinance calendar(%s) failed: %s — skipping", ticker, exc)
        return

    next_date = _next_future_earnings_date(calendar.get("Earnings Date"), date.today())
    if next_date is None:
        context.log.warning(
            "No future earnings date for %s (calendar=%r) — skipping",
            ticker,
            calendar.get("Earnings Date"),
        )
        return

    df = pd.DataFrame(
        [
            {
                "ticker": ticker,
                "next_earnings_date": next_date,
                "fetched_at": datetime.now(UTC).replace(tzinfo=None),
            }
        ]
    )
    clickhouse.insert_df("equity_raw.earnings_calendar", df)
    context.log.info("Inserted next earnings date %s for %s", next_date.isoformat(), ticker)

    # Rate limiting — one yfinance request per partition, same courtesy delay as
    # the sibling yfinance assets.
    time.sleep(1.5)
