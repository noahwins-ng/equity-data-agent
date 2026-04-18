"""Tests for data endpoints (/api/v1/ohlcv/{ticker}).

These endpoints return JSON arrays shaped for TradingView Lightweight Charts:
``{time, open, high, low, close, adj_close, volume}[]`` where ``time`` is an
ISO date string. Tests exercise the router end-to-end via TestClient with a
fake ClickHouse client — no tunnel required.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

import pytest
from api import clickhouse as clickhouse_module
from api.main import app
from api.routers import data as data_module
from fastapi.testclient import TestClient


class _FakeResult:
    def __init__(self, column_names: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
        self.column_names = column_names
        self.result_rows = rows


class _FakeClient:
    """Returns canned rows whichever table is hit; records the last query."""

    def __init__(self, result: _FakeResult) -> None:
        self._result = result
        self.last_query: str | None = None
        self.last_parameters: dict[str, Any] | None = None

    def query(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        self.last_query = query
        self.last_parameters = parameters
        return self._result


_COLS = ("time", "open", "high", "low", "close", "adj_close", "volume")


def _fake_result(rows: list[tuple[Any, ...]]) -> _FakeResult:
    return _FakeResult(_COLS, rows)


@pytest.fixture(autouse=True)
def _reset_client_cache() -> Iterable[None]:
    clickhouse_module.get_client.cache_clear()
    yield
    clickhouse_module.get_client.cache_clear()


@pytest.fixture
def client() -> Iterable[TestClient]:
    with TestClient(app) as c:
        yield c


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(clickhouse_module, "get_client", lambda: fake)
    monkeypatch.setattr(data_module, "get_client", lambda: fake)


def test_ohlcv_daily_returns_iso_date_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClient(
        _fake_result(
            [
                (date(2026, 1, 2), 150.0, 155.0, 149.0, 153.0, 15.3, 12345678),
                (date(2026, 1, 3), 153.0, 158.0, 152.0, 157.0, 15.7, 22222222),
            ]
        )
    )
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/ohlcv/NVDA")
    assert r.status_code == 200
    body = r.json()
    assert body == [
        {
            "time": "2026-01-02",
            "open": 150.0,
            "high": 155.0,
            "low": 149.0,
            "close": 153.0,
            "adj_close": 15.3,
            "volume": 12345678,
        },
        {
            "time": "2026-01-03",
            "open": 153.0,
            "high": 158.0,
            "low": 152.0,
            "close": 157.0,
            "adj_close": 15.7,
            "volume": 22222222,
        },
    ]
    # Daily → raw OHLCV table, ordered ascending so charts render left-to-right.
    assert fake.last_query is not None
    assert "equity_raw.ohlcv_raw" in fake.last_query
    assert "FINAL" in fake.last_query
    assert "ORDER BY date ASC" in fake.last_query
    assert fake.last_parameters == {"ticker": "NVDA"}


def test_ohlcv_weekly_uses_derived_weekly_table(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClient(_fake_result([(date(2026, 1, 5), 150.0, 160.0, 148.0, 158.0, 15.8, 99999)]))
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/ohlcv/NVDA", params={"timeframe": "weekly"})
    assert r.status_code == 200
    assert r.json()[0]["time"] == "2026-01-05"
    assert fake.last_query is not None
    assert "equity_derived.ohlcv_weekly" in fake.last_query
    assert "week_start" in fake.last_query


def test_ohlcv_monthly_uses_derived_monthly_table(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClient(_fake_result([(date(2026, 1, 1), 150.0, 170.0, 145.0, 168.0, 16.8, 888888)]))
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/ohlcv/NVDA", params={"timeframe": "monthly"})
    assert r.status_code == 200
    assert r.json()[0]["time"] == "2026-01-01"
    assert fake.last_query is not None
    assert "equity_derived.ohlcv_monthly" in fake.last_query
    assert "month_start" in fake.last_query


def test_ohlcv_unknown_ticker_returns_404(client: TestClient) -> None:
    r = client.get("/api/v1/ohlcv/BOGUS")
    assert r.status_code == 404
    assert "Unknown ticker" in r.json()["detail"]


def test_ohlcv_lowercase_ticker_is_normalized(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClient(_fake_result([]))
    _install_fake(monkeypatch, fake)
    r = client.get("/api/v1/ohlcv/nvda")
    assert r.status_code == 200
    assert fake.last_parameters == {"ticker": "NVDA"}


def test_ohlcv_invalid_timeframe_returns_422(client: TestClient) -> None:
    r = client.get("/api/v1/ohlcv/NVDA", params={"timeframe": "hourly"})
    assert r.status_code == 422
