"""Tests for the semantic news search endpoint (/api/v1/search/news).

Exercises the router end-to-end via TestClient with a fake Qdrant client —
mirrors the ``_FakeClient`` pattern in ``test_data.py`` so CI runs with no
live Qdrant Cloud connection (Phase 3 API-test baseline, tunnel-free).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from api import qdrant as qdrant_module
from api.main import app
from api.routers import search as search_module
from fastapi.testclient import TestClient
from qdrant_client.models import Document, FieldCondition, Filter


class _FakeScoredPoint:
    def __init__(self, *, point_id: int, score: float, payload: dict[str, Any]) -> None:
        self.id = point_id
        self.score = score
        self.payload = payload


class _FakeQueryResponse:
    def __init__(self, points: list[_FakeScoredPoint]) -> None:
        self.points = points


class _FakeClient:
    """Records the last query_points call and returns canned scored points."""

    def __init__(
        self,
        response: _FakeQueryResponse | None = None,
        *,
        raises: Exception | None = None,
    ) -> None:
        self._response = response or _FakeQueryResponse(points=[])
        self._raises = raises
        self.last_collection: str | None = None
        self.last_query: Any = None
        self.last_filter: Filter | None = None
        self.last_limit: int | None = None

    def query_points(
        self,
        collection_name: str,
        query: Any,
        query_filter: Filter | None = None,
        limit: int = 10,
        with_payload: bool = True,
    ) -> _FakeQueryResponse:
        self.last_collection = collection_name
        self.last_query = query
        self.last_filter = query_filter
        self.last_limit = limit
        if self._raises is not None:
            raise self._raises
        return self._response


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(qdrant_module, "get_client", lambda: fake)
    monkeypatch.setattr(search_module, "get_client", lambda: fake)


@pytest.fixture(autouse=True)
def _reset_client_cache() -> Iterable[None]:
    qdrant_module.get_client.cache_clear()
    yield
    qdrant_module.get_client.cache_clear()


@pytest.fixture
def client() -> Iterable[TestClient]:
    with TestClient(app) as c:
        yield c


def _ts(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp())


def test_returns_ranked_payload_with_iso_date(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two points, higher score first — the endpoint must preserve Qdrant's
    # ranking and surface the display fields (headline, source, date, score, url).
    fake = _FakeClient(
        _FakeQueryResponse(
            points=[
                _FakeScoredPoint(
                    point_id=111,
                    score=0.92,
                    payload={
                        "ticker": "NVDA",
                        "published_at": _ts(2026, 4, 21),
                        "url": "https://finance.example.com/a",
                        "headline": "NVDA beats earnings",
                        "source": "yahoo_finance",
                    },
                ),
                _FakeScoredPoint(
                    point_id=222,
                    score=0.81,
                    payload={
                        "ticker": "NVDA",
                        "published_at": _ts(2026, 4, 20),
                        "url": "https://finance.example.com/b",
                        "headline": "Chip demand mixed",
                        "source": "yahoo_finance",
                    },
                ),
            ]
        )
    )
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/search/news", params={"query": "earnings", "ticker": "NVDA"})
    assert r.status_code == 200
    assert r.json() == [
        {
            "headline": "NVDA beats earnings",
            "source": "yahoo_finance",
            "date": "2026-04-21",
            "score": 0.92,
            "url": "https://finance.example.com/a",
        },
        {
            "headline": "Chip demand mixed",
            "source": "yahoo_finance",
            "date": "2026-04-20",
            "score": 0.81,
            "url": "https://finance.example.com/b",
        },
    ]

    # Query was sent as a Document so Qdrant Cloud Inference embeds it with
    # the same model the news_embeddings asset uses — query + point vectors
    # must live in the same space.
    assert fake.last_collection == "equity_news"
    assert isinstance(fake.last_query, Document)
    assert fake.last_query.text == "earnings"
    assert fake.last_query.model == search_module.EMBED_MODEL


def test_ticker_filter_is_applied(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(_FakeQueryResponse(points=[]))
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/search/news", params={"query": "supply chain", "ticker": "aapl"})
    assert r.status_code == 200
    # Lowercase ticker is normalized to uppercase before filter construction.
    assert fake.last_filter is not None
    must = fake.last_filter.must
    assert isinstance(must, list) and len(must) == 1
    condition = must[0]
    assert isinstance(condition, FieldCondition)
    assert condition.key == "ticker"
    assert condition.match is not None
    assert getattr(condition.match, "value", None) == "AAPL"


def test_omitted_ticker_sends_no_filter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClient(_FakeQueryResponse(points=[]))
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/search/news", params={"query": "rate cut"})
    assert r.status_code == 200
    assert fake.last_filter is None


def test_unknown_ticker_returns_404(client: TestClient) -> None:
    r = client.get("/api/v1/search/news", params={"query": "earnings", "ticker": "BOGUS"})
    assert r.status_code == 404
    assert "Unknown ticker" in r.json()["detail"]


def test_limit_defaults_to_5_and_passes_through(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClient(_FakeQueryResponse(points=[]))
    _install_fake(monkeypatch, fake)

    client.get("/api/v1/search/news", params={"query": "earnings"})
    assert fake.last_limit == 5

    client.get("/api/v1/search/news", params={"query": "earnings", "limit": 12})
    assert fake.last_limit == 12


def test_limit_out_of_range_returns_422(client: TestClient) -> None:
    r = client.get("/api/v1/search/news", params={"query": "x", "limit": 0})
    assert r.status_code == 422
    r = client.get("/api/v1/search/news", params={"query": "x", "limit": 9999})
    assert r.status_code == 422


def test_empty_query_returns_422(client: TestClient) -> None:
    r = client.get("/api/v1/search/news", params={"query": ""})
    assert r.status_code == 422


def test_empty_results_returns_empty_list(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClient(_FakeQueryResponse(points=[]))
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/search/news", params={"query": "nonsense"})
    assert r.status_code == 200
    assert r.json() == []


def test_qdrant_unreachable_falls_back_to_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Transient Qdrant outage must degrade to [] with HTTP 200 so the frontend
    # renders "no news" rather than a hard error. Simulated with the actual
    # error type httpx raises on DNS/connect failure — the endpoint narrows
    # its except to network/transient classes so config/auth bugs still 500.
    fake = _FakeClient(raises=httpx.ConnectError("connection refused"))
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/search/news", params={"query": "earnings", "ticker": "NVDA"})
    assert r.status_code == 200
    assert r.json() == []


def test_missing_payload_fields_round_trip_as_null(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If a legacy point predates the `source` payload field, its absence must
    # surface as null rather than KeyError — guards against the 7-day
    # backfill window where older embeddings haven't been refreshed yet.
    fake = _FakeClient(
        _FakeQueryResponse(
            points=[
                _FakeScoredPoint(
                    point_id=333,
                    score=0.5,
                    payload={
                        "ticker": "NVDA",
                        "published_at": _ts(2026, 4, 19),
                        "url": "https://finance.example.com/c",
                        "headline": "Old point without source",
                    },
                )
            ]
        )
    )
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/search/news", params={"query": "x", "ticker": "NVDA"})
    body = r.json()
    assert body[0]["source"] is None
    assert body[0]["date"] == "2026-04-19"
