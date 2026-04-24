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
from shared.tickers import TICKERS


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


_INDICATOR_COLS = (
    "time",
    "sma_20",
    "sma_50",
    "ema_12",
    "ema_26",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "bb_upper",
    "bb_middle",
    "bb_lower",
)


def _fake_indicator_result(rows: list[tuple[Any, ...]]) -> _FakeResult:
    return _FakeResult(_INDICATOR_COLS, rows)


def test_indicators_daily_returns_iso_date_and_all_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # First row is inside the SMA-50 warm-up window → nulls must survive the round-trip.
    fake = _FakeClient(
        _fake_indicator_result(
            [
                (
                    date(2026, 1, 2),
                    148.5,
                    None,
                    151.2,
                    146.8,
                    58.3,
                    1.2,
                    0.9,
                    0.3,
                    160.0,
                    148.5,
                    137.0,
                ),
                (
                    date(2026, 1, 3),
                    148.9,
                    142.1,
                    151.4,
                    147.0,
                    59.1,
                    1.3,
                    0.95,
                    0.35,
                    160.5,
                    149.0,
                    137.5,
                ),
            ]
        )
    )
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/indicators/NVDA")
    assert r.status_code == 200
    body = r.json()
    assert body[0] == {
        "time": "2026-01-02",
        "sma_20": 148.5,
        "sma_50": None,
        "ema_12": 151.2,
        "ema_26": 146.8,
        "rsi_14": 58.3,
        "macd": 1.2,
        "macd_signal": 0.9,
        "macd_hist": 0.3,
        "bb_upper": 160.0,
        "bb_middle": 148.5,
        "bb_lower": 137.0,
    }
    assert body[1]["sma_50"] == 142.1
    assert fake.last_query is not None
    assert "equity_derived.technical_indicators_daily" in fake.last_query
    assert "FINAL" in fake.last_query
    assert "ORDER BY date ASC" in fake.last_query
    assert fake.last_parameters == {"ticker": "NVDA"}


