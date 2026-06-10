"""Tests for the lean N-way comparison endpoint (QNT-224).

``GET /api/v1/reports/comparison-metrics?tickers=AAPL,MSFT,GOOGL`` returns one
compact metrics row per ticker (P/E, RSI, net margin, price). The builder
fires three separate ClickHouse queries (fundamentals / RSI / price) keyed by
table, so the fake client below branches on the table name rather than
returning one canned result for every query — a single-result fake would mask
a column-order or table-name regression (the QNT-148 CTE-alias lesson: mock
tests that don't model real row shape miss SQL bugs).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest
from api import clickhouse as clickhouse_module
from api import comparison_metrics as cm_module
from api.main import app
from fastapi.testclient import TestClient


class _FakeResult:
    def __init__(self, column_names: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
        self.column_names = column_names
        self.result_rows = rows


class _TableFake:
    """Routes each query to a canned result by the table it selects from.

    Records every query so a test can assert the SQL actually hit the table
    and grouping it intended (catches a builder pointed at the wrong table).
    """

    def __init__(self, results: dict[str, _FakeResult]) -> None:
        self._results = results
        self.queries: list[str] = []

    def query(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        self.queries.append(query)
        for marker, result in self._results.items():
            if marker in query:
                return result
        raise AssertionError(f"unexpected query, no fake for it:\n{query}")


# Real-shape rows: floats straight off ClickHouse argMax, mixed sign / N/M
# triggers exercised below. Column order matches the SELECT in each builder.
def _fundamentals(rows: list[tuple[Any, ...]]) -> _FakeResult:
    return _FakeResult(("ticker", "pe", "margin"), rows)


def _rsi(rows: list[tuple[Any, ...]]) -> _FakeResult:
    return _FakeResult(("ticker", "rsi"), rows)


def _price(rows: list[tuple[Any, ...]]) -> _FakeResult:
    return _FakeResult(("ticker", "price"), rows)


@pytest.fixture(autouse=True)
def _reset_client_cache() -> Iterable[None]:
    clickhouse_module.get_client.cache_clear()
    yield
    clickhouse_module.get_client.cache_clear()


@pytest.fixture
def client() -> Iterable[TestClient]:
    with TestClient(app) as c:
        yield c


def _install(monkeypatch: pytest.MonkeyPatch, fake: _TableFake) -> None:
    monkeypatch.setattr(cm_module, "get_client", lambda: fake)


@pytest.fixture(autouse=True)
def _stub_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """QNT-224 follow-up: the valuation/trend labels reuse the report DB-fetch
    path (their own ``get_client``), so stub them off by default to keep the
    metrics-assembly tests focused on the three argMax queries. One dedicated
    test overrides these to assert the labels propagate into the row."""
    monkeypatch.setattr(cm_module, "compute_valuation_label", lambda _t: None)
    monkeypatch.setattr(cm_module, "compute_trend_label", lambda _t, _tf="daily": None)


def test_three_way_returns_formatted_rows_in_order(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _TableFake(
        {
            "fundamental_summary": _fundamentals(
                [
                    ("AAPL", 28.43, 24.13),
                    ("MSFT", 34.10, 36.82),
                    ("GOOGL", 24.71, 28.04),
                ]
            ),
            "technical_indicators_daily": _rsi([("AAPL", 65.21), ("MSFT", 58.0), ("GOOGL", 47.33)]),
            "ohlcv_raw": _price([("AAPL", 182.5), ("MSFT", 410.12), ("GOOGL", 151.0)]),
        }
    )
    _install(monkeypatch, fake)

    resp = client.get("/api/v1/reports/comparison-metrics?tickers=AAPL,MSFT,GOOGL")
    assert resp.status_code == 200
    rows = resp.json()["rows"]

    # Order follows the request, not the DB return order.
    assert [r["ticker"] for r in rows] == ["AAPL", "MSFT", "GOOGL"]
    assert rows[0] == {
        "ticker": "AAPL",
        "pe": "28.4",
        "rsi": "65.2",
        "net_margin": "24.1%",
        "price": "$182.50",
        "valuation_label": None,
        "trend_daily": None,
        "trend_weekly": None,
    }
    assert rows[1]["price"] == "$410.12"
    assert rows[2]["net_margin"] == "28.0%"

    # Three distinct queries, one per source table, each grouped by ticker.
    joined = "\n".join(fake.queries)
    assert "fundamental_summary" in joined
    assert "period_type = 'quarterly'" in joined
    assert "technical_indicators_daily" in joined
    assert "ohlcv_raw" in joined
    assert joined.count("GROUP BY ticker") == 3


def test_missing_row_renders_nm_not_dropped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GOOGL absent from price + fundamentals; its row must still appear, N/M.
    fake = _TableFake(
        {
            "fundamental_summary": _fundamentals([("AAPL", 28.4, 24.1), ("MSFT", 34.1, 36.8)]),
            "technical_indicators_daily": _rsi([("AAPL", 65.0), ("MSFT", 58.0), ("GOOGL", 47.0)]),
            "ohlcv_raw": _price([("AAPL", 182.5), ("MSFT", 410.0)]),
        }
    )
    _install(monkeypatch, fake)

    resp = client.get("/api/v1/reports/comparison-metrics?tickers=AAPL,MSFT,GOOGL")
    assert resp.status_code == 200
    rows = {r["ticker"]: r for r in resp.json()["rows"]}
    assert set(rows) == {"AAPL", "MSFT", "GOOGL"}
    assert rows["GOOGL"]["rsi"] == "47.0"
    assert rows["GOOGL"]["price"].startswith("N/M")
    assert rows["GOOGL"]["pe"].startswith("N/M")


def test_unknown_and_extra_tickers_are_filtered_and_capped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _TableFake(
        {
            "fundamental_summary": _fundamentals(
                [
                    ("AAPL", 28.4, 24.1),
                    ("MSFT", 34.1, 36.8),
                    ("GOOGL", 24.7, 28.0),
                    ("AMZN", 40.0, 8.0),
                ]
            ),
            "technical_indicators_daily": _rsi(
                [("AAPL", 65.0), ("MSFT", 58.0), ("GOOGL", 47.0), ("AMZN", 55.0)]
            ),
            "ohlcv_raw": _price(
                [("AAPL", 182.5), ("MSFT", 410.0), ("GOOGL", 151.0), ("AMZN", 190.0)]
            ),
        }
    )
    _install(monkeypatch, fake)

    # FAKETICKER dropped (unknown); five valid -> capped at four.
    resp = client.get(
        "/api/v1/reports/comparison-metrics?tickers=AAPL,FAKETICKER,MSFT,GOOGL,AMZN,TSLA"
    )
    assert resp.status_code == 200
    tickers = [r["ticker"] for r in resp.json()["rows"]]
    assert tickers == ["AAPL", "MSFT", "GOOGL", "AMZN"]


def test_fewer_than_two_valid_tickers_is_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _TableFake({})  # no query should run — guard fires first
    _install(monkeypatch, fake)

    resp = client.get("/api/v1/reports/comparison-metrics?tickers=AAPL,NOPE")
    assert resp.status_code == 400
    assert fake.queries == []


def test_valuation_and_trend_labels_propagate_into_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # QNT-224 follow-up: the labels the fundamental + technical reports compute
    # ride into the row verbatim; None (suppressed/N-M) is preserved as null.
    fake = _TableFake(
        {
            "fundamental_summary": _fundamentals(
                [("AAPL", 28.4, 24.1), ("MSFT", 34.1, 36.8), ("GOOGL", 24.7, 28.0)]
            ),
            "technical_indicators_daily": _rsi([("AAPL", 65.0), ("MSFT", 58.0), ("GOOGL", 47.0)]),
            "ohlcv_raw": _price([("AAPL", 182.5), ("MSFT", 410.0), ("GOOGL", 151.0)]),
        }
    )
    _install(monkeypatch, fake)
    monkeypatch.setattr(
        cm_module,
        "compute_valuation_label",
        lambda t: {"AAPL": "Premium", "MSFT": "Inline"}.get(t),  # GOOGL -> None (suppressed)
    )
    # Returns a different label per timeframe so the daily/weekly split is exercised.
    daily = {"AAPL": "Uptrend", "MSFT": "Sideways", "GOOGL": "Downtrend"}
    weekly = {"AAPL": "Sideways", "MSFT": "Uptrend", "GOOGL": "Uptrend"}
    monkeypatch.setattr(
        cm_module,
        "compute_trend_label",
        lambda t, tf="daily": (daily if tf == "daily" else weekly).get(t),
    )

    resp = client.get("/api/v1/reports/comparison-metrics?tickers=AAPL,MSFT,GOOGL")
    assert resp.status_code == 200
    rows = {r["ticker"]: r for r in resp.json()["rows"]}
    assert rows["AAPL"]["valuation_label"] == "Premium"
    assert rows["AAPL"]["trend_daily"] == "Uptrend"
    assert rows["AAPL"]["trend_weekly"] == "Sideways"
    assert rows["MSFT"]["valuation_label"] == "Inline"
    assert rows["GOOGL"]["valuation_label"] is None  # suppressed -> null, not a string
    assert rows["GOOGL"]["trend_daily"] == "Downtrend"
    assert rows["GOOGL"]["trend_weekly"] == "Uptrend"
