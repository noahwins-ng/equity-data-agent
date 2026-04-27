"""Dagster asset: per-ticker news ingest via Finnhub /company-news (QNT-141).

Replaces the prior Yahoo Finance RSS path. Per ADR-015, Finnhub gives
per-publisher attribution + article images + a 1y historical backfill — RSS
gave none of those. The downstream classifier asset (QNT-131) reads
``sentiment_label = 'pending'`` rows; rows insert here with that default and
flip when the classifier runs. See ADR-015 §Decision for the full topology.

Cross-store identity (per ADR-015 §"Topology"):
    Finnhub url -> news_raw (ticker, published_at, id) where id = blake2b(url)
    one row per (ticker, url) pair — cross-mentioned URLs land as N rows
    matching the QNT-120 Qdrant point-id namespacing convention.

Note: this module deliberately omits `from __future__ import annotations` so
Dagster's runtime introspection on `NewsRawConfig` works (matches the pattern
in ohlcv_raw.py).
"""

import hashlib
import logging
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
from dagster import (
    AssetExecutionContext,
    Backoff,
    Config,
    RetryPolicy,
    StaticPartitionsDefinition,
    asset,
)
from shared.tickers import TICKERS

from dagster_pipelines.news_feeds import fetch_company_news
from dagster_pipelines.resources.clickhouse import ClickHouseResource

logger = logging.getLogger(__name__)

news_partitions = StaticPartitionsDefinition(TICKERS)

# Steady-state lookback window. 7 days matches the news_embeddings re-embed
# window in news_embeddings.py (and gives a buffer over the 4h schedule), so
# a single missed tick doesn't leave gaps. Backfill overrides via NewsRawConfig.
_DEFAULT_LOOKBACK_DAYS = 7

# Inter-call sleep, identical to the prior RSS path. Finnhub's free tier is
# 60 RPM; with 10 partitions × ~1 call each, we're nowhere near that ceiling
# even without sleep, but the cushion stays in place to keep us a polite
# client and to absorb partition-coordinator burst when backfilling.
_INTER_CALL_SLEEP_SECONDS = 1.5


class NewsRawConfig(Config):
    """Per-run config for news_raw.

    ``lookback_days`` controls the [today - N, today] window passed to Finnhub.
    Default 7 matches the news_embeddings re-embed window. Backfill jobs
    override per-partition (e.g. lookback_days=365 for the 1y first-run
    backfill, or chunked 30-day windows during cutover).
    """

    lookback_days: int = _DEFAULT_LOOKBACK_DAYS


def _url_hash(url: str) -> int:
    """Deterministic UInt64 hash of a URL for dedup.

    Unchanged from the RSS path. ClickHouse ReplacingMergeTree dedups on
    ``ORDER BY (ticker, published_at, id)``; the same ``id`` for the same URL
    keeps re-runs idempotent. Qdrant point IDs are namespaced as
    ``blake2b(f"{ticker}:{id}")`` per QNT-120, so cross-mentioned URLs still
    materialise as N points (N tickers).
    """
    return int(hashlib.blake2b(url.encode("utf-8"), digest_size=8).hexdigest(), 16)


def _article_to_row(article: dict[str, Any], ticker: str) -> dict[str, Any] | None:
    """Convert a Finnhub /company-news article dict to a news_raw row.

    Returns None for unusable rows (no URL, no headline, no datetime). Empty
    image URLs are kept — design v2 reserves the thumbnail slot but renders a
    placeholder when ``image_url`` is empty.
    """
    url = (article.get("url") or "").strip()
    headline = (article.get("headline") or "").strip()
    if not url or not headline:
        return None

    epoch = article.get("datetime")
    if not isinstance(epoch, int) or epoch <= 0:
        return None
    published_at = datetime.fromtimestamp(epoch, tz=UTC).replace(tzinfo=None)

    return {
        "id": _url_hash(url),
        "ticker": ticker,
        "headline": headline,
        "body": (article.get("summary") or "").strip(),
        # Ingest provenance: identifies which fetch path produced this row,
        # not the originating outlet. The latter lives in publisher_name.
        "source": "finnhub",
        "url": url,
        "published_at": published_at,
        "fetched_at": datetime.now(UTC).replace(tzinfo=None),
        "publisher_name": (article.get("source") or "").strip(),
        "image_url": (article.get("image") or "").strip(),
        # ADR-015 topology (a): rows land 'pending' so the QNT-131 classifier
        # asset has rows to operate on. The 24h pending-age asset check
        # bounds the worst case observable to readers.
        "sentiment_label": "pending",
    }


@asset(
    partitions_def=news_partitions,
    retry_policy=RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL),
    group_name="ingestion",
)
def news_raw(
    context: AssetExecutionContext,
    config: NewsRawConfig,
    clickhouse: ClickHouseResource,
) -> None:
    """Fetch per-ticker headlines from Finnhub and upsert into equity_raw.news_raw.

    Partitioned by ticker. ReplacingMergeTree on ``(ticker, published_at, id)``
    dedups on re-run via ``id = blake2b(url)``. Transient HTTP errors are
    handled by the asset's RetryPolicy (3 retries, exponential backoff).
    A missing FINNHUB_API_KEY raises ``FinnhubAPIKeyMissing`` from
    ``fetch_company_news`` — the asset fails the run rather than silently
    inserting zero rows, since downstream topology (a) needs real rows.
    """
    ticker = context.partition_key
    today = date.today()
    from_date = today - timedelta(days=config.lookback_days)

    context.log.info(
        "Fetching Finnhub /company-news for %s [%s..%s]",
        ticker,
        from_date.isoformat(),
        today.isoformat(),
    )

    articles = fetch_company_news(ticker, from_date=from_date, to_date=today)
    if not articles:
        context.log.warning("No articles returned for %s — skipping insert", ticker)
        return

    rows: list[dict[str, Any]] = []
    for article in articles:
        row = _article_to_row(article, ticker=ticker)
        if row is not None:
            rows.append(row)

    if not rows:
        context.log.warning(
            "All %d articles for %s were unusable (missing url/headline/datetime) — skipping",
            len(articles),
            ticker,
        )
        return

    df = pd.DataFrame(rows)
    df["id"] = df["id"].astype("uint64")

    cols = [
        "id",
        "ticker",
        "headline",
        "body",
        "source",
        "url",
        "published_at",
        "fetched_at",
        "publisher_name",
        "image_url",
        "sentiment_label",
    ]
    df = pd.DataFrame(df[cols])

    clickhouse.insert_df("equity_raw.news_raw", df)
    context.log.info("Inserted %d Finnhub rows for %s", len(df), ticker)

    time.sleep(_INTER_CALL_SLEEP_SECONDS)
