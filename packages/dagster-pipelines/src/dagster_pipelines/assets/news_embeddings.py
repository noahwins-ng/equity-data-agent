"""Dagster asset: embed news headlines via Qdrant Cloud Inference (QNT-54).

Downstream of ``news_raw``. Runs per-ticker partition, scoped to articles
published in the last 7 days, and upserts only deltas (point IDs not yet
present in Qdrant for the ticker) — see ADR-009 for the rolling-window
design and QNT-142 for the delta-only switch that protects the Qdrant free
tier post-Finnhub backfill.

Embedding happens server-side on Qdrant Cloud (``cloud_inference=True``),
so the run-worker here is I/O-bound rather than memory-bound.
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

from dagster_pipelines.resources.clickhouse import ClickHouseResource
from dagster_pipelines.resources.qdrant import QdrantCollectionSpec, QdrantResource

if TYPE_CHECKING:
    from qdrant_client.models import Filter

logger = logging.getLogger(__name__)

news_embeddings_partitions = StaticPartitionsDefinition(TICKERS)

COLLECTION = "equity_news"
VECTOR_SIZE = 384  # all-MiniLM-L6-v2 output dimension
EMBED_MODEL = "sentence-transformers/all-minilm-l6-v2"

# Rolling window the asset operates over (ADR-009). Single source of truth
# shared by ``_FRESH_WINDOW_SQL`` (CH-side upsert input), the QNT-145 GC tail
# (Qdrant-side delete), and the QNT-93 count + orphan checks (Qdrant scope).
WINDOW_DAYS = 7

NEWS_COLLECTION_SPEC = QdrantCollectionSpec(
    name=COLLECTION,
    vector_size=VECTOR_SIZE,
    distance="Cosine",
    payload_indexes={"ticker": "keyword", "published_at": "integer"},
)

# Bound the per-partition scan to articles *published* in the last
# ``WINDOW_DAYS`` (QNT-142). Prior to QNT-141's Finnhub migration this filter
# was on ``fetched_at`` and the per-tick volume was a bounded ~30 rows; the
# Finnhub backfill landed 28k rows with ``fetched_at = today`` and 1y of
# ``published_at`` history, which would have multiplied the per-tick Qdrant
# inference budget by ~93x. Filtering on ``published_at`` keeps Qdrant as
# the rolling-7d semantic search index ADR-009 designed for; the 1y backfill
# stays in ClickHouse for the news card on the ticker-detail page.
_FRESH_WINDOW_SQL = f"""
SELECT id, ticker, headline, url, source, published_at
FROM equity_raw.news_raw FINAL
WHERE ticker = %(ticker)s
  AND published_at >= now() - INTERVAL {WINDOW_DAYS} DAY
ORDER BY published_at DESC
"""


def ticker_filter(ticker: str) -> "Filter":
    """Build a Qdrant filter matching points whose payload ticker equals ``ticker``.

    Shared with the QNT-93 asset checks so the asset and its checks scope
    Qdrant operations identically (same payload index, same match semantics).
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    return Filter(must=[FieldCondition(key="ticker", match=MatchValue(value=ticker))])


