"""Path 4: Agent → Tools → API (QNT-64).

Verifies that the LangGraph agent's tool functions call into the FastAPI
surface and receive correctly-shaped report bodies. We don't run the LLM
or the graph — that's exercised in agent/tests — but we do drive the
tool → HTTP → API → ClickHouse → API → tool round-trip end-to-end.

Mechanism: monkeypatch ``agent.tools.httpx.get`` to forward through a
FastAPI ``TestClient``. TestClient's response object is API-compatible
with httpx.Response (``.text`` / ``.status_code`` / ``.json``) so the
tool's never-raise / [error] formatting paths run unmodified.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import pytest
from agent import tools as agent_tools
from api import clickhouse as api_clickhouse_module
from api import qdrant as api_qdrant_module
from api.main import app
from api.routers import logos as logos_module
from clickhouse_connect.driver.client import Client
from dagster_pipelines.assets.indicators.technical_indicators import compute_indicators
from fastapi.testclient import TestClient

from ._helpers import (
    seed_fundamental_summary,
    seed_indicators_daily,
    seed_news,
    seed_ohlcv_from_fixture,
)


@pytest.fixture(autouse=True)
def _reset_api_caches() -> Iterator[None]:
    api_clickhouse_module.get_client.cache_clear()
    api_qdrant_module.get_client.cache_clear()
    logos_module._logo_cache.clear()
    logos_module._prewarm_done.clear()
    yield
    api_clickhouse_module.get_client.cache_clear()
    api_qdrant_module.get_client.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def patched_tools(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """Redirect ``agent.tools`` HTTP calls through the FastAPI TestClient.

    Returns the TestClient so individual tests can sanity-check the same
    endpoint the tool will hit. Strips the configured ``API_BASE_URL``
    from the request URL so the relative path the TestClient accepts is
    derived without the test having to know what the base actually is.
    """

    def fake_get(url: str, *args: Any, **kwargs: Any) -> Any:
        parsed = urlparse(url)
        path = parsed.path
        if parsed.query:
            path = f"{path}?{parsed.query}"
        # TestClient.get accepts the same `params=` kwarg as httpx; pass
        # through if the caller used it.
        params = kwargs.get("params")
        if params is not None:
            return client.get(path, params=params)
        return client.get(path)

    monkeypatch.setattr(agent_tools.httpx, "get", fake_get)
    return client


def _seed_full_ticker(ch_client: Client, ticker: str) -> None:
    """Seed OHLCV + indicators + fundamentals + news for ``ticker``.

    Brings every report endpoint to a steady state in one call so the
    per-tool tests don't each repeat the same scaffolding.
    """
    seed_ohlcv_from_fixture(ch_client, ticker)
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
    seed_fundamental_summary(ch_client, ticker)
    seed_news(ch_client, ticker, count=3)


@pytest.mark.integration
def test_get_technical_report_returns_real_report_body(
    ch_client: Client, patched_tools: TestClient
) -> None:
    """The technical tool returns a populated report body, not an [error] string.

    Hitting the real API + real CH proves the tool's URL contract,
    response decoding, and the report template's SQL all line up.
    """
    _seed_full_ticker(ch_client, "AAPL")
    body = agent_tools.get_technical_report("AAPL")
    assert "TECHNICAL REPORT" in body
    assert "AAPL" in body
    assert not body.startswith("[error]")


@pytest.mark.integration
def test_get_fundamental_report_returns_real_report_body(
    ch_client: Client, patched_tools: TestClient
) -> None:
    _seed_full_ticker(ch_client, "AAPL")
    body = agent_tools.get_fundamental_report("AAPL")
    assert "FUNDAMENTAL REPORT" in body
    assert "AAPL" in body
    assert not body.startswith("[error]")


@pytest.mark.integration
def test_get_news_report_returns_seeded_headlines(
    ch_client: Client, patched_tools: TestClient
) -> None:
    """News tool surfaces the seeded headlines through the API + template."""
    _seed_full_ticker(ch_client, "AAPL")
    body = agent_tools.get_news_report("AAPL")
    assert "NEWS REPORT" in body
    assert "AAPL" in body
    # The seeded headlines start with "AAPL headline" (see _helpers.seed_news).
    assert "AAPL headline" in body


@pytest.mark.integration
def test_get_summary_report_composes_all_three(
    ch_client: Client, patched_tools: TestClient
) -> None:
    """Summary tool exercises the composer end-to-end via the agent path.

    Three real upstream queries fire under one tool call — the same
    surface the agent's plan→gather phase reads when the synthesize node
    needs an at-a-glance snapshot.
    """
    _seed_full_ticker(ch_client, "AAPL")
    body = agent_tools.get_summary_report("AAPL")
    assert "SUMMARY REPORT" in body
    assert "TECHNICAL REPORT" in body
    assert "FUNDAMENTAL REPORT" in body
    assert "NEWS REPORT" in body


@pytest.mark.integration
def test_unknown_ticker_short_circuits_without_calling_api(
    patched_tools: TestClient,
) -> None:
    """Unknown ticker returns ``[error]`` from the in-process validator.

    The tool layer rejects unknown tickers before the HTTP call (see
    ``_normalize_ticker``); this guards the per-tool retry budget from
    being burned on garbage input. Catches a regression that would
    silently demote unknown-ticker validation to the API layer's 404.
    """
    result = agent_tools.get_technical_report("NOTREAL")
    assert result.startswith("[error]")
    assert "unknown-ticker" in result


@pytest.mark.integration
def test_search_news_falls_back_to_empty_array_on_qdrant_outage(
    patched_tools: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """search_news degrades to ``"[]"`` when Qdrant is misconfigured.

    End-to-end coverage of the QNT-55 fallback: no Qdrant credentials →
    API returns ``[]`` (200) → tool returns ``"[]"`` to the agent. If a
    refactor accidentally let the 200-with-empty-list become a 500, the
    agent's retry loop would burn quota until exhaustion.
    """
    from shared.config import settings

    monkeypatch.setattr(settings, "QDRANT_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(settings, "QDRANT_API_KEY", "")
    out = agent_tools.search_news("AAPL", "earnings")
    assert out == "[]"


@pytest.mark.integration
def test_default_report_tools_dispatch_to_correct_endpoints(
    ch_client: Client, patched_tools: TestClient
) -> None:
    """The plan-shape tool map (company/technical/fundamental/news) routes correctly.

    The graph's plan node iterates over keys in this map. A typo in any
    one of the keys would silently break a plan — exercising the map keys
    end-to-end pins the contract. QNT-175 added ``company`` (static profile,
    no DB query) on top of the original trio.
    """
    _seed_full_ticker(ch_client, "AAPL")
    tools = agent_tools.default_report_tools()
    assert set(tools.keys()) == {"company", "technical", "fundamental", "news"}
    for name, fn in tools.items():
        body = fn("AAPL")
        assert not body.startswith("[error]"), f"tool {name} returned [error]: {body}"
        # Each report's first line carries its kind; cheap shape assertion
        # without coupling to specific wording.
        assert name.upper() in body.upper().split("\n", 1)[0]


@pytest.mark.integration
def test_tool_response_is_json_serialisable(ch_client: Client, patched_tools: TestClient) -> None:
    """Tool outputs survive ``json.dumps`` — the SSE serializer's contract.

    The SSE wrapper around tool calls (``api.routers.agent_chat``) JSON-
    serialises every event. A tool returning bytes or a non-string would
    crash the stream mid-flight; pin the contract here so the streamer
    doesn't have to defensively coerce.
    """
    _seed_full_ticker(ch_client, "AAPL")
    body = agent_tools.get_technical_report("AAPL")
    assert isinstance(body, str)
    # Round-trips through json.dumps without raising.
    encoded = json.dumps({"summary": body})
    assert "TECHNICAL REPORT" in json.loads(encoded)["summary"]