def test_indicators_weekly_uses_derived_weekly_table(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClient(
        _fake_indicator_result(
            [
                (
                    date(2026, 1, 5),
                    148.0,
                    142.0,
                    151.0,
                    146.0,
                    60.0,
                    1.5,
                    1.0,
                    0.5,
                    161.0,
                    149.0,
                    137.0,
                )
            ]
        )
    )
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/indicators/NVDA", params={"timeframe": "weekly"})
    assert r.status_code == 200
    assert r.json()[0]["time"] == "2026-01-05"
    assert fake.last_query is not None
    assert "equity_derived.technical_indicators_weekly" in fake.last_query
    assert "week_start" in fake.last_query


def test_indicators_monthly_uses_derived_monthly_table(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClient(
        _fake_indicator_result(
            [
                (
                    date(2026, 1, 1),
                    148.0,
                    142.0,
                    151.0,
                    146.0,
                    60.0,
                    1.5,
                    1.0,
                    0.5,
                    161.0,
                    149.0,
                    137.0,
                )
            ]
        )
    )
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/indicators/NVDA", params={"timeframe": "monthly"})
    assert r.status_code == 200
    assert r.json()[0]["time"] == "2026-01-01"
    assert fake.last_query is not None
    assert "equity_derived.technical_indicators_monthly" in fake.last_query
    assert "month_start" in fake.last_query


def test_indicators_unknown_ticker_returns_404(client: TestClient) -> None:
    r = client.get("/api/v1/indicators/BOGUS")
    assert r.status_code == 404
    assert "Unknown ticker" in r.json()["detail"]


def test_indicators_lowercase_ticker_is_normalized(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClient(_fake_indicator_result([]))
    _install_fake(monkeypatch, fake)
    r = client.get("/api/v1/indicators/nvda")
    assert r.status_code == 200
    assert fake.last_parameters == {"ticker": "NVDA"}


_FUNDAMENTAL_COLS = (
    "ticker",
    "period_end",
    "period_type",
    "pe_ratio",
    "ev_ebitda",
    "price_to_book",
    "price_to_sales",
    "eps",
    "revenue_yoy_pct",
    "net_income_yoy_pct",
    "fcf_yoy_pct",
    "net_margin_pct",
    "gross_margin_pct",
    "roe",
    "roa",
    "fcf_yield",
    "debt_to_equity",
    "current_ratio",
)


def test_fundamentals_returns_ratios_and_validates_ticker(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two rows: a fully-populated annual row, and a quarterly row whose
    # denominator-division fields are null to verify nulls round-trip.
    fake = _FakeClient(
        _FakeResult(
            _FUNDAMENTAL_COLS,
            [
                (
                    "NVDA",
                    date(2025, 12, 31),
                    "annual",
                    32.5,
                    22.1,
                    18.4,
                    15.0,
                    4.80,
                    60.0,
                    120.0,
                    85.0,
                    50.0,
                    75.0,
                    65.0,
                    35.0,
                    0.03,
                    0.25,
                    3.5,
                ),
                (
                    "NVDA",
                    date(2025, 9, 30),
                    "quarterly",
                    None,
                    None,
                    None,
                    None,
                    1.20,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            ],
        )
    )
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/fundamentals/nvda")
    assert r.status_code == 200
    body = r.json()
    assert body[0] == {
        "ticker": "NVDA",
        "period_end": "2025-12-31",
        "period_type": "annual",
        "pe_ratio": 32.5,
        "ev_ebitda": 22.1,
        "price_to_book": 18.4,
        "price_to_sales": 15.0,
        "eps": 4.80,
        "revenue_yoy_pct": 60.0,
        "net_income_yoy_pct": 120.0,
        "fcf_yoy_pct": 85.0,
        "net_margin_pct": 50.0,
        "gross_margin_pct": 75.0,
        "roe": 65.0,
        "roa": 35.0,
        "fcf_yield": 0.03,
        "debt_to_equity": 0.25,
        "current_ratio": 3.5,
    }
    assert body[1]["period_end"] == "2025-09-30"
    assert body[1]["pe_ratio"] is None
    assert fake.last_query is not None
    assert "equity_derived.fundamental_summary" in fake.last_query
    assert "FINAL" in fake.last_query
    assert "ORDER BY period_end DESC" in fake.last_query
    assert fake.last_parameters == {"ticker": "NVDA"}

    r_bad = client.get("/api/v1/fundamentals/BOGUS")
    assert r_bad.status_code == 404
    assert "Unknown ticker" in r_bad.json()["detail"]


_SUMMARY_COLS = ("ticker", "price", "prior_close", "rsi_14", "sma_50")


def test_dashboard_summary_categorizes_all_tickers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Three rows covering every categorization branch: overbought+bullish,
    # oversold+bearish, and a neutral/neutral row whose SMA-50 is still in the
    # 50-day warm-up window (must fall back to "neutral" trend).
    fake = _FakeClient(
        _FakeResult(
            _SUMMARY_COLS,
            [
                ("NVDA", 153.0, 149.49, 72.3, 140.0),
                ("AAPL", 180.0, 185.0, 28.5, 195.0),
                ("MSFT", 400.0, 400.0, 50.0, None),
            ],
        )
    )
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/dashboard/summary")
    assert r.status_code == 200
    body = r.json()
    by_ticker = {row["ticker"]: row for row in body}

    assert by_ticker["NVDA"] == {
        "ticker": "NVDA",
        "price": 153.0,
        "daily_change_pct": pytest.approx((153.0 - 149.49) / 149.49 * 100),
        "rsi_14": 72.3,
        "rsi_signal": "overbought",
        "trend_status": "bullish",
    }
    assert by_ticker["AAPL"]["rsi_signal"] == "oversold"
    assert by_ticker["AAPL"]["trend_status"] == "bearish"
    assert by_ticker["AAPL"]["daily_change_pct"] == pytest.approx((180.0 - 185.0) / 185.0 * 100)
    # SMA-50 null → trend collapses to neutral regardless of price.
    assert by_ticker["MSFT"]["trend_status"] == "neutral"
    assert by_ticker["MSFT"]["rsi_signal"] == "neutral"
    assert by_ticker["MSFT"]["daily_change_pct"] == pytest.approx(0.0)

    # Rows are emitted in TICKERS-registry order so the frontend doesn't re-sort.
    assert [row["ticker"] for row in body] == ["NVDA", "AAPL", "MSFT"]

    # Query must cover both source tables, use FINAL (ReplacingMergeTree), and
    # pass every configured ticker to ClickHouse in one round trip.
    assert fake.last_query is not None
    assert "equity_raw.ohlcv_raw" in fake.last_query
    assert "equity_derived.technical_indicators_daily" in fake.last_query
    assert fake.last_query.count("FINAL") >= 2
    assert fake.last_parameters == {"tickers": list(TICKERS)}


def test_dashboard_summary_handles_missing_prior_close(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Single-day history (no prior close) → daily_change_pct must be null
    # rather than throwing or defaulting to zero.
    fake = _FakeClient(_FakeResult(_SUMMARY_COLS, [("NVDA", 153.0, None, 50.0, 140.0)]))
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/dashboard/summary")
    assert r.status_code == 200
    assert r.json()[0]["daily_change_pct"] is None
