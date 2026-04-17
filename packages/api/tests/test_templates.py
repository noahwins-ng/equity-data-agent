"""Tests for report templates.

Templates are tested against a fake ClickHouse client so tests don't require
the live SSH tunnel and run in CI. The shape of the query result matches what
``clickhouse_connect`` returns: an object with ``column_names`` and
``result_rows`` attributes.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

import pytest
from api import clickhouse as clickhouse_module
from api.templates import fundamental as fundamental_module
from api.templates import news as news_module
from api.templates import technical as technical_module
from api.templates.fundamental import build_fundamental_report
from api.templates.news import build_news_report
from api.templates.summary import build_summary_report
from api.templates.technical import build_technical_report
from fastapi import HTTPException


class _FakeResult:
    def __init__(self, column_names: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
        self.column_names = column_names
        self.result_rows = rows


class _FakeClient:
    """Dispatches ``client.query()`` calls to canned results by table name."""

    def __init__(self, canned: dict[str, _FakeResult]) -> None:
        self._canned = canned

    def query(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        for table, result in self._canned.items():
            if table in query:
                return result
        raise AssertionError(f"Unexpected query: {query!r}")


@pytest.fixture(autouse=True)
def _reset_client_cache() -> Iterable[None]:
    clickhouse_module.get_client.cache_clear()
    yield
    clickhouse_module.get_client.cache_clear()


def _install_fake(monkeypatch: pytest.MonkeyPatch, canned: dict[str, _FakeResult]) -> None:
    """Swap get_client() for a fake in every place the templates imported it.

    Each template does ``from api.clickhouse import get_client`` so the name is
    bound at module load — patching ``api.clickhouse.get_client`` alone isn't
    enough; the templates' already-resolved reference must also be replaced.
    """
    fake = lambda: _FakeClient(canned)  # noqa: E731
    monkeypatch.setattr(clickhouse_module, "get_client", fake)
    monkeypatch.setattr(technical_module, "get_client", fake)
    monkeypatch.setattr(fundamental_module, "get_client", fake)
    monkeypatch.setattr(news_module, "get_client", fake)


# ---------- technical ----------


_TECH_COLS = (
    "date",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "sma_20",
    "sma_50",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "close",
    "volume",
)


def _tech_result(rows: list[tuple[Any, ...]]) -> _FakeResult:
    return _FakeResult(_TECH_COLS, rows)


def test_technical_unknown_ticker_404() -> None:
    with pytest.raises(HTTPException) as exc:
        build_technical_report("BOGUS")
    assert exc.value.status_code == 404


def test_technical_empty_rows_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake(monkeypatch, {"technical_indicators_daily": _tech_result([])})
    with pytest.raises(HTTPException) as exc:
        build_technical_report("NVDA")
    assert exc.value.status_code == 404


def test_technical_bullish_overbought_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        (date(2026, 4, 16), 72.5, 3.9, 0.9, 3.0, 180.0, 175.0, 200.0, 180.0, 160.0, 198.0, 1000),
        (date(2026, 4, 15), 69.0, 3.1, 0.2, 2.9, 179.0, 174.5, 196.0, 179.0, 162.0, 195.0, 900),
    ]
    _install_fake(monkeypatch, {"technical_indicators_daily": _tech_result(rows)})
    report = build_technical_report("NVDA")

    assert "# TECHNICAL REPORT — NVDA" in report
    assert "As of 2026-04-16" in report
    # Comparative RSI context
    assert "72.5 — overbought (above 70 threshold)" in report
    assert "prior session 69.0, up 3.5" in report
    # MACD with explicit signal-cross wording
    assert "above signal" in report
    # Trend vs SMA-50
    assert "close above SMA-50" in report
    # Daily change was +1.54%
    assert "+1.54% daily" in report
    # Explicit signal verdict
    assert "## SIGNAL" in report
    assert "BULLISH" in report


def test_technical_null_indicators_render_as_nm(monkeypatch: pytest.MonkeyPatch) -> None:
    # Early history: everything except close is null.
    rows = [
        (date(2026, 4, 16), None, None, None, None, None, None, None, None, None, 100.0, 500),
    ]
    _install_fake(monkeypatch, {"technical_indicators_daily": _tech_result(rows)})
    report = build_technical_report("NVDA")
    assert "RSI-14: N/M (insufficient history)" in report
    assert "MACD(12/26/9): N/M" in report
    assert "Bollinger(20,2): N/M" in report
    # Signal must not fabricate a verdict with no data
    assert "N/M (insufficient history across all indicators)" in report


# ---------- fundamental ----------


_FUND_COLS = (
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


def _fund_row(**overrides: Any) -> tuple[Any, ...]:
    base = {
        "period_end": date(2025, 12, 31),
        "period_type": "quarterly",
        "pe_ratio": 25.0,
        "ev_ebitda": 18.0,
        "price_to_book": 5.0,
        "price_to_sales": 8.0,
        "eps": 2.5,
        "revenue_yoy_pct": 12.0,
        "net_income_yoy_pct": 20.0,
        "fcf_yoy_pct": 15.0,
        "net_margin_pct": 25.0,
        "gross_margin_pct": 60.0,
        "roe": 22.0,
        "roa": 12.0,
        "fcf_yield": 3.5,
        "debt_to_equity": 0.5,
        "current_ratio": 1.8,
    }
    base.update(overrides)
    return tuple(base[c] for c in _FUND_COLS)


def test_fundamental_pe_nulled_with_low_eps_renders_near_zero_earnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # QNT-87 end-to-end: pe_ratio=None + eps=0.01 → "N/M (near-zero earnings)".
    rows = [_fund_row(pe_ratio=None, eps=0.01)]
    _install_fake(monkeypatch, {"fundamental_summary": _FakeResult(_FUND_COLS, rows)})
    report = build_fundamental_report("UNH")
    assert "P/E: N/M (near-zero earnings)" in report


def test_fundamental_prior_period_trend_label(monkeypatch: pytest.MonkeyPatch) -> None:
    latest = _fund_row(revenue_yoy_pct=25.0)
    prior = _fund_row(
        period_end=date(2025, 9, 30),
        revenue_yoy_pct=18.0,
    )
    _install_fake(
        monkeypatch,
        {"fundamental_summary": _FakeResult(_FUND_COLS, [latest, prior])},
    )
    report = build_fundamental_report("NVDA")
    # +25% vs +18% prior → accelerating
    assert "Revenue: +25.00% YoY" in report
    assert "prior period +18.00%, accelerating" in report


def test_fundamental_unknown_ticker_404() -> None:
    with pytest.raises(HTTPException) as exc:
        build_fundamental_report("BOGUS")
    assert exc.value.status_code == 404


# ---------- news ----------


def test_news_empty_returns_nm_block(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake(
        monkeypatch,
        {"news_raw": _FakeResult(("published_at", "source", "headline"), [])},
    )
    report = build_news_report("NVDA")
    assert "# NEWS REPORT — NVDA" in report
    assert "## HEADLINES" in report
    assert "N/M (no news ingested for NVDA" in report
    assert "## SIGNAL" in report


def test_news_unknown_ticker_404() -> None:
    with pytest.raises(HTTPException) as exc:
        build_news_report("BOGUS")
    assert exc.value.status_code == 404


# ---------- summary ----------


def test_summary_composes_all_three_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    tech_rows = [
        (date(2026, 4, 16), 55.0, 1.0, 0.5, 0.5, 180.0, 175.0, 200.0, 180.0, 160.0, 185.0, 1000),
    ]
    _install_fake(
        monkeypatch,
        {
            "technical_indicators_daily": _tech_result(tech_rows),
            "fundamental_summary": _FakeResult(_FUND_COLS, [_fund_row()]),
            "news_raw": _FakeResult(("published_at", "source", "headline"), []),
        },
    )
    report = build_summary_report("NVDA")
    assert "# SUMMARY REPORT — NVDA" in report
    assert "# TECHNICAL REPORT — NVDA" in report
    assert "# FUNDAMENTAL REPORT — NVDA" in report
    assert "# NEWS REPORT — NVDA" in report


def test_summary_demotes_subreport_404_to_inline_nm(monkeypatch: pytest.MonkeyPatch) -> None:
    # Technical returns empty (404 internally); summary must stay 200 with N/M.
    _install_fake(
        monkeypatch,
        {
            "technical_indicators_daily": _tech_result([]),
            "fundamental_summary": _FakeResult(_FUND_COLS, [_fund_row()]),
            "news_raw": _FakeResult(("published_at", "source", "headline"), []),
        },
    )
    report = build_summary_report("NVDA")
    assert "N/M (No technical data for NVDA)" in report
    assert "# FUNDAMENTAL REPORT — NVDA" in report
