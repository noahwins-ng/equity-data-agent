"""Tests for the news_raw and news_embeddings asset checks (QNT-93).

Checks are executed via ``Definitions.get_subset(asset_check_selection=...)``
so the real Dagster wiring (severity, metadata, pass/fail) is exercised
end-to-end. The fake ClickHouse / Qdrant resources hand the check canned
data — tests assert the check's pass/fail verdict + key metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import pytest
from dagster import AssetChecksDefinition, Definitions
from dagster_pipelines.asset_checks.news_embeddings_checks import (
    news_embeddings_embedding_dimension,
    news_embeddings_no_orphaned_vectors,
    news_embeddings_vector_count_matches_source,
)
from dagster_pipelines.asset_checks.news_raw_checks import (
    news_raw_has_rows,
    news_raw_no_empty_headlines,
    news_raw_no_future_published_at,
    news_raw_recent_ingestion,
    news_raw_valid_urls,
)
from dagster_pipelines.assets.news_embeddings import VECTOR_SIZE, news_embeddings, point_id
from dagster_pipelines.assets.news_raw import news_raw

# ── Fakes ─────────────────────────────────────────────────────────────────────


@dataclass
class _Result:
    result_rows: list[list[Any]]


@dataclass
class _FakeClickHouse:
    """Canned responses keyed by SQL substring — tests configure what each
    query returns by registering (substring, response) tuples."""

    execute_responses: list[tuple[str, list[list[Any]]]] = field(default_factory=list)
    query_df_responses: list[tuple[str, pd.DataFrame]] = field(default_factory=list)

    def execute(self, query: str, *_args: Any, **_kwargs: Any) -> _Result:
        for substring, rows in self.execute_responses:
            if substring in query:
                return _Result(result_rows=rows)
        raise AssertionError(f"No execute stub matched query: {query[:120]}…")

    def query_df(self, query: str, parameters: dict[str, Any] | None = None) -> pd.DataFrame:
        for substring, df in self.query_df_responses:
            if substring in query:
                return df.copy()
        raise AssertionError(f"No query_df stub matched query: {query[:120]}…")

    def insert_df(self, *_args: Any, **_kwargs: Any) -> None: ...


@dataclass
class _FakeQdrant:
    counts_by_ticker: dict[str, int] = field(default_factory=dict)
    ids_by_ticker: dict[str, list[int]] = field(default_factory=dict)
    dimension: int = VECTOR_SIZE
    dimension_raises: Exception | None = None
    # Filter shape recorders — tests that need to assert *which* filter shape
    # the check passed to ``count`` / ``scroll_ids`` (QNT-142 windowed count
    # filter, QNT-145 windowed orphan-check scroll filter) read from these.
    # Append-only, ordered.
    count_filters: list[Any] = field(default_factory=list)
    scroll_filters: list[Any] = field(default_factory=list)
    delete_filters: list[Any] = field(default_factory=list)

    def count(self, _collection: str, query_filter: Any | None = None) -> int:
        self.count_filters.append(query_filter)
        ticker = _ticker_from_filter(query_filter)
        return self.counts_by_ticker.get(ticker, 0)

    def scroll_ids(
        self,
        _collection: str,
        query_filter: Any | None = None,
        page_size: int = 10_000,
        max_pages: int = 100,
    ) -> list[int]:
        del page_size, max_pages  # fake is in-memory, pagination doesn't apply
        self.scroll_filters.append(query_filter)
        ticker = _ticker_from_filter(query_filter)
        return list(self.ids_by_ticker.get(ticker, []))

    def delete_points_by_filter(self, _collection: str, query_filter: Any) -> None:
        self.delete_filters.append(query_filter)

    def collection_dimension(self, _collection: str) -> int:
        if self.dimension_raises is not None:
            raise self.dimension_raises
        return self.dimension


def _ticker_from_filter(query_filter: Any) -> str:
    """Extract the ticker literal from the ``_ticker_filter`` Qdrant Filter we build."""
    condition = query_filter.must[0]
    return condition.match.value


# ── Harness ───────────────────────────────────────────────────────────────────


def _run_check(
    check: AssetChecksDefinition,
    *,
    asset: Any,
    resources: dict[str, Any],
):
    """Execute a single asset check in-process, bypassing the asset step.

    Mirrors the runtime path (Dagster wires severity + metadata through the
    ``AssetCheckResult`` returned by the check), so the test catches
    decorator-level regressions that a direct function call would miss.
    """
    defs = Definitions(assets=[asset], asset_checks=[check], resources=resources)
    # Only this check is registered, so the full check-key set is exactly {check}.
    check_keys = set(defs.resolve_asset_graph().asset_check_keys)
    job = defs.resolve_implicit_global_asset_job_def().get_subset(
        asset_check_selection=check_keys,
        asset_selection=set(),
    )
    result = job.execute_in_process(raise_on_error=False)
    evals = result.get_asset_check_evaluations()
    assert len(evals) == 1, f"expected 1 check eval, got {len(evals)}"
    return evals[0]


# ── news_raw checks ───────────────────────────────────────────────────────────


def test_news_raw_has_rows_fails_when_a_ticker_is_empty() -> None:
    """Only 9 of 10 tickers have rows — check surfaces the empty one by name."""
    ch = _FakeClickHouse(
        query_df_responses=[
            (
                "GROUP BY ticker",
                pd.DataFrame(
                    [
                        {"ticker": t, "row_count": 5}
                        for t in (
                            "AAPL",
                            "AMZN",
                            "GOOGL",
                            "JPM",
                            "META",
                            "MSFT",
                            "NVDA",
                            "TSLA",
                            "UNH",
                        )
                    ]
                ),
            ),
        ],
    )
    e = _run_check(news_raw_has_rows, asset=news_raw, resources={"clickhouse": ch})
    assert not e.passed
    assert e.metadata["empty_tickers"].value == ["V"]


def test_news_raw_has_rows_passes_when_all_tickers_populated() -> None:
    ch = _FakeClickHouse(
        query_df_responses=[
            (
                "GROUP BY ticker",
                pd.DataFrame(
                    [
                        {"ticker": t, "row_count": 5}
                        for t in (
                            "AAPL",
                            "AMZN",
                            "GOOGL",
                            "JPM",
                            "META",
                            "MSFT",
                            "NVDA",
                            "TSLA",
                            "UNH",
                            "V",
                        )
                    ]
                ),
            ),
        ],
    )
    e = _run_check(news_raw_has_rows, asset=news_raw, resources={"clickhouse": ch})
    assert e.passed


@pytest.mark.parametrize(
    ("check", "expected_substring", "bad_count", "expect_passed"),
    [
        (news_raw_no_empty_headlines, "empty(trim(headline))", 0, True),
        (news_raw_no_empty_headlines, "empty(trim(headline))", 3, False),
        (news_raw_valid_urls, "startsWith(url, 'http", 0, True),
        (news_raw_valid_urls, "startsWith(url, 'http", 7, False),
        (news_raw_no_future_published_at, "published_at >", 0, True),
        (news_raw_no_future_published_at, "published_at >", 1, False),
    ],
)
def test_news_raw_integrity_checks(
    check: AssetChecksDefinition,
    expected_substring: str,
    bad_count: int,
    expect_passed: bool,
) -> None:
    """Integrity checks return passed=True iff the offending row count is 0."""
    ch = _FakeClickHouse(execute_responses=[(expected_substring, [[bad_count]])])
    e = _run_check(check, asset=news_raw, resources={"clickhouse": ch})
    assert e.passed is expect_passed


def test_news_raw_recent_ingestion_reports_stale_tickers() -> None:
    """One ticker (TSLA) is past the staleness threshold; the rest are fresh.
    Python-side classification lets us surface both counts without a second scan."""
    ch = _FakeClickHouse(
        query_df_responses=[
            (
                "GROUP BY ticker",
                pd.DataFrame(
                    [{"ticker": "TSLA", "hours_since_fetch": 60}]
                    + [
                        {"ticker": t, "hours_since_fetch": 2}
                        for t in (
                            "AAPL",
                            "AMZN",
                            "GOOGL",
                            "JPM",
                            "META",
                            "MSFT",
                            "NVDA",
                            "UNH",
                            "V",
                        )
                    ]
                ),
            ),
        ],
    )
    e = _run_check(news_raw_recent_ingestion, asset=news_raw, resources={"clickhouse": ch})
    assert not e.passed
    assert e.metadata["stale_tickers"].value == {"TSLA": 60}
    assert e.metadata["fresh_ticker_count"].value == 9


# ── news_embeddings checks ────────────────────────────────────────────────────


def _all_tickers_df(count_per_ticker: int) -> pd.DataFrame:
    from shared.tickers import TICKERS

    return pd.DataFrame([{"ticker": t, "n": count_per_ticker} for t in TICKERS])


def test_news_embeddings_vector_count_matches_within_tolerance() -> None:
    from shared.tickers import TICKERS

    ch = _FakeClickHouse(
        query_df_responses=[("GROUP BY ticker", _all_tickers_df(100))],
    )
    # Qdrant slightly behind per ticker (in-flight embedding between sensor tick
    # and check run), delta of 3 is within the tightened tolerance of 5.
    qdrant = _FakeQdrant(counts_by_ticker={t: 97 for t in TICKERS})
    e = _run_check(
        news_embeddings_vector_count_matches_source,
        asset=news_embeddings,
        resources={"clickhouse": ch, "qdrant": qdrant},
    )
    assert e.passed
    # Every ticker's delta surfaces in metadata even when inside tolerance —
    # drift below the fail threshold is still diagnostic signal.
    per_ticker = e.metadata["per_ticker_delta"].value
    assert isinstance(per_ticker, dict)
    assert set(per_ticker.keys()) == set(TICKERS)
    for entry in per_ticker.values():
        assert isinstance(entry, dict)
        assert entry["delta"] == -3


def test_news_embeddings_vector_count_flags_large_divergence() -> None:
    from shared.tickers import TICKERS

    ch = _FakeClickHouse(
        query_df_responses=[("GROUP BY ticker", _all_tickers_df(100))],
    )
    qdrant = _FakeQdrant(counts_by_ticker={t: 100 for t in TICKERS} | {"NVDA": 0})
    e = _run_check(
        news_embeddings_vector_count_matches_source,
        asset=news_embeddings,
        resources={"clickhouse": ch, "qdrant": qdrant},
    )
    assert not e.passed
    divergent = e.metadata["divergent_tickers"].value
    assert isinstance(divergent, dict)
    assert "NVDA" in divergent


def test_news_embeddings_vector_count_uses_published_at_windowed_qdrant_filter() -> None:
    """Both sides of the count comparison must scope to the same 7-day
    published_at window. Without the Qdrant-side window the gap drifts
    monotonically as old points accumulate (no GC) past the CH-side cutoff
    — the check would emit a permanent WARN within ~9 days of the QNT-141
    backfill landing and mask real drift.

    Verifies the structural shape of the filter sent to ``qdrant.count``:
    a ticker FieldCondition + a published_at Range with a recent ``gte``.
    """
    from datetime import UTC, datetime

    from shared.tickers import TICKERS

    ch = _FakeClickHouse(
        query_df_responses=[("GROUP BY ticker", _all_tickers_df(100))],
    )
    qdrant = _FakeQdrant(counts_by_ticker={t: 100 for t in TICKERS})

    before = int(datetime.now(UTC).timestamp())
    _run_check(
        news_embeddings_vector_count_matches_source,
        asset=news_embeddings,
        resources={"clickhouse": ch, "qdrant": qdrant},
    )
    after = int(datetime.now(UTC).timestamp())

    # One count call per ticker, all windowed.
    assert len(qdrant.count_filters) == len(TICKERS)
    seven_days_seconds = 7 * 24 * 60 * 60
    for f in qdrant.count_filters:
        # First condition is ticker (helper still finds it for the existing
        # _ticker_from_filter shortcut), second is the published_at Range.
        assert len(f.must) == 2
        assert f.must[0].key == "ticker"
        assert f.must[1].key == "published_at"
        cutoff = f.must[1].range.gte
        assert cutoff is not None
        # ``gte`` cutoff lands within the window [now - 7d - test-fudge,
        # now - 7d + test-fudge]; if anyone reverts to a one-sided window the
        # fudged-by-execution-time bound still fails.
        assert before - seven_days_seconds - 5 <= cutoff <= after - seven_days_seconds + 5


def test_news_embeddings_no_orphaned_vectors_passes_when_all_ids_in_clickhouse() -> None:
    """Expected IDs are computed per-ticker as ``point_id(ticker, news_raw.id)``
    (QNT-120). If every Qdrant point's namespaced ID derives from a row in
    news_raw under the same ticker, no orphans."""
    from shared.tickers import TICKERS

    url_ids = [1, 2, 3, 4, 5]
    ch = _FakeClickHouse(
        # One DF stub serves every per-ticker query; the check namespaces the
        # raw url_ids with ticker before comparing, so the expected set is
        # naturally distinct per ticker.
        query_df_responses=[
            ("SELECT id FROM equity_raw.news_raw", pd.DataFrame({"id": url_ids})),
        ],
    )
    qdrant = _FakeQdrant(
        ids_by_ticker={t: [point_id(t, i) for i in url_ids] for t in TICKERS},
    )
    e = _run_check(
        news_embeddings_no_orphaned_vectors,
        asset=news_embeddings,
        resources={"clickhouse": ch, "qdrant": qdrant},
    )
    assert e.passed
    assert e.metadata["total_orphans"].value == 0


def test_news_embeddings_no_orphaned_vectors_flags_missing_ids() -> None:
    """Each ticker has 5 Qdrant points but only 3 of their url_ids map to
    news_raw rows → 2 orphans per ticker. Simulates news_raw rows deleted
    (manual fix, TTL) while Qdrant retained the vectors."""
    from shared.tickers import TICKERS

    url_ids_in_clickhouse = [1, 2, 3]  # 4 and 5 were deleted
    ch = _FakeClickHouse(
        query_df_responses=[
            (
                "SELECT id FROM equity_raw.news_raw",
                pd.DataFrame({"id": url_ids_in_clickhouse}),
            ),
        ],
    )
    qdrant = _FakeQdrant(
        ids_by_ticker={t: [point_id(t, i) for i in [1, 2, 3, 4, 5]] for t in TICKERS},
    )
    e = _run_check(
        news_embeddings_no_orphaned_vectors,
        asset=news_embeddings,
        resources={"clickhouse": ch, "qdrant": qdrant},
    )
    assert not e.passed
    assert e.metadata["total_orphans"].value == 2 * len(TICKERS)


def test_news_embeddings_no_orphaned_vectors_ignores_cross_ticker_ids() -> None:
    """Regression test for QNT-120: a Qdrant point derived from ticker A's
    url_id must NOT pass orphan validation under ticker B — even though the
    underlying url_id exists in news_raw under A. Without per-ticker
    namespacing this would spuriously pass because both stores share the
    raw hash."""
    from shared.tickers import TICKERS

    shared_url_id = 7777
    ch = _FakeClickHouse(
        query_df_responses=[
            ("SELECT id FROM equity_raw.news_raw", pd.DataFrame({"id": [shared_url_id]})),
        ],
    )
    # MSFT has the cross-ticker-ID orphan (TSLA's namespaced ID appears under
    # MSFT's filter); other tickers are clean.
    ids_by_ticker = {t: [point_id(t, shared_url_id)] for t in TICKERS}
    ids_by_ticker["MSFT"] = [point_id("TSLA", shared_url_id)]
    qdrant = _FakeQdrant(ids_by_ticker=ids_by_ticker)

    e = _run_check(
        news_embeddings_no_orphaned_vectors,
        asset=news_embeddings,
        resources={"clickhouse": ch, "qdrant": qdrant},
    )
    assert not e.passed
    assert e.metadata["orphans_per_ticker"].value == {"MSFT": 1}


def test_news_embeddings_no_orphaned_vectors_uses_published_at_windowed_scroll_filter() -> None:
    """QNT-145: the orphan check must scroll Qdrant scoped to the same 7-day
    ``published_at`` window the count check uses. Without this scoping, after
    GC ships the orphan check becomes a no-op on quiet tickers (no points
    outside the window means scrolling all-time is the same as scrolling the
    window) — but the asymmetry is exactly the trap that broke QNT-142's count
    check before symmetrising. Scoping on both checks keeps the two stores'
    contracts symmetric and makes regressions surface immediately.
    """
    from datetime import UTC, datetime

    from shared.tickers import TICKERS

    ch = _FakeClickHouse(
        query_df_responses=[
            ("SELECT id FROM equity_raw.news_raw", pd.DataFrame({"id": [1, 2, 3]})),
        ],
    )
    qdrant = _FakeQdrant(
        ids_by_ticker={t: [point_id(t, i) for i in [1, 2, 3]] for t in TICKERS},
    )

    before = int(datetime.now(UTC).timestamp())
    _run_check(
        news_embeddings_no_orphaned_vectors,
        asset=news_embeddings,
        resources={"clickhouse": ch, "qdrant": qdrant},
    )
    after = int(datetime.now(UTC).timestamp())

    # One scroll per ticker, all windowed.
    assert len(qdrant.scroll_filters) == len(TICKERS)
    seven_days_seconds = 7 * 24 * 60 * 60
    for f in qdrant.scroll_filters:
        # Same ticker + published_at Range shape as the count-check filter.
        assert len(f.must) == 2
        assert f.must[0].key == "ticker"
        assert f.must[1].key == "published_at"
        cutoff = f.must[1].range.gte
        assert cutoff is not None
        # ``gte`` cutoff lands within the window [now - 7d - test-fudge,
        # now - 7d + test-fudge]; if anyone reverts to the unscoped
        # ``ticker_filter`` (no Range), pyright still accepts but this
        # assertion fails because Range is None.
        assert before - seven_days_seconds - 5 <= cutoff <= after - seven_days_seconds + 5


def test_news_embeddings_embedding_dimension_passes_at_384() -> None:
    qdrant = _FakeQdrant(dimension=VECTOR_SIZE)
    # ClickHouse is unused by this check but the resource must still be present.
    e = _run_check(
        news_embeddings_embedding_dimension,
        asset=news_embeddings,
        resources={"clickhouse": _FakeClickHouse(), "qdrant": qdrant},
    )
    assert e.passed


def test_news_embeddings_embedding_dimension_fails_when_collection_drifts() -> None:
    qdrant = _FakeQdrant(dimension=512)
    e = _run_check(
        news_embeddings_embedding_dimension,
        asset=news_embeddings,
        resources={"clickhouse": _FakeClickHouse(), "qdrant": qdrant},
    )
    assert not e.passed
    assert e.metadata["configured_dimension"].value == 512


def test_news_embeddings_embedding_dimension_handles_missing_collection() -> None:
    """If collection_dimension raises (e.g. collection not yet created on first
    run), the check must surface a WARN failure rather than crashing."""
    qdrant = _FakeQdrant(dimension_raises=RuntimeError("collection not found"))
    e = _run_check(
        news_embeddings_embedding_dimension,
        asset=news_embeddings,
        resources={"clickhouse": _FakeClickHouse(), "qdrant": qdrant},
    )
    assert not e.passed
    assert "collection not found" in str(e.metadata["error"].value)
