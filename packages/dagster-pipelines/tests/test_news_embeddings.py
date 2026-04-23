"""Tests for the news_embeddings asset (QNT-54).

Qdrant Cloud Inference (ADR-009) handles embedding server-side — the asset
just sends ``Document(text, model)`` points. Tests monkeypatch
``QdrantResource`` so ``uv run pytest`` passes offline without a Qdrant Cloud
account. Pattern mirrors ``_FakeClient`` in packages/api/tests/test_data.py
(Phase 3 retro lesson).

Why monkeypatch instead of subclassing: Dagster rebuilds
``ConfigurableResource`` instances when binding them to an asset invocation,
which strips subclass method overrides. Patching the base class's methods
on the instance (or class, inside a fixture) survives that rebuild because
it targets the Python class itself.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import pytest
from dagster import build_asset_context
from dagster_pipelines.assets.news_embeddings import (
    COLLECTION,
    EMBED_MODEL,
    VECTOR_SIZE,
    news_embeddings,
    point_id,
)
from dagster_pipelines.resources.qdrant import QdrantCollectionSpec, QdrantResource
from qdrant_client.models import Document

# ── Recorder ──────────────────────────────────────────────────────────────────


@dataclass
class _UpsertCall:
    collection: str
    points: list[Any]


@dataclass
class _Recorder:
    ensured: list[QdrantCollectionSpec] = field(default_factory=list)
    upserts: list[_UpsertCall] = field(default_factory=list)


@pytest.fixture
def qdrant_recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Patch ``QdrantResource`` to record calls instead of hitting Qdrant Cloud.

    Yields a fresh ``_Recorder`` per test. Patching the base class methods
    (not a subclass) means Dagster's resource rebuild on asset invocation
    preserves the patch — the methods live on the class, not the instance.
    """
    recorder = _Recorder()

    def _ensure(self: QdrantResource, spec: QdrantCollectionSpec) -> None:
        recorder.ensured.append(spec)

    def _upsert(self: QdrantResource, collection: str, points: list[Any]) -> None:
        recorder.upserts.append(_UpsertCall(collection=collection, points=list(points)))

    monkeypatch.setattr(QdrantResource, "ensure_collection", _ensure)
    monkeypatch.setattr(QdrantResource, "upsert_points", _upsert)
    return recorder


# ── ClickHouse fake ───────────────────────────────────────────────────────────


class _FakeClickHouse:
    """Stubs the ClickHouseResource surface used by the asset."""

    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df
        self.last_query: str | None = None
        self.last_parameters: dict[str, Any] | None = None

    def query_df(self, query: str, parameters: dict[str, Any] | None = None) -> pd.DataFrame:
        self.last_query = query
        self.last_parameters = parameters
        return self._df.copy()


def _sample_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "id": 111111111111111111,
                "ticker": "NVDA",
                "headline": "NVDA beats earnings, stock jumps 10%",
                "url": "https://finance.example.com/a",
                "source": "yahoo_finance",
                "published_at": datetime(2026, 4, 21, 14, 30, 0),
            },
            {
                "id": 222222222222222222,
                "ticker": "NVDA",
                "headline": "Chip demand outlook mixed for Q2",
                "url": "https://finance.example.com/b",
                "source": "yahoo_finance",
                "published_at": datetime(2026, 4, 22, 9, 0, 0),
            },
        ]
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_builds_document_points_and_upserts(qdrant_recorder: _Recorder) -> None:
    """Happy path: ClickHouse returns rows → each headline becomes a Qdrant
    point whose vector is a Document(text, model) for server-side embedding.
    Point ID is ``point_id(ticker, news_raw.id)`` so cross-mentioned URLs
    land as distinct points per ticker (QNT-120)."""
    clickhouse = _FakeClickHouse(_sample_df())
    ctx = build_asset_context(partition_key="NVDA")

    news_embeddings(
        context=ctx,
        clickhouse=clickhouse,  # type: ignore[arg-type]
        qdrant=QdrantResource(),
    )

    # Collection ensured exactly once with the expected payload indexes
    # (AC "filter by ticker + published_at range" requires these indexes).
    assert len(qdrant_recorder.ensured) == 1
    spec = qdrant_recorder.ensured[0]
    assert spec.name == COLLECTION
    assert spec.vector_size == VECTOR_SIZE
    assert spec.distance == "Cosine"
    assert spec.payload_indexes == {"ticker": "keyword", "published_at": "integer"}

    # One upsert of two points; IDs namespaced by ticker; vectors are Document
    # objects (Qdrant embeds them server-side); payload has filterable ticker
    # + published_at and round-trippable url + headline.
    assert len(qdrant_recorder.upserts) == 1
    call = qdrant_recorder.upserts[0]
    assert call.collection == COLLECTION
    assert len(call.points) == 2

    first, second = call.points
    assert first.id == point_id("NVDA", 111111111111111111)
    assert second.id == point_id("NVDA", 222222222222222222)

    assert isinstance(first.vector, Document)
    assert first.vector.text == "NVDA beats earnings, stock jumps 10%"
    assert first.vector.model == EMBED_MODEL
    assert isinstance(second.vector, Document)
    assert second.vector.text == "Chip demand outlook mixed for Q2"
    assert second.vector.model == EMBED_MODEL

    # pd.Timestamp.timestamp() on a naive value treats it as UTC (ClickHouse's
    # native storage); matches the asset's conversion path.
    expected_ts = datetime(2026, 4, 21, 14, 30, 0, tzinfo=UTC).timestamp()
    assert first.payload == {
        "ticker": "NVDA",
        "published_at": int(expected_ts),
        "url": "https://finance.example.com/a",
        "headline": "NVDA beats earnings, stock jumps 10%",
        "source": "yahoo_finance",
    }
    assert {p.payload["ticker"] for p in call.points} == {"NVDA"}


