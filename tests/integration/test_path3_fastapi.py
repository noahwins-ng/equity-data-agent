"""Path 3: FastAPI endpoints exercised against a real ClickHouse (QNT-64).

Closes the QNT-148 mock-only coverage gap. Every API router that hits
ClickHouse — logos (no DB), quote, ohlcv, fundamentals, news, search,
agent_chat, reports, dashboard, indicators, tickers — gets at least one
test that drives the production code path with the real
``api.clickhouse.get_client`` against a fixture-loaded test DB.

What the QNT-148 hotfix proved: mock-only API tests can't catch a CTE
alias collision (``ILLEGAL_AGGREGATION``), an aggregate-inside-aggregate
(error code 184), or a GROUP BY scope mismatch — those bugs only surface
when the real SQL parser sees the query. These tests close the gap by
running every router's real query at least once.

Routers without a CH dependency (logos, search) are still exercised
end-to-end so their dependency wiring (Finnhub key absence, Qdrant outage
fallback) doesn't silently regress.

Conftest's autouse truncate fixture wipes equity_* tables before each
test, and ``api.clickhouse.get_client``'s lru_cache is cleared between
tests so a stale fake client from unit tests in the same session doesn't
leak in.
"""

from __future__ import annotations

from collections.abc import Iterator

import pandas as pd
import pytest
from api import clickhouse as api_clickhouse_module
from api import qdrant as api_qdrant_module
from api.main import app
from api.routers import logos as logos_module
from clickhouse_connect.driver.client import Client
from dagster_pipelines.assets.indicators.technical_indicators import compute_indicators
from fastapi.testclient import TestClient

from ._helpers import (
    seed_fundamental_summary,
    seed_fundamentals,
    seed_indicators_daily,
    seed_news,
    seed_ohlcv_from_fixture,
    seed_synthetic_ohlcv,
)


@pytest.fixture(autouse=True)
def _reset_api_caches() -> Iterator[None]:
    """Clear the API's lru_cache + per-process logo state between tests.

    ``api.clickhouse.get_client`` is cached process-wide; if a unit test
    earlier in the session cached a fake client, the integration test
    would silently inherit it. Same story for the logo cache and the
    prewarm-done event — both are module-level state in
    ``api.routers.logos``.
    """
    api_clickhouse_module.get_client.cache_clear()
    api_qdrant_module.get_client.cache_clear()
    logos_module._logo_cache.clear()
    logos_module._prewarm_done.clear()
    yield
    api_clickhouse_module.get_client.cache_clear()
    api_qdrant_module.get_client.cache_clear()
    logos_module._logo_cache.clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """FastAPI TestClient — the lifespan context wires CH + logo prewarm."""
    with TestClient(app) as c:
        yield c


def _seed_indicators(ch_client: Client, ticker: str) -> None:
    """Helper: seed OHLCV + computed indicators for ``ticker`` in one shot.

    Several Path-3 tests need both ``equity_raw.ohlcv_raw`` and
    ``equity_derived.technical_indicators_daily`` populated (the technical
    report joins them). Inlining the two-step seed here keeps each test
    body focused on what it's asserting.
    """
    seed_ohlcv_from_fixture(ch_client, ticker)
    _seed_indicators_only(ch_client, ticker)


def _seed_indicators_only(ch_client: Client, ticker: str) -> None:
    """Compute + persist indicators for ``ticker`` assuming OHLCV is loaded."""
    df = ch_client.query_df(
        "SELECT date, high, low, close, adj_close, volume "
        "FROM equity_raw.ohlcv_raw FINAL "
        "WHERE ticker = %(ticker)s "
        "ORDER BY date",
        parameters={"ticker": ticker},
    )
    df["date"] = pd.to_datetime(df["date"]).dt.date
    computed = compute_indicators(df)
    seed_indicators_daily(ch_client, ticker, computed)


# ─── Reports router (4 endpoints) ───────────────────────────────────────────


