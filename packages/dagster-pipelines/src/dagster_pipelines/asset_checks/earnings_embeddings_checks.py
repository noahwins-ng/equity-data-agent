"""Data quality checks for the Qdrant ``equity_earnings`` collection (QNT-260).

The earnings_embeddings asset upserts one point per release chunk, keyed by
``point_id(ticker, doc_id, chunk_index)``. These checks verify the two stores
stay in sync at the release granularity and that the collection dimension hasn't
regressed:

1. Every release in ClickHouse has at least one embedded chunk in Qdrant
   (the asset silently skipping a release/ticker is the failure to catch).
2. The collection's vector dimension is still 384.

Both default to WARN. Counting *chunks* exactly would require re-chunking every
body in the check, so the count check works at the release (doc_id) granularity
the embed step preserves — each release must contribute >= 1 point.
"""

from __future__ import annotations

from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    asset_check,
)

from dagster_pipelines.assets.earnings_embeddings import (
    COLLECTION,
    VECTOR_SIZE,
    earnings_embeddings,
)
from dagster_pipelines.resources.clickhouse import ClickHouseResource
from dagster_pipelines.resources.qdrant import QdrantResource

_TABLE = "equity_raw.earnings_releases_raw"
_DEFAULT_SEVERITY = AssetCheckSeverity.WARN


@asset_check(asset=earnings_embeddings)
def earnings_embeddings_all_releases_indexed(
    clickhouse: ClickHouseResource,
    qdrant: QdrantResource,
) -> AssetCheckResult:
    """Warn if any ClickHouse release has zero Qdrant points.

    Every release (ticker, doc_id) in earnings_releases_raw must have produced
    >= 1 chunk point. A release with zero points means the asset skipped it
    (transient Qdrant failure past the retry budget, or a body that chunked to
    nothing).

    QNT-263 follow-up: derive the indexed (ticker, doc_id) set from ONE paginated
    scroll of the collection, then diff against ClickHouse — instead of a
    ``count()`` per release. The old per-doc fan-out fired ~one Qdrant call per
    release, which under a concurrent backfill (8 partitions × the fan-out) blew
    the Qdrant free-tier request-rate limit and failed the check on data that was
    actually fully indexed. One scroll is O(pages) calls regardless of corpus
    size, so the check no longer scales its Qdrant load with the release count.
    """
    releases = clickhouse.query_df(
        f"SELECT ticker, doc_id FROM {_TABLE} FINAL GROUP BY ticker, doc_id"
    )
    expected = {
        (str(t), int(d)) for t, d in zip(releases["ticker"], releases["doc_id"], strict=False)
    }

    indexed: set[tuple[str, int]] = set()
    for payload in qdrant.scroll_payloads(COLLECTION):
        ticker = payload.get("ticker")
        doc_id = payload.get("doc_id")
        if ticker is not None and doc_id is not None:
            indexed.add((str(ticker), int(doc_id)))

    missing = sorted(expected - indexed)
    return AssetCheckResult(
        passed=len(missing) == 0,
        severity=_DEFAULT_SEVERITY,
        metadata={
            "releases_checked": len(expected),
            "unindexed_count": len(missing),
            "unindexed_releases": [{"ticker": t, "doc_id": d} for t, d in missing],
            "collection": COLLECTION,
        },
        description=f"{len(missing)}/{len(expected)} releases have zero Qdrant points",
    )


@asset_check(asset=earnings_embeddings)
def earnings_embeddings_dimension(qdrant: QdrantResource) -> AssetCheckResult:
    """Warn if the ``equity_earnings`` collection's vector dimension is not 384.

    Qdrant enforces vector size at collection creation and rejects mismatched
    upserts, so the collection config is a storage-side guarantee for every
    point. A missing collection (first embed run failed before
    ``ensure_collection``) surfaces as a WARN, not a hard exception.
    """
    try:
        dim = qdrant.collection_dimension(COLLECTION)
    except Exception as exc:
        return AssetCheckResult(
            passed=False,
            severity=_DEFAULT_SEVERITY,
            metadata={"collection": COLLECTION, "error": str(exc)},
            description=f"Collection {COLLECTION} not accessible: {exc}",
        )
    return AssetCheckResult(
        passed=dim == VECTOR_SIZE,
        severity=_DEFAULT_SEVERITY,
        metadata={
            "collection": COLLECTION,
            "configured_dimension": dim,
            "expected_dimension": VECTOR_SIZE,
        },
        description=(
            f"Collection {COLLECTION} configured for {dim}-dim vectors (expected {VECTOR_SIZE})"
        ),
    )


__all__ = [
    "earnings_embeddings_all_releases_indexed",
    "earnings_embeddings_dimension",
]
