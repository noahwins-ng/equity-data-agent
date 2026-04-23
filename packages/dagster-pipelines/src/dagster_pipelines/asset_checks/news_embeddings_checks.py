"""Data quality checks for Qdrant ``equity_news`` vs equity_raw.news_raw (QNT-93).

The news_embeddings asset upserts one point per news_raw row, keyed by the
same UInt64 blake2b(url) hash. These checks verify the two stores stay in
sync — the pairs of things that can go wrong:

1. Drift in count (embedding asset failing silently for some ticker)
2. Orphaned vectors (news_raw row deleted but Qdrant kept the point)
3. Wrong vector dimension (collection config regressed from 384)

All checks default to WARN (``_DEFAULT_SEVERITY``); promote a specific check to
ERROR by replacing the ``severity=`` kwarg in its ``AssetCheckResult``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    asset_check,
)

from dagster_pipelines.assets.news_embeddings import (
    COLLECTION,
    VECTOR_SIZE,
    news_embeddings,
)
from dagster_pipelines.resources.clickhouse import ClickHouseResource
from dagster_pipelines.resources.qdrant import QdrantResource

if TYPE_CHECKING:
    from qdrant_client.models import Filter

_NEWS_TABLE = "equity_raw.news_raw"

_DEFAULT_SEVERITY = AssetCheckSeverity.WARN

# Steady-state, news_raw_sensor fires news_embeddings within seconds of a
# news_raw materialization, so the only legitimate divergence is rows landing
# in news_raw between the embedding sensor tick and this check's run — bounded
# by per-ticker in-flight count, not the full per-tick RSS output. A tolerance
# >5 starts masking the exact silent-failure class this check exists to catch
# (embedding asset skipping a ticker). First-deployment WARNs until any pre-
# news_embeddings backlog falls outside the 7-day re-embed window are expected.
_COUNT_DELTA_TOLERANCE = 5


def _ticker_filter(ticker: str) -> Filter:
    """Build a Qdrant filter matching points whose payload ticker equals ``ticker``."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    return Filter(must=[FieldCondition(key="ticker", match=MatchValue(value=ticker))])


@asset_check(asset=news_embeddings)
def news_embeddings_vector_count_matches_source(
    clickhouse: ClickHouseResource,
    qdrant: QdrantResource,
) -> AssetCheckResult:
    """Warn if per-ticker Qdrant point count diverges from news_raw row count.

    The embedding asset upserts one vector per news_raw row, so the two counts
    should track each other. A per-ticker gap larger than the in-flight
    tolerance indicates the asset skipped some rows (transient Qdrant failure
    that exceeded the retry budget) or the feed produced many new rows since
    the last tick.
    """
    from shared.tickers import TICKERS

    ch_counts_df = clickhouse.query_df(
        f"SELECT ticker, count() AS n FROM {_NEWS_TABLE} FINAL GROUP BY ticker"
    )
    ch_counts = {
        str(t): int(n) for t, n in zip(ch_counts_df["ticker"], ch_counts_df["n"], strict=False)
    }

    per_ticker_delta: dict[str, dict[str, int]] = {}
    divergences: dict[str, dict[str, int]] = {}
    for ticker in TICKERS:
        qd = qdrant.count(COLLECTION, query_filter=_ticker_filter(ticker))
        ch = ch_counts.get(ticker, 0)
        entry = {"qdrant": qd, "clickhouse": ch, "delta": qd - ch}
        per_ticker_delta[ticker] = entry
        if abs(qd - ch) > _COUNT_DELTA_TOLERANCE:
            divergences[ticker] = entry

    return AssetCheckResult(
        passed=len(divergences) == 0,
        severity=_DEFAULT_SEVERITY,
        metadata={
            # All per-ticker deltas surface even when within tolerance — drift
            # below the fail threshold is still diagnostic signal.
            "per_ticker_delta": per_ticker_delta,
            "divergent_tickers": divergences,
            "tolerance": _COUNT_DELTA_TOLERANCE,
            "collection": COLLECTION,
        },
        description=(
            f"{len(divergences)} tickers have |qdrant - clickhouse| > {_COUNT_DELTA_TOLERANCE}"
            + (f": {divergences}" if divergences else "")
        ),
    )


@asset_check(asset=news_embeddings)
def news_embeddings_no_orphaned_vectors(
    clickhouse: ClickHouseResource,
    qdrant: QdrantResource,
) -> AssetCheckResult:
    """Warn if any Qdrant point ID is missing from news_raw.

    ``id`` is shared between the two stores (blake2b(url) UInt64), so a Qdrant
    point whose ID is absent from news_raw is an orphan — the news_raw row
    was deleted (manual fix, TTL policy) but Qdrant retained the vector.
    Orphans contaminate semantic search with results that no longer have a
    source row to display.
    """
    from shared.tickers import TICKERS

    orphan_counts: dict[str, int] = {}
    total_scanned = 0
    for ticker in TICKERS:
        # scroll_ids paginates fully and raises if it hits the safety cap, so a
        # silently-truncated scroll cannot mask orphans beyond page 1.
        qd_ids = qdrant.scroll_ids(COLLECTION, query_filter=_ticker_filter(ticker))
        if not qd_ids:
            continue
        total_scanned += len(qd_ids)

        # Inline the IDs — IDs are int-cast UInt64 digits only (see
        # QdrantResource.scroll_ids), so the f-string is injection-safe. IN-list
        # of up to ~10k integers is well within ClickHouse's parser limits.
        id_list = ",".join(str(i) for i in qd_ids)
        matches_row = clickhouse.execute(
            f"SELECT count() FROM {_NEWS_TABLE} FINAL WHERE id IN ({id_list})"
        ).result_rows[0]
        matches = int(matches_row[0])
        missing = len(qd_ids) - matches
        if missing > 0:
            orphan_counts[ticker] = missing

    total_orphans = sum(orphan_counts.values())
    return AssetCheckResult(
        passed=total_orphans == 0,
        severity=_DEFAULT_SEVERITY,
        metadata={
            "orphans_per_ticker": orphan_counts,
            "total_orphans": total_orphans,
            "points_scanned": total_scanned,
        },
        description=(
            f"{total_orphans} orphaned vectors across {len(orphan_counts)} tickers "
            f"(scanned {total_scanned} points)"
        ),
    )


@asset_check(asset=news_embeddings)
def news_embeddings_embedding_dimension(
    qdrant: QdrantResource,
) -> AssetCheckResult:
    """Warn if the ``equity_news`` collection's vector dimension is not 384.

    Qdrant enforces vector size at collection creation and rejects any upsert
    with a mismatched size, so checking the collection config is a storage-
    side guarantee for every point. Vectors are Float32 natively in Qdrant
    regardless of the Python-side type sent on upsert (`all-MiniLM-L6-v2`
    emits 384-dim embeddings; the collection is configured to match).

    If the collection doesn't exist yet — e.g. the first news_embeddings run
    failed before ``ensure_collection`` — surface that as a WARN failure
    rather than letting the qdrant_client exception bubble out as a harder
    check failure.
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
    "news_embeddings_embedding_dimension",
    "news_embeddings_no_orphaned_vectors",
    "news_embeddings_vector_count_matches_source",
]
