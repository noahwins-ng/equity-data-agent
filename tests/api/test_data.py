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


def test_ohlcv_benchmark_ticker_spy_is_allowed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SPY is a benchmark — not in TICKERS but valid for /ohlcv. Verifies the
    # benchmark routes through ALL_OHLCV_TICKERS without leaking onto the
    # fundamentals/news endpoints.
    fake = _FakeClient(
        _fake_result([(date(2026, 1, 2), 480.0, 482.0, 478.0, 481.0, 481.0, 50000000)])
    )
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/ohlcv/SPY")
    assert r.status_code == 200
    assert r.json()[0]["time"] == "2026-01-02"
    assert fake.last_parameters == {"ticker": "SPY"}


def test_fundamentals_rejects_spy(client: TestClient) -> None:
    # Benchmark tickers must NOT leak onto fundamentals — they have no
    # fundamentals data and should 404 the same as unknown symbols do.
    r = client.get("/api/v1/fundamentals/SPY")
    assert r.status_code == 404
    assert "Unknown ticker" in r.json()["detail"]


def test_indicators_rejects_spy(client: TestClient) -> None:
    # Same gate: indicators partition on TICKERS only.
    r = client.get("/api/v1/indicators/SPY")
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
    "sma_200",
    "ema_12",
    "ema_26",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "macd_bullish_cross",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "bb_pct_b",
    "adx_14",
    "atr_14",
    "obv",
)


def _fake_indicator_result(rows: list[tuple[Any, ...]]) -> _FakeResult:
    return _FakeResult(_INDICATOR_COLS, rows)