@pytest.mark.integration
def test_reports_technical_renders_against_real_sql(ch_client: Client, client: TestClient) -> None:
    """``/api/v1/reports/technical/AAPL`` returns a populated text report.

    Exercises the INNER JOIN between ``technical_indicators_daily`` and
    ``ohlcv_raw FINAL`` — a query mock-only tests would never catch a
    JOIN-condition typo in.
    """
    _seed_indicators(ch_client, "AAPL")
    r = client.get("/api/v1/reports/technical/AAPL")
    assert r.status_code == 200
    body = r.text
    assert "TECHNICAL REPORT" in body and "AAPL" in body
    assert "## PRICE ACTION" in body
    assert "## SIGNAL" in body


@pytest.mark.integration
def test_reports_fundamental_renders_against_real_sql(
    ch_client: Client, client: TestClient
) -> None:
    """``/api/v1/reports/fundamental/AAPL`` returns a populated text report.

    Real-SQL coverage for the quarterly-row pull from
    ``equity_derived.fundamental_summary``.
    """
    seed_fundamental_summary(ch_client, "AAPL")
    r = client.get("/api/v1/reports/fundamental/AAPL")
    assert r.status_code == 200
    body = r.text
    assert "FUNDAMENTAL REPORT" in body and "AAPL" in body


@pytest.mark.integration
def test_reports_news_renders_with_seeded_rows(ch_client: Client, client: TestClient) -> None:
    """``/api/v1/reports/news`` queries news_raw with ticker + 7-day window."""
    seed_news(ch_client, "AAPL", count=3)
    r = client.get("/api/v1/reports/news/AAPL")
    assert r.status_code == 200
    assert "NEWS REPORT" in r.text and "AAPL" in r.text


@pytest.mark.integration
def test_reports_summary_composes_three_subreports(ch_client: Client, client: TestClient) -> None:
    """Summary delegates to technical / fundamental / news in sequence.

    Three real queries fire end-to-end through the composer, so any single
    sub-template SQL bug surfaces here even when individual endpoints are
    fine in isolation. Exercises the `_safe` HTTPException demotion path
    too — the news section degrades gracefully on empty data.
    """
    _seed_indicators(ch_client, "AAPL")
    seed_fundamental_summary(ch_client, "AAPL")
    # Skip seeding news on purpose — exercises the "no data" branch.
    r = client.get("/api/v1/reports/summary/AAPL")
    assert r.status_code == 200
    body = r.text
    assert "SUMMARY REPORT" in body
    assert "TECHNICAL REPORT" in body  # delegated
    assert "FUNDAMENTAL REPORT" in body  # delegated


# ─── Data router (5 endpoints) ──────────────────────────────────────────────


@pytest.mark.integration
def test_data_ohlcv_returns_iso_date_rows_against_real_sql(
    ch_client: Client, client: TestClient
) -> None:
    """``/api/v1/ohlcv/AAPL`` reads ohlcv_raw FINAL and emits ISO-date rows."""
    seed_ohlcv_from_fixture(ch_client, "AAPL")
    r = client.get("/api/v1/ohlcv/AAPL")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list) and len(body) == 501
    # Ascending date order (chart-friendly).
    assert body[0]["time"] == "2023-01-03"
    assert body[-1]["time"] == "2024-12-30"


@pytest.mark.integration
def test_data_quote_runs_window_function_query(ch_client: Client, client: TestClient) -> None:
    """``/api/v1/quote/AAPL`` exercises the ``WITH ranked AS ...`` CTE.

    The QNT-148 class of bug — alias colliding with input column inside an
    aggregate — would surface as ``ILLEGAL_AGGREGATION`` at runtime. This
    test guarantees the rewrite (``today_open``/``today_volume``/``price``
    aliases) keeps working.
    """
    seed_synthetic_ohlcv(ch_client, "AAPL", days=60, base_price=100.0)
    seed_fundamental_summary(ch_client, "AAPL")
    seed_fundamentals(ch_client, "AAPL")  # market_cap source
    r = client.get("/api/v1/quote/AAPL")
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "AAPL"
    assert body["price"] is not None
    # >= 30 weekday bars in the synthetic seed → avg_volume_30d populated.
    assert body["avg_volume_30d"] is not None


