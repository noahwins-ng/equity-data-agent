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
import re
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pandas as pd
from dagster import (
    AssetExecutionContext,
    Backoff,
    Config,
    Jitter,
    RetryPolicy,
    StaticPartitionsDefinition,
    asset,
)
from shared.contracts import NEWS_RAW_CONTRACT, validate_contract
from shared.tickers import NEWS_RELEVANCE, TICKERS

from dagster_pipelines.news_feeds import (
    fetch_company_news,
    make_resolver_client,
    resolve_publisher_host,
)
from dagster_pipelines.rejects import Reject, record_rejects
from dagster_pipelines.resources.clickhouse import ClickHouseResource

logger = logging.getLogger(__name__)

news_partitions = StaticPartitionsDefinition(TICKERS)

# Steady-state lookback window. 7 days matches the news_embeddings re-embed
# window in news_embeddings.py (and gives a 6-tick buffer over the daily
# schedule), so a single missed tick doesn't leave gaps. Backfill overrides
# via NewsRawConfig.
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


# Compiled once at import. The boundary `(?<![-\w])` / `(?![-\w])` is stricter
# than `\b` because `\b` treats hyphens as boundaries — so a naive `\bMeta\b`
# pattern matches "meta-analysis" and "meta-study", which would let academic-
# jargon headlines slip through META's headline-only filter. Treating both
# word characters AND hyphens as boundary-blocking keeps "Meta's earnings"
# matchable (apostrophe is neither word char nor hyphen) while rejecting
# hyphenated compounds. Per-ticker scope ("any" vs "headline") is read
# alongside the pattern in `_passes_relevance`; see shared.tickers
# NEWS_RELEVANCE for the curation contract.
_RELEVANCE_PATTERNS: dict[str, re.Pattern[str]] = {
    ticker: re.compile(
        "|".join(
            rf"(?<![-\w]){re.escape(str(alias))}(?![-\w])"
            for alias in cfg["aliases"]  # type: ignore[union-attr]
        ),
        re.IGNORECASE,
    )
    for ticker, cfg in NEWS_RELEVANCE.items()
}


def _passes_relevance(ticker: str, headline: str, body: str) -> bool:
    """Per-ticker keep/drop gate. See shared.tickers.NEWS_RELEVANCE."""
    pattern = _RELEVANCE_PATTERNS[ticker]
    scope = NEWS_RELEVANCE[ticker]["scope"]
    if scope == "headline":
        return pattern.search(headline) is not None
    return pattern.search(headline) is not None or pattern.search(body) is not None


def _reject_reason(article: dict[str, Any]) -> str:
    """Classify why ``_article_to_row`` dropped an article, for the reject sink.

    Mirrors the guard order in ``_article_to_row`` exactly: missing url/headline
    or an invalid datetime are structural ("unusable"); anything else that
    survives those guards but still drops failed the relevance gate. The two
    functions must move together — kept separate so the drop-path classification
    never perturbs the hot keep path.
    """
    url = (article.get("url") or "").strip()
    headline = (article.get("headline") or "").strip()
    if not url or not headline:
        return "unusable"
    epoch = article.get("datetime")
    if not isinstance(epoch, int) or epoch <= 0:
        return "unusable"
    return "below_relevance"


def _article_to_row(
    article: dict[str, Any],
    ticker: str,
    *,
    resolver_client: httpx.Client | None = None,
) -> dict[str, Any] | None:
    """Convert a Finnhub /company-news article dict to a news_raw row.

    Returns None for unusable rows (no URL, no headline, no datetime). Empty
    image URLs are kept — design v2 reserves the thumbnail slot but renders a
    placeholder when ``image_url`` is empty.

    ``resolver_client`` is forwarded to ``resolve_publisher_host`` so the
    asset can share one ``httpx.Client`` across all articles in a partition
    (connection-pooled — the alternative is a fresh client per call which
    burns ~5x the wall-clock at scale). Tests stub it.
    """
    url = (article.get("url") or "").strip()
    headline = (article.get("headline") or "").strip()
    if not url or not headline:
        return None

    epoch = article.get("datetime")
    if not isinstance(epoch, int) or epoch <= 0:
        return None
    published_at = datetime.fromtimestamp(epoch, tz=UTC).replace(tzinfo=None)

    body = (article.get("summary") or "").strip()
    if not _passes_relevance(ticker, headline, body):
        return None

    return {
        "id": _url_hash(url),
        "ticker": ticker,
        "headline": headline,
        "body": body,
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
        # Per QNT-148: resolved at ingest, '' on any failure (timeout, 4xx).
        # Direct outlet URLs short-circuit to host(url); finnhub.io redirects
        # follow up to 5 hops within a 5s deadline.
        "resolved_host": resolve_publisher_host(url, client=resolver_client),
    }


@asset(
    partitions_def=news_partitions,
    retry_policy=RetryPolicy(
        max_retries=3,
        delay=30,
        backoff=Backoff.EXPONENTIAL,
        jitter=Jitter.PLUS_MINUS,
    ),
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
        # No articles fetched is not a reject, but still emit the 0-count so the
        # rejected_rows metric has no gaps for the QNT-240 dashboard.
        record_rejects(context, clickhouse, source_asset="news_raw", rejects=[])
        return

    # Source-boundary contract (QNT-259): validate the raw Finnhub frame before
    # per-article processing, so a renamed/missing Finnhub key hard-fails the
    # partition (-> QNT-62 Discord sensor) instead of silently degrading every
    # article to an "unusable" reject. Schema-tier only; per-row value handling
    # stays in _article_to_row below.
    validate_contract(pd.DataFrame(articles), NEWS_RAW_CONTRACT)

    # One pooled client for all redirect resolutions in this partition.
    # Connection reuse is the difference between ~30 ms/call (warm pool) and
    # ~150 ms/call (cold connect per article) at the observed ~30-row daily
    # volume per ticker. The asset's RetryPolicy handles transient errors at
    # the partition level; per-article failures soft-fail to '' inside the
    # resolver and never bubble up here. ``make_resolver_client`` keeps the
    # timeout / redirect-budget config centralised in news_feeds.py.
    resolver_client = make_resolver_client()
    try:
        rows: list[dict[str, Any]] = []
        rejects: list[Reject] = []
        resolved_count = 0
        for article in articles:
            row = _article_to_row(article, ticker=ticker, resolver_client=resolver_client)
            if row is not None:
                rows.append(row)
                if row["resolved_host"]:
                    resolved_count += 1
            else:
                rejects.append(
                    Reject(
                        ticker=ticker,
                        reason=_reject_reason(article),
                        payload={
                            "url": article.get("url"),
                            "headline": article.get("headline"),
                            "datetime": article.get("datetime"),
                            "publisher": article.get("source"),
                        },
                    )
                )
    finally:
        resolver_client.close()

    dropped = len(articles) - len(rows)
    context.log.info(
        "news_raw[%s]: kept %d / %d articles (dropped %d below relevance threshold or unusable)",
        ticker,
        len(rows),
        len(articles),
        dropped,
    )
    record_rejects(context, clickhouse, source_asset="news_raw", rejects=rejects)

    if not rows:
        context.log.warning(
            "All %d articles for %s were unusable or below relevance threshold — skipping",
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
        "resolved_host",
    ]
    df = pd.DataFrame(df[cols])

    clickhouse.insert_df("equity_raw.news_raw", df)
    context.log.info(
        "Inserted %d Finnhub rows for %s (%d/%d resolved_host populated)",
        len(df),
        ticker,
        resolved_count,
        len(rows),
    )

    time.sleep(_INTER_CALL_SLEEP_SECONDS)