def aged_ticker_filter(ticker: str, cutoff_ts: int) -> "Filter":
    """Build a Qdrant filter matching points whose payload ticker equals
    ``ticker`` AND ``published_at < cutoff_ts``.

    Used by the asset's GC tail (QNT-145) to delete points whose
    ``published_at`` has aged past the rolling window. The complement of the
    windowed filter the QNT-142 count check uses, applied at the same Python-
    side ``cutoff_ts`` anchor: the count check counts ``published_at >=
    cutoff_ts`` and the asset deletes ``published_at < cutoff_ts``, so the
    union of "kept" and "deleted" partitions every point exactly once.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue, Range

    return Filter(
        must=[
            FieldCondition(key="ticker", match=MatchValue(value=ticker)),
            FieldCondition(key="published_at", range=Range(lt=cutoff_ts)),
        ]
    )


def point_id(ticker: str, url_id: int) -> int:
    """Derive a Qdrant UInt64 point ID from ``(ticker, url_id)``.

    ClickHouse keys ``news_raw`` on ``(ticker, published_at, id)`` — one row per
    ``(ticker, url)`` pair — so a URL cross-mentioned across N tickers produces
    N rows. Qdrant needs a matching key, otherwise the last ticker's upsert
    wins and cross-mentioned URLs silently disappear from per-ticker ticker-
    filtered search (QNT-120).

    Namespacing by ticker preserves:
      * Same-ticker idempotency (same URL under same ticker → same point ID →
        upsert dedups on the server, matching news_raw ReplacingMergeTree).
      * Cross-ticker cardinality (same URL under two tickers → two distinct
        points, each reachable via its own payload ticker filter).
    """
    return int(
        hashlib.blake2b(f"{ticker}:{url_id}".encode(), digest_size=8).hexdigest(),
        16,
    )


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
    """Read rows from news_raw whose ``published_at`` is within the last 7
    days for the partition ticker, scroll the existing point IDs in Qdrant
    for the same ticker, and upsert only the rows whose ``point_id`` is not
    already indexed. Steady-state ticks embed only the actually-new
    articles since the last tick; first post-resume tick catches up to the
    7-day window in one or two runs and then runs flat.

    Point ID = ``blake2b(f"{ticker}:{url_id}")`` — see ``point_id`` helper.
    Qdrant dedup matches ClickHouse ReplacingMergeTree's ``(ticker, url)``
    composite key, so a URL cross-mentioned across tickers lands as one
    point per ticker and surfaces under each ticker's payload filter
    (fixes the QNT-120 silent overwrite). Re-runs are idempotent — the
    delta filter short-circuits the upsert when every candidate already
    exists in Qdrant.

    QNT-145: after the (possibly empty) upsert, GC any ticker-scoped points
    whose ``published_at`` has aged past the rolling window. Runs every tick
    even on quiet tickers — points indexed in earlier ticks may have aged
    out since the last run, and skipping GC on a quiet tick would let the
    asymmetry between "in-window upsert" and "all-time retention" reopen
    the monotonic-growth gap this ticket exists to close.
    """
    from datetime import UTC, datetime, timedelta

    from qdrant_client.models import Document, PointStruct

    ticker = context.partition_key

    qdrant.ensure_collection(NEWS_COLLECTION_SPEC)

    # Single Python-side cutoff anchor for this tick. The CH read predicate
    # (``published_at >= now() - INTERVAL N DAY``) is server-side and re-
    # evaluated on each query, but the Qdrant GC needs a concrete value to
    # send in the filter; deriving it once here keeps "what we read" and
    # "what we keep" referring to the same conceptual cutoff per tick. The
    # QNT-93 count check uses the same derivation pattern for symmetry.
    cutoff_ts = int((datetime.now(UTC) - timedelta(days=WINDOW_DAYS)).timestamp())

    df = clickhouse.query_df(_FRESH_WINDOW_SQL, parameters={"ticker": ticker})
    if df.empty:
        context.log.info("No recent news for %s — skipping upsert", ticker)
    else:
        import pandas as pd

        # Pull every Qdrant point ID currently indexed under this ticker so we
        # can skip rows that are already embedded. ``scroll_ids`` paginates
        # under the hood and raises if it hits the safety cap, so a runaway
        # collection surfaces as a failed run rather than silently truncated.
        existing_ids: set[int] = set(
            qdrant.scroll_ids(COLLECTION, query_filter=ticker_filter(ticker))
        )

        points: list[PointStruct] = []
        for record in df.to_dict(orient="records"):
            pid = point_id(str(record["ticker"]), int(record["id"]))
            if pid in existing_ids:
                continue
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
                    id=pid,
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

        if points:
            qdrant.upsert_points(COLLECTION, points)
            context.log.info(
                "Upserted %d new embeddings for %s to %s (%d candidates, %d already indexed)",
                len(points),
                ticker,
                COLLECTION,
                len(df),
                len(df) - len(points),
            )
        else:
            context.log.info(
                "All %d candidate articles for %s already embedded — skipping upsert",
                len(df),
                ticker,
            )

    # GC tail: delete ticker-scoped points whose ``published_at < cutoff_ts``.
    # The asset only ever upserts points inside the window, so deleting points
    # outside it converges Qdrant on ADR-009's rolling-7d definition: any
    # point that lingers past the window is by construction a stale upsert
    # from an earlier tick, never a current source of truth.
    qdrant.delete_points_by_filter(COLLECTION, aged_ticker_filter(ticker, cutoff_ts))
    # qdrant_client's filtered ``delete`` doesn't return a count, so the run
    # history records the cutoff (not "N points"); the count drops out of
    # ``news_embeddings_vector_count_matches_source`` on the next check tick.
    context.log.info(
        "GC: purged aged points for %s (cutoff_ts=%d, window=%d days)",
        ticker,
        cutoff_ts,
        WINDOW_DAYS,
    )