def test_indicators_daily_returns_iso_date_and_all_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # First row is inside the SMA-200 warm-up window → nulls must survive the
    # round-trip. macd_bullish_cross is a UInt8 0/1 flag without warm-up.
    fake = _FakeClient(
        _fake_indicator_result(
            [
                (
                    date(2026, 1, 2),
                    148.5,
                    142.0,
                    None,
                    151.2,
                    146.8,
                    58.3,
                    1.2,
                    0.9,
                    0.3,
                    0,
                    160.0,
                    148.5,
                    137.0,
                    0.55,
                    None,
                    None,
                    None,
                ),
                (
                    date(2026, 1, 3),
                    148.9,
                    142.1,
                    140.0,
                    151.4,
                    147.0,
                    59.1,
                    1.3,
                    0.95,
                    0.35,
                    1,
                    160.5,
                    149.0,
                    137.5,
                    0.62,
                    25.4,
                    3.1,
                    1234567.0,
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
        "sma_50": 142.0,
        "sma_200": None,
        "ema_12": 151.2,
        "ema_26": 146.8,
        "rsi_14": 58.3,
        "macd": 1.2,
        "macd_signal": 0.9,
        "macd_hist": 0.3,
        "macd_bullish_cross": 0,
        "bb_upper": 160.0,
        "bb_middle": 148.5,
        "bb_lower": 137.0,
        "bb_pct_b": 0.55,
        "adx_14": None,
        "atr_14": None,
        "obv": None,
    }
    assert body[1]["sma_200"] == 140.0
    assert body[1]["macd_bullish_cross"] == 1
    assert body[1]["adx_14"] == 25.4
    assert fake.last_query is not None
    assert "equity_derived.technical_indicators_daily" in fake.last_query
    assert "FINAL" in fake.last_query
    assert "ORDER BY date ASC" in fake.last_query
    assert fake.last_parameters == {"ticker": "NVDA"}


def _indicator_row(d: date) -> tuple[Any, ...]:
    return (
        d,
        148.0,  # sma_20
        142.0,  # sma_50
        140.0,  # sma_200
        151.0,  # ema_12
        146.0,  # ema_26
        60.0,  # rsi_14
        1.5,  # macd
        1.0,  # macd_signal
        0.5,  # macd_hist
        0,  # macd_bullish_cross
        161.0,  # bb_upper
        149.0,  # bb_middle
        137.0,  # bb_lower
        0.5,  # bb_pct_b
        22.0,  # adx_14
        3.0,  # atr_14
        100000.0,  # obv
    )


def test_indicators_weekly_uses_derived_weekly_table(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClient(_fake_indicator_result([_indicator_row(date(2026, 1, 5))]))
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
    fake = _FakeClient(_fake_indicator_result([_indicator_row(date(2026, 1, 1))]))
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
    "ebitda_margin_pct",
    "gross_margin_bps_yoy",
    "net_margin_bps_yoy",
    "roe",
    "roa",
    "fcf_yield",
    "debt_to_equity",
    "current_ratio",
    "revenue_ttm",
    "net_income_ttm",
    "fcf_ttm",
    # Absolute-value columns from the LEFT JOIN onto equity_raw.fundamentals
    # — populated for quarterly + annual rows so the frontend can render
    # `Revenue` / `Net income` / `FCF` outside of TTM. Null on TTM rows.
    "revenue",
    "net_income",
    "free_cash_flow",
    "ebitda",
)


def test_fundamentals_returns_ratios_and_validates_ticker(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two rows: a fully-populated TTM row carrying the new bps-delta + rollup
    # fields, and a quarterly row whose denominator-division fields are null
    # to verify nulls round-trip.
    fake = _FakeClient(
        _FakeResult(
            _FUNDAMENTAL_COLS,
            [
                (
                    "NVDA",
                    date(2025, 12, 31),
                    "ttm",
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
                    40.0,  # ebitda_margin_pct
                    250.0,  # gross_margin_bps_yoy (+2.50 pp)
                    400.0,  # net_margin_bps_yoy (+4.00 pp)
                    65.0,
                    35.0,
                    0.03,
                    0.25,
                    3.5,
                    100_000_000_000.0,  # revenue_ttm
                    50_000_000_000.0,  # net_income_ttm
                    40_000_000_000.0,  # fcf_ttm
                    None,  # revenue (raw — TTM row has no raw row to join)
                    None,  # net_income
                    None,  # free_cash_flow
                    None,  # ebitda
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
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    25_000_000_000.0,  # revenue (Q3 absolute)
                    12_000_000_000.0,  # net_income
                    9_000_000_000.0,  # free_cash_flow
                    11_000_000_000.0,  # ebitda
                ),
            ],
        )
    )
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/fundamentals/nvda")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["period_type"] == "ttm"
    assert body[0]["ebitda_margin_pct"] == 40.0
    assert body[0]["gross_margin_bps_yoy"] == 250.0
    assert body[0]["net_margin_bps_yoy"] == 400.0
    assert body[0]["revenue_ttm"] == 100_000_000_000.0
    assert body[0]["fcf_ttm"] == 40_000_000_000.0
    assert body[1]["period_end"] == "2025-09-30"
    assert body[1]["pe_ratio"] is None
    assert body[1]["ebitda_margin_pct"] is None
    # Quarterly row carries absolute revenue / net_income / fcf / ebitda from
    # the LEFT JOIN onto equity_raw.fundamentals — without these the frontend
    # can't render Revenue / Net income / FCF outside of TTM.
    assert body[1]["revenue"] == 25_000_000_000.0
    assert body[1]["net_income"] == 12_000_000_000.0
    assert body[1]["free_cash_flow"] == 9_000_000_000.0
    assert body[1]["ebitda"] == 11_000_000_000.0
    assert fake.last_query is not None
    assert "equity_derived.fundamental_summary" in fake.last_query
    assert "equity_raw.fundamentals" in fake.last_query
    assert "FINAL" in fake.last_query
    assert "ORDER BY s.period_end DESC" in fake.last_query
    assert fake.last_parameters == {"ticker": "NVDA"}

    r_bad = client.get("/api/v1/fundamentals/BOGUS")
    assert r_bad.status_code == 404
    assert "Unknown ticker" in r_bad.json()["detail"]


_SUMMARY_COLS = ("ticker", "price", "prior_close", "rsi_14", "sma_50", "sparkline")


def test_dashboard_summary_categorizes_all_tickers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Three rows covering every categorization branch: overbought+bullish,
    # oversold+bearish, and a neutral/neutral row whose SMA-50 is still in the
    # 50-day warm-up window (must fall back to "neutral" trend).
    nvda_sparkline = [150.0 + i * 0.05 for i in range(60)]
    aapl_sparkline = [180.0 - i * 0.1 for i in range(60)]
    msft_sparkline = [400.0] * 60
    fake = _FakeClient(
        _FakeResult(
            _SUMMARY_COLS,
            [
                ("NVDA", 153.0, 149.49, 72.3, 140.0, nvda_sparkline),
                ("AAPL", 180.0, 185.0, 28.5, 195.0, aapl_sparkline),
                ("MSFT", 400.0, 400.0, 50.0, None, msft_sparkline),
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
        "name": "NVIDIA",
        "price": 153.0,
        "daily_change_pct": pytest.approx((153.0 - 149.49) / 149.49 * 100),
        "rsi_14": 72.3,
        "rsi_signal": "overbought",
        "trend_status": "bullish",
        "sparkline": nvda_sparkline,
    }
    assert by_ticker["AAPL"]["rsi_signal"] == "oversold"
    assert by_ticker["AAPL"]["trend_status"] == "bearish"
    assert by_ticker["AAPL"]["daily_change_pct"] == pytest.approx((180.0 - 185.0) / 185.0 * 100)
    # SMA-50 null → trend collapses to neutral regardless of price.
    assert by_ticker["MSFT"]["trend_status"] == "neutral"
    assert by_ticker["MSFT"]["rsi_signal"] == "neutral"
    assert by_ticker["MSFT"]["daily_change_pct"] == pytest.approx(0.0)
    # Sparkline of 60 closes ships per ticker (avoids the 10× per-ticker /ohlcv
    # fan-out the frontend would otherwise need on dashboard load).
    for row in body:
        assert len(row["sparkline"]) == 60
        assert all(isinstance(v, float) for v in row["sparkline"])

    # Rows are emitted in TICKERS-registry order so the frontend doesn't re-sort.
    assert [row["ticker"] for row in body] == ["NVDA", "AAPL", "MSFT"]

    # Query must cover both source tables, use FINAL (ReplacingMergeTree), and
    # pass every configured ticker to ClickHouse in one round trip.
    assert fake.last_query is not None
    assert "equity_raw.ohlcv_raw" in fake.last_query
    assert "equity_derived.technical_indicators_daily" in fake.last_query
    assert fake.last_query.count("FINAL") >= 2
    assert fake.last_parameters == {"tickers": list(TICKERS), "bars": 60}


def test_dashboard_summary_handles_missing_prior_close(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Single-day history (no prior close) → daily_change_pct must be null
    # rather than throwing or defaulting to zero. The sparkline still ships;
    # an empty array is the documented "no history" fallback.
    fake = _FakeClient(_FakeResult(_SUMMARY_COLS, [("NVDA", 153.0, None, 50.0, 140.0, [])]))
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/dashboard/summary")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["daily_change_pct"] is None
    assert body[0]["sparkline"] == []


_NEWS_COLS = (
    "id",
    "headline",
    "body",
    "publisher_name",
    "image_url",
    "url",
    "source",
    "published_at",
    "sentiment_label",
    # Per QNT-148 / ADR-016: a single canonical pill label, computed
    # server-side via the multiIf chain (resolved_host → URL host →
    # publisher_name → ''). The frontend reads `item.publisher` with no
    # further fallback. Replaces the prior `host` column.
    "publisher",
)


def test_news_returns_iso_timestamp_and_window_filter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime as dt

    fake = _FakeClient(
        _FakeResult(
            _NEWS_COLS,
            [
                (
                    "12345",
                    "NVIDIA reports record Q4 revenue",
                    "NVIDIA's Q4 revenue beat estimates...",
                    "Reuters",
                    "https://example.com/img.jpg",
                    "https://reuters.com/article",
                    "finnhub",
                    dt(2026, 4, 28, 14, 30, 0),
                    "pending",
                    "reuters.com",
                ),
                (
                    "12346",
                    "Analyst lifts NVDA price target",
                    "Goldman raised the price target to $200",
                    "Bloomberg",
                    "",  # image_url empty → frontend hides thumbnail
                    "https://bloomberg.com/article",
                    "finnhub",
                    dt(2026, 4, 27, 9, 0, 0),
                    "pending",
                    "bloomberg.com",
                ),
            ],
        )
    )
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/news/NVDA")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0] == {
        "id": "12345",
        "headline": "NVIDIA reports record Q4 revenue",
        "body": "NVIDIA's Q4 revenue beat estimates...",
        "publisher_name": "Reuters",
        "image_url": "https://example.com/img.jpg",
        "url": "https://reuters.com/article",
        "source": "finnhub",
        "published_at": "2026-04-28T14:30:00",
        "sentiment_label": "pending",
        "publisher": "reuters.com",
    }
    # Empty image_url survives — the frontend treats "" as missing.
    assert body[1]["image_url"] == ""
    assert fake.last_query is not None
    assert "equity_raw.news_raw" in fake.last_query
    assert "FINAL" in fake.last_query
    # Dedup is by article id within ticker (ADR-016 §3) — the SQL collapses
    # same-id rows that drift on `published_at` and orders by the
    # post-aggregation timestamp. Both shapes must show up in the rendered
    # query so the contract isn't accidentally regressed.
    assert "GROUP BY id" in fake.last_query
    # Outer ORDER BY uses the renamed alias to dodge the ILLEGAL_AGGREGATION
    # CH error (see test_news_query_dedups_by_article_id_with_argmax for the
    # full regression context).
    assert "ORDER BY latest_published_at DESC" in fake.last_query
    # The canonical publisher must come out of the resolved_host →
    # domain(url) → publisher_name fallback chain, not a single column.
    assert "resolved_host" in fake.last_query
    assert "publisher_name" in fake.last_query
    assert fake.last_parameters is not None
    assert fake.last_parameters["ticker"] == "NVDA"
    assert fake.last_parameters["days"] == 7  # default window matches the card header
    assert fake.last_parameters["limit"] == 25


def test_news_respects_days_and_limit_query(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClient(_FakeResult(_NEWS_COLS, []))
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/news/NVDA", params={"days": 30, "limit": 50})
    assert r.status_code == 200
    assert r.json() == []  # empty rows is a valid 200 — same as service-down per ADR-014 §5
    assert fake.last_parameters == {"ticker": "NVDA", "days": 30, "limit": 50}


def test_news_unknown_ticker_returns_404(client: TestClient) -> None:
    r = client.get("/api/v1/news/BOGUS")
    assert r.status_code == 404
    assert "Unknown ticker" in r.json()["detail"]


def test_news_rejects_spy(client: TestClient) -> None:
    # Benchmark tickers have no news pipeline — same gate as fundamentals.
    r = client.get("/api/v1/news/SPY")
    assert r.status_code == 404


def test_news_lowercase_ticker_is_normalized(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClient(_FakeResult(_NEWS_COLS, []))
    _install_fake(monkeypatch, fake)
    r = client.get("/api/v1/news/nvda")
    assert r.status_code == 200
    assert fake.last_parameters is not None
    assert fake.last_parameters["ticker"] == "NVDA"


def test_news_clamps_invalid_query_params(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClient(_FakeResult(_NEWS_COLS, []))
    _install_fake(monkeypatch, fake)
    # days=0 is below the lower bound; FastAPI must 422 rather than passing it through.
    r = client.get("/api/v1/news/NVDA", params={"days": 0})
    assert r.status_code == 422
    # limit > max should also fail validation.
    r2 = client.get("/api/v1/news/NVDA", params={"limit": 1000})
    assert r2.status_code == 422


def test_news_query_dedups_by_article_id_with_argmax(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-016 §3: same-ticker repeat rows (timestamp drift) collapse to one
    card row per article. The dedup signal is the URL hash (`id`); each
    payload field is `argMax(field, published_at)` so a re-fetch with a
    corrected headline / publisher_name actually wins over the original.

    Mocked at the SQL level — the assertion is on the rendered query
    shape, not the data round-trip — because the live behaviour depends
    on ClickHouse `argMax` semantics that the FakeClient can't simulate.
    """
    fake = _FakeClient(_FakeResult(_NEWS_COLS, []))
    _install_fake(monkeypatch, fake)

    r = client.get("/api/v1/news/NVDA")
    assert r.status_code == 200

    assert fake.last_query is not None
    q = fake.last_query
    # Dedup contract: GROUP BY id, argMax over the payload columns.
    assert "GROUP BY id" in q
    # Regression guard for the prod break that #162 shipped: ClickHouse
    # raised ILLEGAL_AGGREGATION when the aggregated timestamp was aliased
    # back to its source column name (``max(published_at) AS published_at``);
    # the alias resolution then made the inner ``WHERE published_at >= ...``
    # parse as ``WHERE max(published_at) >= ...``. Pin the rename so a
    # well-meaning future cleanup can't silently re-break /news.
    assert "max(published_at) AS latest_published_at" in q
    assert "max(published_at) AS published_at" not in q
    for col in (
        "headline",
        "body",
        "publisher_name",
        "image_url",
        "url",
        "source",
        "sentiment_label",
        "resolved_host",
    ):
        # Each payload column must come from argMax keyed on published_at.
        assert f"argMax({col}, published_at)" in q, f"missing argMax for {col}"
    # The published_at column itself uses max() — no argMax for the key.
    assert "max(published_at)" in q


_QUOTE_OHLCV_COLS = (
    "as_of",
    "today_open",
    "day_high",
    "day_low",
    "price",
    "prev_close",
    "today_volume",
    "avg_volume_30d",
    "bars_in_window",
)


class _MultiQueryFakeClient:
    """Fake client returning canned results in call order — used for the
    quote endpoint, which fans out over OHLCV + fundamentals + raw market cap.
    """

    def __init__(self, results: list[_FakeResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def query(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        self.calls.append((query, parameters))
        if not self._results:
            raise AssertionError("no more canned results")
        return self._results.pop(0)


def test_quote_returns_full_header_bundle(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # OHLCV: 30+ bars in window → avg_volume populated.
    fake = _MultiQueryFakeClient(
        [
            _FakeResult(
                _QUOTE_OHLCV_COLS,
                [
                    (
                        date(2026, 4, 28),
                        152.0,
                        158.0,
                        151.5,
                        157.0,
                        149.0,
                        22_000_000,
                        18_500_000.0,
                        30,
                    )
                ],
            ),
            _FakeResult(("pe_ratio",), [(32.5,)]),
            _FakeResult(("market_cap",), [(3_900_000_000_000.0,)]),
        ]
    )
    monkeypatch.setattr(clickhouse_module, "get_client", lambda: fake)
    monkeypatch.setattr(data_module, "get_client", lambda: fake)

    r = client.get("/api/v1/quote/NVDA")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "ticker": "NVDA",
        "name": "NVIDIA",
        "sector": "Technology",
        "industry": "Semiconductors",
        "price": 157.0,
        "prev_close": 149.0,
        "open": 152.0,
        "day_high": 158.0,
        "day_low": 151.5,
        "volume": 22_000_000,
        "avg_volume_30d": 18_500_000.0,
        "market_cap": 3_900_000_000_000.0,
        "pe_ratio_ttm": 32.5,
        "as_of": "2026-04-28",
    }
    # Three queries: OHLCV bundle → P/E TTM → raw market cap.
    assert len(fake.calls) == 3
    assert "equity_raw.ohlcv_raw" in fake.calls[0][0]
    assert "period_type = 'ttm'" in fake.calls[1][0]
    assert "equity_raw.fundamentals" in fake.calls[2][0]


def test_quote_avg_volume_is_null_when_window_short(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Brand-new ticker with only 5 bars → avg_volume_30d must be null rather
    # than a partial average that would mislead the % comparison label.
    fake = _MultiQueryFakeClient(
        [
            _FakeResult(
                _QUOTE_OHLCV_COLS,
                [
                    (
                        date(2026, 4, 28),
                        152.0,
                        158.0,
                        151.5,
                        157.0,
                        149.0,
                        22_000_000,
                        21_000_000.0,
                        5,
                    )
                ],
            ),
            _FakeResult(("pe_ratio",), []),
            _FakeResult(("market_cap",), []),
        ]
    )
    monkeypatch.setattr(clickhouse_module, "get_client", lambda: fake)
    monkeypatch.setattr(data_module, "get_client", lambda: fake)

    r = client.get("/api/v1/quote/NVDA")
    assert r.status_code == 200
    body = r.json()
    assert body["avg_volume_30d"] is None
    assert body["pe_ratio_ttm"] is None
    assert body["market_cap"] is None


def test_quote_unknown_ticker_returns_404(client: TestClient) -> None:
    r = client.get("/api/v1/quote/BOGUS")
    assert r.status_code == 404


def test_quote_rejects_spy(client: TestClient) -> None:
    r = client.get("/api/v1/quote/SPY")
    assert r.status_code == 404


def test_quote_returns_404_when_no_ohlcv_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No OHLCV history → 404 rather than a quote with all-null fields.
    fake = _MultiQueryFakeClient([_FakeResult(_QUOTE_OHLCV_COLS, [])])
    monkeypatch.setattr(clickhouse_module, "get_client", lambda: fake)
    monkeypatch.setattr(data_module, "get_client", lambda: fake)

    r = client.get("/api/v1/quote/NVDA")
    assert r.status_code == 404
    assert "No OHLCV data" in r.json()["detail"]
