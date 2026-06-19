"""Tests for the earnings_embeddings asset (QNT-260).

Mirrors test_news_embeddings.py: Qdrant Cloud Inference embeds server-side, so
the asset just sends ``Document(text, model)`` points; ``QdrantResource`` is
monkeypatched so ``uv run pytest`` passes offline. Unlike news (rolling 7-day
window + GC tail), earnings releases are quarterly and bounded, so the asset
embeds every stored release delta-only with no aged-out deletion.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import pytest
from dagster import build_asset_context
from dagster_pipelines.assets.earnings_embeddings import (
    COLLECTION,
    EMBED_MODEL,
    VECTOR_SIZE,
    earnings_embeddings,
    point_id,
)
from dagster_pipelines.edgar_feeds import chunk_release
from dagster_pipelines.resources.qdrant import QdrantCollectionSpec, QdrantResource
from qdrant_client.models import Document


@dataclass
class _UpsertCall:
    collection: str
    points: list[Any]


@dataclass
class _ScrollCall:
    collection: str
    ticker: str | None


@dataclass
class _Recorder:
    ensured: list[QdrantCollectionSpec] = field(default_factory=list)
    upserts: list[_UpsertCall] = field(default_factory=list)
    scrolls: list[_ScrollCall] = field(default_factory=list)
    existing_ids: list[int] = field(default_factory=list)


def _ticker_from_filter(query_filter: Any) -> str:
    return query_filter.must[0].match.value


@pytest.fixture
def qdrant_recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()

    def _ensure(self: QdrantResource, spec: QdrantCollectionSpec) -> None:
        recorder.ensured.append(spec)

    def _upsert(self: QdrantResource, collection: str, points: list[Any]) -> None:
        recorder.upserts.append(_UpsertCall(collection=collection, points=list(points)))

    def _scroll_ids(
        self: QdrantResource,
        collection: str,
        query_filter: Any | None = None,
        page_size: int = 10_000,
        max_pages: int = 100,
    ) -> list[int]:
        del page_size, max_pages
        ticker = _ticker_from_filter(query_filter) if query_filter is not None else None
        recorder.scrolls.append(_ScrollCall(collection=collection, ticker=ticker))
        return list(recorder.existing_ids)

    monkeypatch.setattr(QdrantResource, "ensure_collection", _ensure)
    monkeypatch.setattr(QdrantResource, "upsert_points", _upsert)
    monkeypatch.setattr(QdrantResource, "scroll_ids", _scroll_ids)
    return recorder


class _FakeClickHouse:
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df
        self.last_query: str | None = None
        self.last_parameters: dict[str, Any] | None = None

    def query_df(self, query: str, parameters: dict[str, Any] | None = None) -> pd.DataFrame:
        self.last_query = query
        self.last_parameters = parameters
        return self._df.copy()


# A body whose first line is a heading ("Outlook") followed by prose, so the
# chunker tags at least one chunk with a real section (verifies section-tagging).
_BODY = (
    "Outlook\n"
    "The company expects revenue to grow next quarter, with continued strength "
    "across its core segments and improving operating leverage over the period."
)
_DOC_ID = 111111111111111111


def _sample_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "doc_id": _DOC_ID,
                "ticker": "NVDA",
                "filing_date": datetime(2026, 5, 20, 0, 0, 0),
                "title": "NVIDIA Announces Financial Results for Q1 Fiscal 2027",
                "url": "https://www.sec.gov/Archives/edgar/data/1045810/x/pr.htm",
                "body": _BODY,
            }
        ]
    )


def test_builds_section_tagged_document_points(qdrant_recorder: _Recorder) -> None:
    """Happy path: each release body is chunked into section-tagged Document
    points; IDs are namespaced by (ticker, doc_id, chunk_index)."""
    clickhouse = _FakeClickHouse(_sample_df())
    ctx = build_asset_context(partition_key="NVDA")

    earnings_embeddings(
        context=ctx,
        clickhouse=clickhouse,  # type: ignore[arg-type]
        qdrant=QdrantResource(),
    )

    assert len(qdrant_recorder.ensured) == 1
    spec = qdrant_recorder.ensured[0]
    assert spec.name == COLLECTION
    assert spec.vector_size == VECTOR_SIZE
    assert spec.payload_indexes == {
        "ticker": "keyword",
        "doc_id": "integer",
        "filing_date": "integer",
        "section": "keyword",
    }

    expected_chunks = chunk_release(_BODY)
    assert len(qdrant_recorder.upserts) == 1
    points = qdrant_recorder.upserts[0].points
    assert len(points) == len(expected_chunks)

    first = points[0]
    assert first.id == point_id("NVDA", _DOC_ID, expected_chunks[0].index)
    assert isinstance(first.vector, Document)
    assert first.vector.model == EMBED_MODEL
    assert first.vector.text == expected_chunks[0].text
    # Section tagging present and filterable.
    assert first.payload["section"] == expected_chunks[0].section
    assert first.payload["ticker"] == "NVDA"
    assert first.payload["doc_id"] == _DOC_ID
    assert first.payload["chunk_index"] == expected_chunks[0].index
    expected_ts = int(datetime(2026, 5, 20, tzinfo=UTC).timestamp())
    assert first.payload["filing_date"] == expected_ts
    # A real section heading was detected (not everything defaulted to "Summary").
    assert any(p.payload["section"] == "Outlook" for p in points)


def test_query_scopes_to_partition_ticker(qdrant_recorder: _Recorder) -> None:
    clickhouse = _FakeClickHouse(_sample_df())
    ctx = build_asset_context(partition_key="NVDA")
    earnings_embeddings(
        context=ctx,
        clickhouse=clickhouse,  # type: ignore[arg-type]
        qdrant=QdrantResource(),
    )
    assert clickhouse.last_query is not None
    assert "equity_raw.earnings_releases_raw" in clickhouse.last_query
    assert "FINAL" in clickhouse.last_query
    assert clickhouse.last_parameters == {"ticker": "NVDA"}


def test_delta_only_skips_already_indexed_chunks(qdrant_recorder: _Recorder) -> None:
    """Chunks whose point_id is already in Qdrant must be skipped — re-embedding
    identical chunks would burn the free-tier inference budget."""
    chunks = chunk_release(_BODY)
    # Pre-seed every chunk's id as already indexed.
    qdrant_recorder.existing_ids = [point_id("NVDA", _DOC_ID, c.index) for c in chunks]

    ctx = build_asset_context(partition_key="NVDA")
    earnings_embeddings(
        context=ctx,
        clickhouse=_FakeClickHouse(_sample_df()),  # type: ignore[arg-type]
        qdrant=QdrantResource(),
    )

    assert len(qdrant_recorder.scrolls) == 1
    assert qdrant_recorder.scrolls[0].ticker == "NVDA"
    # All chunks already indexed → no upsert call at all.
    assert qdrant_recorder.upserts == []


def test_empty_partition_skips_qdrant(qdrant_recorder: _Recorder) -> None:
    clickhouse = _FakeClickHouse(
        pd.DataFrame(columns=["doc_id", "ticker", "filing_date", "title", "url", "body"])
    )
    ctx = build_asset_context(partition_key="NVDA")
    earnings_embeddings(
        context=ctx,
        clickhouse=clickhouse,  # type: ignore[arg-type]
        qdrant=QdrantResource(),
    )
    # Empty CH → short-circuit before scroll and upsert (no wasted round-trips).
    assert qdrant_recorder.scrolls == []
    assert qdrant_recorder.upserts == []


def test_point_id_namespacing_and_idempotency() -> None:
    # Same chunk re-derives the same id (idempotent upsert / RMT parity).
    assert point_id("NVDA", 42, 0) == point_id("NVDA", 42, 0)
    # Distinct by ticker, by doc, and by chunk index.
    assert point_id("NVDA", 42, 0) != point_id("AMD", 42, 0)
    assert point_id("NVDA", 42, 0) != point_id("NVDA", 43, 0)
    assert point_id("NVDA", 42, 0) != point_id("NVDA", 42, 1)


def test_point_id_fits_uint64() -> None:
    pid = point_id("AAPL", 2**63 - 1, 99)
    assert 0 <= pid < 2**64
