"""Dagster asset: embed 8-K earnings releases into Qdrant (QNT-260).

The second RAG corpus's embedding stage, downstream of ``earnings_releases_raw``
and mirroring ``news_embeddings``. Per-ticker partition: read the ticker's
releases from ClickHouse, chunk each ``body`` into section-tagged windows
(``edgar_feeds.chunk_release``), and upsert the chunks into the equity_earnings
Qdrant collection — delta-only (point IDs not yet present for the ticker).

Unlike news (a rolling 7-day window with GC), earnings releases are quarterly
and bounded — a handful of releases × ~30 chunks per ticker — so the asset
embeds every release the ingestion window keeps, with no aged-out GC tail.

Embedding happens server-side on Qdrant Cloud (``cloud_inference=True``), so the
run-worker is I/O-bound rather than memory-bound. Point ID =
``blake2b(f"{ticker}:{doc_id}:{chunk_index}")`` — namespaced by ticker (QNT-120
convention) and chunk so re-runs are idempotent and per-chunk granular.
"""

import hashlib
import logging
from typing import TYPE_CHECKING

from dagster import (
    AssetExecutionContext,
    Backoff,
    RetryPolicy,
    StaticPartitionsDefinition,
    asset,
)
from shared.tickers import TICKERS

from dagster_pipelines.edgar_feeds import chunk_release
from dagster_pipelines.resources.clickhouse import ClickHouseResource
from dagster_pipelines.resources.qdrant import QdrantCollectionSpec, QdrantResource

if TYPE_CHECKING:
    from qdrant_client.models import Filter

logger = logging.getLogger(__name__)

earnings_embeddings_partitions = StaticPartitionsDefinition(TICKERS)

COLLECTION = "equity_earnings"
VECTOR_SIZE = 384  # all-MiniLM-L6-v2 output dimension
EMBED_MODEL = "sentence-transformers/all-minilm-l6-v2"

EARNINGS_COLLECTION_SPEC = QdrantCollectionSpec(
    name=COLLECTION,
    vector_size=VECTOR_SIZE,
    distance="Cosine",
    payload_indexes={
        "ticker": "keyword",
        "doc_id": "integer",
        "filing_date": "integer",
        "section": "keyword",
    },
)

# Read every stored release for the partition ticker. The ingestion window keeps
# the corpus bounded (~5 quarters), so there's no rolling-window predicate here —
# all releases are embed candidates and the delta filter skips already-indexed
# chunks.
_RELEASES_SQL = """
SELECT doc_id, ticker, filing_date, title, url, body
FROM equity_raw.earnings_releases_raw FINAL
WHERE ticker = %(ticker)s
ORDER BY filing_date DESC
"""


def ticker_filter(ticker: str) -> "Filter":
    """Qdrant filter matching points whose payload ticker equals ``ticker``.

    Shared with the asset checks so the asset and its checks scope Qdrant
    operations identically.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    return Filter(must=[FieldCondition(key="ticker", match=MatchValue(value=ticker))])


def point_id(ticker: str, doc_id: int, chunk_index: int) -> int:
    """Derive a Qdrant UInt64 point ID from ``(ticker, doc_id, chunk_index)``.

    Namespaced by ticker (QNT-120 convention — a release cross-listed under two
    tickers lands as distinct points) and by chunk index so each chunk of a
    release is its own point. Same ``(ticker, doc_id, chunk_index)`` -> same ID,
    so re-runs upsert-dedup on the server, matching the ReplacingMergeTree
    idempotency of the source row.
    """
    return int(
        hashlib.blake2b(f"{ticker}:{doc_id}:{chunk_index}".encode(), digest_size=8).hexdigest(),
        16,
    )


@asset(
    partitions_def=earnings_embeddings_partitions,
    retry_policy=RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL),
    group_name="earnings",
    deps=["earnings_releases_raw"],
)
def earnings_embeddings(
    context: AssetExecutionContext,
    clickhouse: ClickHouseResource,
    qdrant: QdrantResource,
) -> None:
    """Chunk + embed the partition ticker's earnings releases into equity_earnings.

    Reads all releases for the ticker, chunks each body section-aware, scrolls
    the ticker's existing Qdrant point IDs, and upserts only the chunks not yet
    indexed. Re-runs are idempotent — the delta filter short-circuits the upsert
    when every chunk already exists.
    """
    import pandas as pd
    from qdrant_client.models import Document, PointStruct

    ticker = context.partition_key

    qdrant.ensure_collection(EARNINGS_COLLECTION_SPEC)

    df = clickhouse.query_df(_RELEASES_SQL, parameters={"ticker": ticker})
    if df.empty:
        context.log.info("No earnings releases for %s — skipping upsert", ticker)
        return

    existing_ids: set[int] = set(qdrant.scroll_ids(COLLECTION, query_filter=ticker_filter(ticker)))

    points: list[PointStruct] = []
    total_chunks = 0
    for record in df.to_dict(orient="records"):
        doc_id = int(record["doc_id"])
        # earnings_releases_raw.filing_date is Date NOT NULL, so the cast from a
        # NaT-capable pd.Timestamp to a concrete Timestamp is safe (matches the
        # published_at handling in news_embeddings).
        filing_at: pd.Timestamp = pd.Timestamp(record["filing_date"]).tz_localize("UTC")  # type: ignore[assignment]
        filing_ts = int(filing_at.timestamp())
        for chunk in chunk_release(str(record["body"])):
            total_chunks += 1
            pid = point_id(ticker, doc_id, chunk.index)
            if pid in existing_ids:
                continue
            points.append(
                PointStruct(
                    id=pid,
                    vector=Document(text=chunk.text, model=EMBED_MODEL),
                    payload={
                        "ticker": ticker,
                        "doc_id": doc_id,
                        "filing_date": filing_ts,
                        "section": chunk.section,
                        "chunk_index": chunk.index,
                        "url": str(record["url"]),
                        "title": str(record["title"]),
                        "text": chunk.text,
                    },
                )
            )

    if points:
        qdrant.upsert_points(COLLECTION, points)
        context.log.info(
            "Upserted %d new earnings chunks for %s to %s (%d total chunks, %d already indexed)",
            len(points),
            ticker,
            COLLECTION,
            total_chunks,
            total_chunks - len(points),
        )
    else:
        context.log.info(
            "All %d earnings chunks for %s already embedded — skipping upsert",
            total_chunks,
            ticker,
        )