def test_query_filters_by_ticker_and_7_day_window(qdrant_recorder: _Recorder) -> None:
    """The asset must pass partition_key as the ticker parameter and scope the
    query to the 7-day fresh window — otherwise a backfill would re-embed the
    full news history."""
    clickhouse = _FakeClickHouse(_sample_df())
    ctx = build_asset_context(partition_key="NVDA")

    news_embeddings(
        context=ctx,
        clickhouse=clickhouse,  # type: ignore[arg-type]
        qdrant=QdrantResource(),
    )

    assert clickhouse.last_query is not None
    assert "equity_raw.news_raw" in clickhouse.last_query
    assert "INTERVAL 7 DAY" in clickhouse.last_query
    assert "FINAL" in clickhouse.last_query
    assert clickhouse.last_parameters == {"ticker": "NVDA"}


def test_empty_partition_skips_qdrant_write(qdrant_recorder: _Recorder) -> None:
    """If the 7-day window has no news for the ticker (common on quiet days or
    first-run tickers), the asset must not call upsert at all."""
    clickhouse = _FakeClickHouse(
        pd.DataFrame(columns=["id", "ticker", "headline", "url", "source", "published_at"])
    )
    ctx = build_asset_context(partition_key="NVDA")

    news_embeddings(
        context=ctx,
        clickhouse=clickhouse,  # type: ignore[arg-type]
        qdrant=QdrantResource(),
    )

    assert qdrant_recorder.upserts == []


def test_cloud_inference_enabled_by_default() -> None:
    """Production QdrantResource should ship with cloud_inference=True so the
    asset can pass Document points and have Qdrant embed server-side (ADR-009)."""
    assert QdrantResource().cloud_inference is True


# ── point_id helper (QNT-120) ─────────────────────────────────────────────────


def test_point_id_cross_ticker_same_url_differs() -> None:
    """Same URL under different tickers → distinct Qdrant IDs. This is the core
    QNT-120 invariant — without it, the last ticker's upsert overwrites the
    others and cross-mentioned articles silently disappear from per-ticker
    ticker-filtered search."""
    url_id = 123456789
    assert point_id("MSFT", url_id) != point_id("TSLA", url_id)


def test_point_id_same_ticker_same_url_idempotent() -> None:
    """Same ``(ticker, url_id)`` → same point ID across calls. Required so that
    Qdrant upsert dedups the same row on re-embed (matching ClickHouse's
    ReplacingMergeTree dedup on ``(ticker, published_at, id)``)."""
    assert point_id("NVDA", 42) == point_id("NVDA", 42)


def test_point_id_fits_uint64() -> None:
    """Qdrant accepts 64-bit unsigned integer IDs; the blake2b digest_size=8
    output must fit. Guards against an accidental bump to digest_size=16
    that would silently overflow Qdrant's ID validator."""
    pid = point_id("AAPL", 2**63 - 1)
    assert 0 <= pid < 2**64


def test_cross_ticker_dataframe_produces_distinct_ids(qdrant_recorder: _Recorder) -> None:
    """Two rows for the SAME url_id but different tickers (the cross-mention
    case) must produce two distinct Qdrant points — one per ticker. The asset
    runs per-partition so we simulate MSFT's partition with its slice of the
    cross-mentioned row, and verify the ID matches what TSLA's partition would
    NOT collide with."""
    shared_url_id = 3849346792762833023
    msft_df = pd.DataFrame(
        [
            {
                "id": shared_url_id,
                "ticker": "MSFT",
                "headline": "Tesla Q1 earnings review",
                "url": "https://example.com/shared-article",
                "source": "yahoo_finance",
                "published_at": datetime(2026, 4, 21, 14, 30, 0),
            }
        ]
    )
    ctx = build_asset_context(partition_key="MSFT")
    news_embeddings(
        context=ctx,
        clickhouse=_FakeClickHouse(msft_df),  # type: ignore[arg-type]
        qdrant=QdrantResource(),
    )

    assert len(qdrant_recorder.upserts) == 1
    point = qdrant_recorder.upserts[0].points[0]
    assert point.id == point_id("MSFT", shared_url_id)
    assert point.id != point_id("TSLA", shared_url_id)
    assert point.payload["ticker"] == "MSFT"