@pytest.mark.integration
def test_data_fundamentals_runs_multi_join_with_window(
    ch_client: Client, client: TestClient
) -> None:
    """``/api/v1/fundamentals/AAPL`` runs the gross-profit-TTM window CTE.

    The endpoint joins ``fundamental_summary`` against two
    ``equity_raw.fundamentals`` derived subqueries plus a window-function
    CTE. Real-SQL coverage catches GROUP BY scope mismatches the mock
    tests can't.
    """
    seed_fundamentals(ch_client, "AAPL")
    seed_fundamental_summary(ch_client, "AAPL")
    r = client.get("/api/v1/fundamentals/AAPL")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) >= 1
    # Quarterly + TTM rows should be present in the ordered response.
    period_types = {row["period_type"] for row in body}
    assert "quarterly" in period_types
    assert "ttm" in period_types


@pytest.mark.integration
def test_data_indicators_returns_warmup_nulls(ch_client: Client, client: TestClient) -> None:
    """``/api/v1/indicators/AAPL`` preserves Nullable columns through JSON."""
    _seed_indicators(ch_client, "AAPL")
    r = client.get("/api/v1/indicators/AAPL")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 501
    # First row is in the warm-up: every indicator should be JSON null.
    first = body[0]
    assert first["sma_20"] is None
    assert first["sma_50"] is None
    assert first["rsi_14"] is None


@pytest.mark.integration
def test_data_news_dedups_by_id(ch_client: Client, client: TestClient) -> None:
    """``/api/v1/news/AAPL`` uses ``argMax + GROUP BY id`` — exercises the
    QNT-148 fix path (``latest_published_at`` alias avoiding aggregate-
    inside-aggregate)."""
    seed_news(ch_client, "AAPL", count=4)
    r = client.get("/api/v1/news/AAPL?days=7&limit=10")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 4
    # Most-recent-first ordering preserved.
    assert body[0]["headline"] == "AAPL headline 0"


@pytest.mark.integration
def test_data_dashboard_summary_runs_multi_cte_query(ch_client: Client, client: TestClient) -> None:
    """``/api/v1/dashboard/summary`` runs a 3-CTE LEFT JOIN cascade.

    This is the single biggest query the API ships — three CTEs joined on
    ticker. A regression in the LEFT JOIN ordering would silently drop
    indicators or sparkline arrays for tickers with partial data; running
    it against a real CH catches the shape failure that mocks can't.
    """
    # Seed two tickers so the GROUP BY paths in each CTE see > 1 partition
    seed_ohlcv_from_fixture(ch_client, "AAPL")
    seed_ohlcv_from_fixture(ch_client, "MSFT")
    _seed_indicators_only(ch_client, "AAPL")  # indicators without re-seeding OHLCV
    _seed_indicators_only(ch_client, "MSFT")
    r = client.get("/api/v1/dashboard/summary")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    seeded = {row["ticker"] for row in body if row["ticker"] in {"AAPL", "MSFT"}}
    assert seeded == {"AAPL", "MSFT"}
    for row in body:
        if row["ticker"] in seeded:
            assert isinstance(row["sparkline"], list)
            assert row["price"] is not None


# ─── Tickers + Logos + Search routers (no CH dependency) ────────────────────


@pytest.mark.integration
def test_tickers_endpoint_returns_full_universe(client: TestClient) -> None:
    """The tickers endpoint surfaces the in-process ``shared.tickers.TICKERS``."""
    r = client.get("/api/v1/tickers")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    # All 10 portfolio tickers; benchmark (SPY) is intentionally excluded
    # by the endpoint (it returns TICKERS, not ALL_OHLCV_TICKERS).
    assert "AAPL" in body and "NVDA" in body and "UNH" in body
    assert "SPY" not in body


