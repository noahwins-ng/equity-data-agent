"""Dagster asset: embed news headlines via Qdrant Cloud Inference (QNT-54).

Downstream of ``news_raw``. Runs per-ticker partition, full re-embed of the
last 7 days of rows on every tick. Bounded volume (~30 rows/tick at current
RSS cadence) makes the simpler full-refresh strategy preferable to a
cursor-based incremental — see ADR-009.

Embedding happens server-side on Qdrant Cloud (``cloud_inference=True``),
so the run-worker here is I/O-bound rather than memory-bound.
"""

import logging

from dagster import (
    AssetExecutionContext,
    Backoff,
    RetryPolicy,
    StaticPartitionsDefinition,
    asset,
)
from shared.tickers import TICKERS

from dagster_pipelines.resources.clickhouse import ClickHouseResource
from dagster_pipelines.resources.qdrant import QdrantCollectionSpec, QdrantResource

logger = logging.getLogger(__name__)

news_embeddings_partitions = StaticPartitionsDefinition(TICKERS)

COLLECTION = "equity_news"
VECTOR_SIZE = 384  # all-MiniLM-L6-v2 output dimension
EMBED_MODEL = "sentence-transformers/all-minilm-l6-v2"

NEWS_COLLECTION_SPEC = QdrantCollectionSpec(
    name=COLLECTION,
    vector_size=VECTOR_SIZE,
    distance="Cosine",
    payload_indexes={"ticker": "keyword", "published_at": "integer"},
)

# Re-embed the tail of news_raw every tick. 7-day window is wider than the
# ~4h RSS cadence so a missed tick doesn't leave gaps, and narrow enough
# that the per-partition upsert stays bounded.
_FRESH_WINDOW_SQL = """
SELECT id, ticker, headline, url, source, published_at
FROM equity_raw.news_raw FINAL
WHERE ticker = %(ticker)s
  AND fetched_at >= now() - INTERVAL 7 DAY
ORDER BY published_at DESC
"""


@asset(
    partitions_def=news_embeddings_partitions,
    retry_policy=RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL),
    group_name="news",
    deps=["news_raw"],
)
def news_embeddings(
    context: AssetExecutionContext,
    clickhouse: ClickHouseResource,
    qdrant: QdrantResource,
) -> None:
    """Read rows recently ingested into news_raw (``fetched_at`` within the
    last 7 days) for the partition ticker, send each headline to Qdrant Cloud
    Inference for embedding, and upsert the resulting points into
    ``equity_news``. The window is on ingest time, not publish time — a
    late-discovered old article enters the window when first ingested and
    gets embedded, which is what we want.

    Point ID = the existing ``id`` column (blake2b(url) UInt64), so Qdrant
    dedup matches ClickHouse ReplacingMergeTree dedup — single source of
    truth for "is this URL already in the system". Re-runs are idempotent.
    """
    from qdrant_client.models import Document, PointStruct

    ticker = context.partition_key

    qdrant.ensure_collection(NEWS_COLLECTION_SPEC)

    df = clickhouse.query_df(_FRESH_WINDOW_SQL, parameters={"ticker": ticker})
    if df.empty:
        context.log.info("No recent news for %s — skipping", ticker)
        return

    import pandas as pd

    points: list[PointStruct] = []
    for record in df.to_dict(orient="records"):
        # equity_raw.news_raw.published_at is DateTime NOT NULL, so the cast
        # from NaT-capable pd.Timestamp to a concrete Timestamp is safe.
        # tz_localize("UTC") makes the UTC interpretation explicit — pandas'
        # default already treats naive .timestamp() as UTC, but stating it
        # removes ambiguity if this code ever migrates off pd.Timestamp.
        published_at: pd.Timestamp = pd.Timestamp(record["published_at"]).tz_localize(  # type: ignore[assignment]
            "UTC"
        )
        points.append(
            PointStruct(
                id=int(record["id"]),
                # Document is embedded server-side by Qdrant using the named
                # model; no local inference, no model weights, no CPU/memory
                # cost on the dagster run-worker beyond the HTTP round-trip.
                vector=Document(text=str(record["headline"]), model=EMBED_MODEL),
                payload={
                    "ticker": str(record["ticker"]),
                    # Qdrant integer index requires int; store unix seconds.
                    "published_at": int(published_at.timestamp()),
                    "url": str(record["url"]),
                    "headline": str(record["headline"]),
                    "source": str(record["source"]),
                },
            )
        )

    qdrant.upsert_points(COLLECTION, points)
    context.log.info("Upserted %d embeddings for %s to %s", len(points), ticker, COLLECTION)
