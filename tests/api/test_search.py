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
    """Records the last query_points call and returns canned scored points.

    QNT-262: optionally serves a ``scroll`` corpus for the hybrid path. ``scroll``
    raises if no corpus was supplied so a test that doesn't expect the hybrid
    branch fails loudly rather than silently scrolling an empty corpus.
    """

    def __init__(
        self,
        response: _FakeQueryResponse | None = None,
        *,
        raises: Exception | None = None,
        scroll_points: list[_FakeScoredPoint] | None = None,
    ) -> None:
        self._response = response or _FakeQueryResponse(points=[])
        self._raises = raises
        self._scroll_points = scroll_points
        self.last_collection: str | None = None
        self.last_query: Any = None
        self.last_filter: Filter | None = None
        self.last_limit: int | None = None
        self.scroll_calls = 0

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

    def scroll(
        self,
        collection_name: str,
        scroll_filter: Filter | None = None,
        limit: int = 256,
        with_payload: bool = True,
        with_vectors: bool = False,
        offset: Any = None,
    ) -> tuple[list[_FakeScoredPoint], Any]:
        self.scroll_calls += 1
        if self._scroll_points is None:
            raise AssertionError("scroll() called but no corpus configured")
        # Single page; second call (offset set) would not happen — return None
        # offset to terminate the pagination loop.
        return self._scroll_points, None


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
                        "body": "NVIDIA reported record data-center revenue.",
                    },
                ),
                _FakeScoredPoint(
                    # Within _RELEVANCE_GAP of the top hit so both survive the
                    # QNT-226 filter — this test pins payload shape + ranking +
                    # body passthrough, not the filter (covered separately).
                    point_id=222,
                    score=0.88,
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
    # QNT-225: body (the article summary) is surfaced; a point with no body
    # payload (pre-QNT-225 / headline-only) round-trips as "".
    assert r.json() == [
        {
            "headline": "NVDA beats earnings",
            "source": "yahoo_finance",
            "date": "2026-04-21",
            "score": 0.92,
            "url": "https://finance.example.com/a",
            "body": "NVIDIA reported record data-center revenue.",
        },
        {
            "headline": "Chip demand mixed",
            "source": "yahoo_finance",
            "date": "2026-04-20",
            "score": 0.88,
            "url": "https://finance.example.com/b",
            "body": "",
        },
    ]

    # Query was sent as a Document so Qdrant Cloud Inference embeds it with
    # the same model the news_embeddings asset uses — query + point vectors
    # must live in the same space.
    assert fake.last_collection == "equity_news"
    assert isinstance(fake.last_query, Document)
    assert fake.last_query.text == "earnings"
    assert fake.last_query.model == search_module.EMBED_MODEL


def test_relevance_filter_drops_cluster_tail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # QNT-226: scores mirror a real clean-window calibration pull (top hits
    # cluster ~0.53-0.59, a clear tail hit at 0.43). With _RELEVANCE_GAP=0.08
    # the cutoff is 0.59 - 0.08 = 0.51, so the three clustered hits survive and
    # the 0.43 tail is dropped. Pins the tail-trim behaviour without depending
    # on a query-independent absolute floor (which the calibration showed is
    # unsafe across queries).
    fake = _FakeClient(
        _FakeQueryResponse(
            points=[
                _FakeScoredPoint(point_id=1, score=0.59, payload={"headline": "strong A"}),
                _FakeScoredPoint(point_id=2, score=0.56, payload={"headline": "strong B"}),
                _FakeScoredPoint(point_id=3, score=0.53, payload={"headline": "strong C"}),
                _FakeScoredPoint(point_id=4, score=0.43, payload={"headline": "tail padding"}),
            ]
        )
    )
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/search/news", params={"query": "earnings", "ticker": "NVDA"})
    assert r.status_code == 200
    headlines = [row["headline"] for row in r.json()]
    assert headlines == ["strong A", "strong B", "strong C"]


def test_single_hit_survives_relevance_filter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lone hit is its own top score, so the gap can't strip it — a weak-signal
    # query that returns one match still surfaces it (above the degenerate floor).
    fake = _FakeClient(
        _FakeQueryResponse(
            points=[_FakeScoredPoint(point_id=9, score=0.42, payload={"headline": "only hit"})]
        )
    )
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/search/news", params={"query": "asml", "ticker": "TSLA"})
    assert r.status_code == 200
    assert [row["headline"] for row in r.json()] == ["only hit"]


def test_degenerate_query_below_floor_is_dropped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # QNT-226 degenerate-query guard: a query that matches nothing meaningful
    # returns uniformly sub-_MIN_SCORE (0.30) hits. The top-relative gap alone
    # would keep them (each is within the gap of the top), so the floor is what
    # drops them — a lone 0.28 hit is below the floor and filtered out.
    fake = _FakeClient(
        _FakeQueryResponse(
            points=[_FakeScoredPoint(point_id=7, score=0.28, payload={"headline": "noise"})]
        )
    )
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/search/news", params={"query": "zzzzz", "ticker": "NVDA"})
    assert r.status_code == 200
    assert r.json() == []


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


# --- QNT-262: hybrid (dense + BM25 RRF) + rerank --------------------------------


def _point(pid: int, headline: str, *, body: str = "", score: float = 0.5) -> _FakeScoredPoint:
    return _FakeScoredPoint(
        point_id=pid,
        score=score,
        payload={
            "ticker": "NVDA",
            "published_at": _ts(2026, 4, 20),
            "url": f"https://ex.com/{pid}",
            "headline": headline,
            "body": body,
            "source": "finnhub",
        },
    )


def test_hybrid_surfaces_lexical_only_hit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Dense returns two generic hits and misses the entity match; the corpus
    # carries a third doc that lexically contains "Hynix". BM25 ranks that doc,
    # RRF fuses it into the result the dense ranker alone would never surface --
    # the exact short-doc/entity case hybrid exists for.
    dense = _FakeQueryResponse(points=[_point(111, "NVDA data center"), _point(222, "chip demand")])
    corpus = [
        _point(111, "NVDA data center"),
        _point(222, "chip demand"),
        _point(333, "NVDA inks SK Hynix HBM supply deal"),
    ]
    fake = _FakeClient(dense, scroll_points=corpus)
    _install_fake(monkeypatch, fake)

    r = client.get(
        "/api/v1/search/news",
        params={"query": "SK Hynix supply", "ticker": "NVDA", "hybrid": True},
    )
    assert r.status_code == 200
    headlines = [row["headline"] for row in r.json()]
    assert "NVDA inks SK Hynix HBM supply deal" in headlines
    assert fake.scroll_calls == 1


def test_hybrid_without_ticker_falls_back_to_dense(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # hybrid=true but no ticker -> the BM25 scroll would be unbounded, so the
    # endpoint stays on the dense path and never scrolls.
    fake = _FakeClient(_FakeQueryResponse(points=[_point(1, "generic")]))
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/search/news", params={"query": "rate cut", "hybrid": True})
    assert r.status_code == 200
    assert fake.scroll_calls == 0


def test_hybrid_rerank_noops_without_cohere_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # rerank=true with no COHERE_API_KEY (default "") must degrade to the fused
    # order, not error -- the never-hard-dependency contract.
    monkeypatch.setattr(search_module.settings, "COHERE_API_KEY", "", raising=False)
    corpus = [_point(111, "alpha"), _point(222, "beta")]
    fake = _FakeClient(_FakeQueryResponse(points=corpus), scroll_points=corpus)
    _install_fake(monkeypatch, fake)

    r = client.get(
        "/api/v1/search/news",
        params={"query": "alpha", "ticker": "NVDA", "hybrid": True, "rerank": True},
    )
    assert r.status_code == 200
    # "alpha" lexically matches doc 111 -> it ranks at or above beta; both survive.
    assert {row["headline"] for row in r.json()} == {"alpha", "beta"}


def test_hybrid_rerank_reorders_when_key_present(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With a key, the Cohere layer reorders the fused candidates. Stub the rerank
    # client to invert the order and assert the response follows the reranker.
    monkeypatch.setattr(search_module.settings, "COHERE_API_KEY", "test-key", raising=False)
    corpus = [_point(111, "alpha"), _point(222, "beta")]
    fake = _FakeClient(_FakeQueryResponse(points=corpus), scroll_points=corpus)
    _install_fake(monkeypatch, fake)

    def _fake_rerank(query, documents, *, api_key, model, top_n):  # type: ignore[no-untyped-def]
        # Reverse the candidate order with descending relevance scores.
        ids = list(documents)[::-1]
        return [(doc_id, 0.9 - i * 0.1) for i, doc_id in enumerate(ids)]

    monkeypatch.setattr(search_module, "cohere_rerank", _fake_rerank)

    r = client.get(
        "/api/v1/search/news",
        params={"query": "alpha beta", "ticker": "NVDA", "hybrid": True, "rerank": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert [row["headline"] for row in body] == ["beta", "alpha"]
    # Reranked rows carry the Cohere relevance score, not a cosine.
    assert body[0]["score"] == 0.9
