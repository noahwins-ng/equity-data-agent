"""Tests for the earnings_embeddings asset checks (QNT-263 follow-up).

``earnings_embeddings_all_releases_indexed`` was rewritten to derive the indexed
(ticker, doc_id) set from ONE ``scroll_payloads`` call instead of a per-release
``count()`` fan-out (the fan-out blew the Qdrant free-tier request-rate limit
under concurrent backfills and failed the check on fully-indexed data). These
run the check through the real Dagster wiring and assert pass/fail + metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from dagster import AssetChecksDefinition, Definitions
from dagster_pipelines.asset_checks.earnings_embeddings_checks import (
    earnings_embeddings_all_releases_indexed,
)
from dagster_pipelines.assets.earnings_embeddings import earnings_embeddings


@dataclass
class _FakeClickHouse:
    releases: pd.DataFrame = field(default_factory=pd.DataFrame)

    def query_df(self, query: str, parameters: dict[str, Any] | None = None) -> pd.DataFrame:
        assert "GROUP BY ticker, doc_id" in query
        return self.releases.copy()


@dataclass
class _FakeQdrant:
    payloads: list[dict[str, Any]] = field(default_factory=list)
    scroll_calls: int = 0

    def scroll_payloads(
        self,
        _collection: str,
        query_filter: Any | None = None,
        page_size: int = 10_000,
        max_pages: int = 100,
    ) -> list[dict[str, Any]]:
        del query_filter, page_size, max_pages
        self.scroll_calls += 1
        return list(self.payloads)


def _run_check(check: AssetChecksDefinition, *, asset: Any, resources: dict[str, Any]) -> Any:
    defs = Definitions(assets=[asset], asset_checks=[check], resources=resources)
    check_keys = set(defs.resolve_asset_graph().asset_check_keys)
    job = defs.resolve_implicit_global_asset_job_def().get_subset(
        asset_check_selection=check_keys, asset_selection=set()
    )
    result = job.execute_in_process(raise_on_error=False)
    evals = result.get_asset_check_evaluations()
    assert len(evals) == 1, f"expected 1 check eval, got {len(evals)}"
    return evals[0]


def _releases(*pairs: tuple[str, int]) -> pd.DataFrame:
    return pd.DataFrame([{"ticker": t, "doc_id": d} for t, d in pairs])


def test_passes_when_every_release_indexed_with_one_scroll() -> None:
    ch = _FakeClickHouse(releases=_releases(("NVDA", 1), ("NVDA", 2), ("AAPL", 9)))
    qd = _FakeQdrant(
        payloads=[
            {"ticker": "NVDA", "doc_id": 1, "chunk_index": 0},
            {"ticker": "NVDA", "doc_id": 1, "chunk_index": 1},
            {"ticker": "NVDA", "doc_id": 2, "chunk_index": 0},
            {"ticker": "AAPL", "doc_id": 9, "chunk_index": 0},
        ]
    )
    ev = _run_check(
        earnings_embeddings_all_releases_indexed,
        asset=earnings_embeddings,
        resources={"clickhouse": ch, "qdrant": qd},
    )
    assert ev.passed
    assert ev.metadata["unindexed_count"].value == 0
    assert ev.metadata["releases_checked"].value == 3
    # The rate-limit fix: one scroll for the whole corpus, not one call per release.
    assert qd.scroll_calls == 1


def test_fails_and_names_the_unindexed_release() -> None:
    ch = _FakeClickHouse(releases=_releases(("NVDA", 1), ("AAPL", 9)))
    # AAPL/9 produced no points (asset skipped it) -> only NVDA/1 is indexed.
    qd = _FakeQdrant(payloads=[{"ticker": "NVDA", "doc_id": 1, "chunk_index": 0}])
    ev = _run_check(
        earnings_embeddings_all_releases_indexed,
        asset=earnings_embeddings,
        resources={"clickhouse": ch, "qdrant": qd},
    )
    assert not ev.passed
    assert ev.metadata["unindexed_count"].value == 1
    assert ev.metadata["unindexed_releases"].value == [{"ticker": "AAPL", "doc_id": 9}]
