import hashlib
import logging
import time
from datetime import datetime
from time import struct_time
from typing import Any

import feedparser
import pandas as pd
from dagster import (
    AssetExecutionContext,
    Backoff,
    RetryPolicy,
    StaticPartitionsDefinition,
    asset,
)
from shared.tickers import TICKERS

from dagster_pipelines.news_feeds import ticker_feed_url
from dagster_pipelines.resources.clickhouse import ClickHouseResource

logger = logging.getLogger(__name__)

news_partitions = StaticPartitionsDefinition(TICKERS)

_USER_AGENT = "equity-data-agent/0.1 (RSS news ingest)"


def _url_hash(url: str) -> int:
    """Deterministic UInt64 hash of a URL for dedup.

    AC specifies `sipHash64`; Python stdlib has no sipHash. blake2b truncated to
    8 bytes is stronger and serves the same dedup purpose — collision-free per-URL
    key that ReplacingMergeTree keys its dedup on.
    """
    return int(hashlib.blake2b(url.encode("utf-8"), digest_size=8).hexdigest(), 16)


def _parse_published(entry: dict[str, Any]) -> datetime | None:
    """Extract a published/updated timestamp from a feedparser entry."""
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if isinstance(val, struct_time):
            return datetime(*val[:6])
    return None


def _entry_to_row(entry: dict[str, Any], ticker: str, source: str) -> dict[str, Any] | None:
    """Convert a feedparser entry to a news_raw row dict, or None if unusable."""
    url = (entry.get("link") or "").strip()
    headline = (entry.get("title") or "").strip()
    if not url or not headline:
        return None

    published_at = _parse_published(entry)
    if published_at is None:
        return None

    return {
        "id": _url_hash(url),
        "ticker": ticker,
        "headline": headline,
        "body": (entry.get("summary") or "").strip(),
        "source": source,
        "url": url,
        "published_at": published_at,
        "fetched_at": datetime.utcnow(),
    }


@asset(
    partitions_def=news_partitions,
    retry_policy=RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL),
    group_name="ingestion",
)
def news_raw(
    context: AssetExecutionContext,
    clickhouse: ClickHouseResource,
) -> None:
    """Fetch per-ticker RSS headlines from Yahoo Finance and upsert into equity_raw.news_raw.

    Partitioned by ticker. ReplacingMergeTree on (ticker, published_at, id) dedups
    on re-run via `id = hash(url)`. Transient feed errors (404, parse errors, DNS
    failures) are logged and return without raising so one bad feed doesn't fail
    the whole schedule tick.
    """
    ticker = context.partition_key
    url = ticker_feed_url(ticker)

    context.log.info("Fetching RSS for %s from %s", ticker, url)

    try:
        parsed = feedparser.parse(url, request_headers={"User-Agent": _USER_AGENT})
    except Exception as exc:
        context.log.warning("feedparser.parse raised for %s: %s — skipping", ticker, exc)
        return

    if parsed.bozo and not parsed.entries:
        context.log.warning(
            "Feed parse error for %s: %s — skipping",
            ticker,
            getattr(parsed, "bozo_exception", "unknown"),
        )
        return

    rows: list[dict[str, Any]] = []
    for entry in parsed.entries:
        row = _entry_to_row(entry, ticker=ticker, source="yahoo_finance")
        if row is not None:
            rows.append(row)

    if not rows:
        context.log.warning("No usable entries for %s — skipping", ticker)
        return

    df = pd.DataFrame(rows)
    df["id"] = df["id"].astype("uint64")

    cols = ["id", "ticker", "headline", "body", "source", "url", "published_at", "fetched_at"]
    df = pd.DataFrame(df[cols])

    clickhouse.insert_df("equity_raw.news_raw", df)
    context.log.info("Inserted %d news rows for %s", len(df), ticker)

    time.sleep(1.5)