@pytest.mark.integration
def test_logos_endpoint_returns_per_ticker_map_without_finnhub_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without FINNHUB_API_KEY the logo endpoint returns ``{ticker: null}``.

    The lifespan-spawned prewarm thread sees the empty key and short-
    circuits to caching ``None`` for every ticker; the endpoint then
    surfaces those nulls. This is the documented dev-without-key path —
    real-SQL coverage here means "real network short-circuit," not real
    Finnhub calls.

    Skips the shared ``client`` fixture so the env mutation lands BEFORE
    the lifespan starts the prewarm thread; otherwise a real key from
    ``.env`` would race the monkeypatch.
    """
    from shared.config import settings

    monkeypatch.setattr(settings, "FINNHUB_API_KEY", "")
    with TestClient(app) as c:
        r = c.get("/api/v1/logos")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {"AAPL", "NVDA", "MSFT"}
    assert all(v is None for v in body.values())


@pytest.mark.integration
def test_search_news_falls_back_to_empty_when_qdrant_misconfigured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without working Qdrant credentials the search endpoint returns ``[]``.

    The endpoint's ADR-014 contract: every Qdrant outage-shaped failure
    (network, missing collection, bad credentials) collapses to an empty
    200 response so the frontend renders identically to "no results."
    Verifying the fallback runs end-to-end ensures the QNT-55 wiring
    survives a refactor.
    """
    from shared.config import settings

    monkeypatch.setattr(settings, "QDRANT_URL", "http://127.0.0.1:1")  # closed port
    monkeypatch.setattr(settings, "QDRANT_API_KEY", "")
    r = client.get("/api/v1/search/news?query=earnings&ticker=AAPL")
    assert r.status_code == 200
    assert r.json() == []


# ─── Health + Agent chat routers ────────────────────────────────────────────


@pytest.mark.integration
def test_health_endpoint_runs_clickhouse_probe(
    client: TestClient,
) -> None:
    """``/api/v1/health`` runs ``SELECT 1`` against the real ClickHouse.

    A real probe means a live driver call — same bytes the deploy gate
    relies on. The status field collapses to ``ok`` only when CH is
    reachable; any regression in the connection settings flow surfaces
    here as a 503.
    """
    r = client.get("/api/v1/health")
    # 200 (ok / degraded) is acceptable; 503 (down) means CH probe failed.
    assert r.status_code == 200
    payload = r.json()
    assert payload["services"]["clickhouse"] == "ok"


@pytest.mark.integration
def test_agent_chat_streams_unknown_ticker_error(
    client: TestClient,
) -> None:
    """``POST /api/v1/agent/chat`` with an unknown ticker emits a sane SSE.

    Hits the SSE generator's unknown-ticker short-circuit so the per-IP
    budget bookkeeping path runs end-to-end. Avoids triggering the LLM
    (no real ticker, no graph invocation) — which would burn quota and
    require LiteLLM credentials in CI.
    """
    r = client.post("/api/v1/agent/chat", json={"ticker": "XXXX", "message": "test"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    assert "event: error" in body
    assert "unknown-ticker" in body
    assert "event: done" in body


# ─── Concurrent-query smoke test ────────────────────────────────────────────


@pytest.mark.integration
def test_concurrent_router_queries_dont_collide(ch_client: Client, client: TestClient) -> None:
    """Two routers fired in parallel both succeed against the same client.

    The ``api.clickhouse`` wrapper sets ``autogenerate_session_id=False``
    so concurrent requests don't trip "Attempt to execute concurrent
    queries within the same session." Running two endpoints in parallel
    against a real CH proves the fix is still in place.
    """
    import concurrent.futures

    seed_ohlcv_from_fixture(ch_client, "AAPL")
    _seed_indicators_only(ch_client, "AAPL")
    seed_fundamental_summary(ch_client, "AAPL")
    seed_fundamentals(ch_client, "AAPL")

    def hit(path: str) -> int:
        return client.get(path).status_code

    paths = [
        "/api/v1/ohlcv/AAPL",
        "/api/v1/indicators/AAPL",
        "/api/v1/fundamentals/AAPL",
        "/api/v1/quote/AAPL",
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        codes = list(executor.map(hit, paths))
    assert codes == [200, 200, 200, 200]
