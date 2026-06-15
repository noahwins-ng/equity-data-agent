"""Tests for report templates.

Templates are tested against a fake ClickHouse client so tests don't require
the live SSH tunnel and run in CI. The shape of the query result matches what
``clickhouse_connect`` returns: an object with ``column_names`` and
``result_rows`` attributes.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from api import clickhouse as clickhouse_module
from api.templates import company as company_module
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
    """Dispatches ``client.query()`` calls to canned results by table name.

    Matches by substring against ``query``; iteration order = insertion order,
    so tests can prioritise a specific match by ordering its key earlier.
    """

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


_TECH_COLS = (
    "as_of",
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


_EMPTY_PEER_RESULT = _FakeResult(("pe_ratio", "ev_ebitda", "price_to_sales"), [])
_EMPTY_TECH_RESULT = _tech_result([])


def _install_fake(monkeypatch: pytest.MonkeyPatch, canned: dict[str, _FakeResult]) -> None:
    """Swap get_client() for a fake in every place the templates imported it.

    Auto-injects empty defaults so tests can be terse:
      * ``argMax``: peer-median query (used by the fundamental report).
      * ``technical_indicators_weekly`` / ``technical_indicators_monthly``: the
        non-daily timeframes added in QNT-207. Tests that focus on DAILY can
        ignore them; QNT-207 renders them in-place as N/M when empty.
    """
    if "argMax" not in canned:
        canned = {"argMax": _EMPTY_PEER_RESULT, **canned}
    if "technical_indicators_weekly" not in canned:
        canned = {**canned, "technical_indicators_weekly": _EMPTY_TECH_RESULT}
    if "technical_indicators_monthly" not in canned:
        canned = {**canned, "technical_indicators_monthly": _EMPTY_TECH_RESULT}
    fake = lambda: _FakeClient(canned)  # noqa: E731
    monkeypatch.setattr(clickhouse_module, "get_client", fake)
    monkeypatch.setattr(technical_module, "get_client", fake)
    monkeypatch.setattr(fundamental_module, "get_client", fake)
    monkeypatch.setattr(news_module, "get_client", fake)
    monkeypatch.setattr(company_module, "get_client", fake)


# ---------- technical ----------


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
    assert "## DAILY" in report
    # Comparative RSI context — bucket label + canonical thresholds (QNT-136
    # appended the cross-bucket threshold for in-corpus quoting).
    assert "72.5 — overbought (above 70 threshold" in report
    assert "prior period 69.0, up 3.5" in report
    # MACD with explicit signal-cross wording
    assert "above signal" in report
    # Price action shows close vs SMA-50
    assert "close above SMA-50" in report
    # Period change (was "+1.54% daily" in v1; now "+1.54% vs prior period")
    assert "+1.54% vs prior period" in report
    # TREND replaces SIGNAL footer
    assert "### DAILY TREND" in report
    assert "Uptrend" in report
    assert "## SIGNAL" not in report


def test_technical_null_indicators_render_as_nm(monkeypatch: pytest.MonkeyPatch) -> None:
    # Early history: everything except close is null.
    rows = [
        (date(2026, 4, 16), None, None, None, None, None, None, None, None, None, 100.0, 500),
    ]
    _install_fake(monkeypatch, {"technical_indicators_daily": _tech_result(rows)})
    report = build_technical_report("NVDA")
    # RSI N/M still flags insufficient history; thresholds are tacked on so the
    # agent has them in-corpus even on the empty-history branch (QNT-136).
    assert "RSI-14 (daily): N/M (insufficient history" in report
    assert "MACD(12/26/9) (daily): N/M" in report
    assert "Bollinger(20,2) (daily): N/M" in report
    # TREND must not fabricate a verdict with no data
    assert "N/M (insufficient history; need SMA-20, SMA-50" in report


@pytest.mark.parametrize(
    ("rsi_value", "must_contain"),
    [
        # Every bucket must carry both 70 and 30 thresholds so the agent's
        # synthesize step can quote them verbatim instead of leaking them
        # as TA prior knowledge (QNT-136 finding: RSI 69 reports without
        # the 70 threshold caused 'unsupported: 70' hallucination flags).
        (75.0, ["75.0", "above 70 threshold", "oversold ≤ 30"]),
        (68.0, ["68.0", "approaching overbought", "70 threshold", "oversold ≤ 30"]),
        (50.0, ["50.0", "neutral", "overbought ≥ 70", "oversold ≤ 30"]),
        (32.0, ["32.0", "approaching oversold", "30 threshold", "overbought ≥ 70"]),
        (25.0, ["25.0", "below 30 threshold", "overbought ≥ 70"]),
    ],
)
def test_technical_rsi_label_always_cites_canonical_thresholds(
    monkeypatch: pytest.MonkeyPatch,
    rsi_value: float,
    must_contain: list[str],
) -> None:
    """QNT-136 regression guard: every non-N/M RSI bucket must print both the
    70 (overbought) and 30 (oversold) thresholds in the report body."""
    rows = [
        (
            date(2026, 4, 16),
            rsi_value,
            3.9,
            0.9,
            3.0,
            180.0,
            175.0,
            200.0,
            180.0,
            160.0,
            198.0,
            1000,
        ),
    ]
    _install_fake(monkeypatch, {"technical_indicators_daily": _tech_result(rows)})
    report = build_technical_report("NVDA")
    for needle in must_contain:
        assert needle in report, f"RSI={rsi_value} report missing {needle!r}"


def test_technical_header_freshness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Header carries timeframe + days-old count."""
    rows = [
        (date(2026, 4, 16), 72.5, 3.9, 0.9, 3.0, 180.0, 175.0, 200.0, 180.0, 160.0, 198.0, 1000),
    ]
    _install_fake(monkeypatch, {"technical_indicators_daily": _tech_result(rows)})
    report = build_technical_report("NVDA")
    assert "daily" in report
    assert "days old" in report


# ---------- technical AC1 (QNT-207) ----------


def _daily_row(*, as_of: date, close: float, prior_close: float | None = None) -> tuple[Any, ...]:
    return (as_of, 55.0, 1.0, 0.5, 0.5, 180.0, 175.0, 200.0, 180.0, 160.0, close, 1000)


def test_technical_renders_all_three_timeframes(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: ## DAILY, ## WEEKLY, ## MONTHLY all appear; each has PRICE ACTION /
    MOMENTUM / VOLATILITY / TREND; ## SIGNAL does not appear."""
    daily_rows = [
        _daily_row(as_of=date(2026, 4, 16), close=185.0),
        _daily_row(as_of=date(2026, 4, 15), close=184.0),
    ]
    weekly_rows = [
        (date(2026, 4, 13), 60.0, 2.0, 0.5, 0.5, 178.0, 172.0, 205.0, 182.0, 160.0, 187.0, 5000),
        (date(2026, 4, 6), 58.0, 1.8, 0.4, 0.4, 175.0, 170.0, 200.0, 180.0, 158.0, 184.0, 4900),
    ]
    monthly_rows = [
        (date(2026, 4, 1), 65.0, 5.0, 1.0, 1.0, 170.0, 160.0, 220.0, 175.0, 140.0, 190.0, 20000),
        (date(2026, 3, 1), 62.0, 4.0, 0.8, 0.8, 165.0, 155.0, 210.0, 170.0, 135.0, 180.0, 19000),
    ]
    _install_fake(
        monkeypatch,
        {
            "technical_indicators_daily": _tech_result(daily_rows),
            "technical_indicators_weekly": _tech_result(weekly_rows),
            "technical_indicators_monthly": _tech_result(monthly_rows),
        },
    )
    report = build_technical_report("NVDA")

    # AC1 — three timeframe sections
    assert "## DAILY" in report
    assert "## WEEKLY" in report
    assert "## MONTHLY" in report
    # Each has the PRICE ACTION / MOMENTUM / VOLATILITY / TREND sub-structure,
    # prefixed with the timeframe label so the LLM can't drop scope when
    # quoting a sub-section (QNT-207 follow-up).
    for tf in ("DAILY", "WEEKLY", "MONTHLY"):
        assert f"### {tf} PRICE ACTION" in report
        assert f"### {tf} MOMENTUM" in report
        assert f"### {tf} VOLATILITY" in report
        assert f"### {tf} TREND" in report
    # Per-line scope tags travel with each metric so a thesis can't strip
    # the timeframe when quoting a single value.
    assert "Close (daily):" in report
    assert "RSI-14 (weekly):" in report
    assert "Bollinger(20,2) (monthly):" in report
    # ## SIGNAL is gone
    assert "## SIGNAL" not in report
    # Disclaimer line surfaced once at the top
    assert (
        "Daily captures intraday-to-week swings; weekly captures multi-week regime; "
        "monthly captures cycle-level posture."
    ) in report
    # Every TREND block resolves to one of the three labels (this dataset is
    # bullish across all three: close > SMA-50, SMA-20 > SMA-50, positive slope).
    assert "Uptrend" in report


@pytest.mark.parametrize(
    ("close", "prior_close", "sma_20", "sma_50", "expected"),
    [
        # Uptrend: all three conditions met
        (200.0, 195.0, 190.0, 180.0, "Uptrend"),
        # Downtrend: all three reversed
        (150.0, 155.0, 160.0, 170.0, "Downtrend"),
        # Sideways: SMA-20 above SMA-50 but close below SMA-50
        (175.0, 174.0, 185.0, 180.0, "Sideways"),
        # Sideways: positive slope but close below SMA-50
        (170.0, 165.0, 175.0, 180.0, "Sideways"),
    ],
)
def test_technical_trend_label_derivation(
    monkeypatch: pytest.MonkeyPatch,
    close: float,
    prior_close: float,
    sma_20: float,
    sma_50: float,
    expected: str,
) -> None:
    """AC1: TREND label comes from close vs SMA-50, SMA-20 vs SMA-50, slope."""
    rows = [
        (date(2026, 4, 16), 55.0, 1.0, 0.5, 0.5, sma_20, sma_50, 220.0, 180.0, 140.0, close, 1000),
        (
            date(2026, 4, 15),
            54.0,
            0.9,
            0.4,
            0.4,
            sma_20 - 1,
            sma_50 - 1,
            218.0,
            178.0,
            138.0,
            prior_close,
            900,
        ),
    ]
    _install_fake(monkeypatch, {"technical_indicators_daily": _tech_result(rows)})
    report = build_technical_report("NVDA")
    assert expected in report


def test_technical_weekly_empty_renders_nm_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """A trailing weekly/monthly slice with no data renders as N/M inline,
    not as a 404 on the whole report — daily is the gate."""
    daily_rows = [
        _daily_row(as_of=date(2026, 4, 16), close=185.0),
        _daily_row(as_of=date(2026, 4, 15), close=184.0),
    ]
    _install_fake(
        monkeypatch,
        {"technical_indicators_daily": _tech_result(daily_rows)},
    )
    report = build_technical_report("NVDA")
    assert "## DAILY" in report
    assert "## WEEKLY" in report
    assert "## MONTHLY" in report
    assert "no weekly indicator data" in report
    assert "no monthly indicator data" in report


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
    # QNT-87 end-to-end: pe_ratio=None + eps=0.01 → "N/M (near-zero earnings…".
    rows = [_fund_row(pe_ratio=None, eps=0.01)]
    _install_fake(monkeypatch, {"fundamental_summary": _FakeResult(_FUND_COLS, rows)})
    report = build_fundamental_report("INTC")
    assert "P/E (quarterly): N/M (near-zero earnings" in report


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
    assert "Revenue (quarterly): +25.00% YoY" in report
    assert "prior period +18.00%, accelerating" in report


def test_fundamental_unknown_ticker_404() -> None:
    with pytest.raises(HTTPException) as exc:
        build_fundamental_report("BOGUS")
    assert exc.value.status_code == 404


@pytest.mark.parametrize(
    ("pe", "eps", "must_contain"),
    [
        # Every P/E line — including the N/M branches — must carry the
        # canonical rich/cheap thresholds (QNT-137 / ADR-012).
        (15.0, 2.5, ["P/E (quarterly): 15.00", "rich ≥ 40", "cheap ≤ 20"]),
        (45.0, 2.5, ["P/E (quarterly): 45.00", "rich ≥ 40", "cheap ≤ 20"]),
        (
            None,
            0.01,
            ["P/E (quarterly): N/M", "near-zero earnings", "rich ≥ 40", "cheap ≤ 20"],
        ),
        (
            None,
            None,
            ["P/E (quarterly): N/M", "EPS unavailable", "rich ≥ 40", "cheap ≤ 20"],
        ),
    ],
)
def test_fundamental_pe_label_always_cites_canonical_thresholds(
    monkeypatch: pytest.MonkeyPatch,
    pe: float | None,
    eps: float | None,
    must_contain: list[str],
) -> None:
    rows = [_fund_row(pe_ratio=pe, eps=eps)]
    _install_fake(monkeypatch, {"fundamental_summary": _FakeResult(_FUND_COLS, rows)})
    report = build_fundamental_report("NVDA")
    for needle in must_contain:
        assert needle in report, f"P/E={pe} eps={eps} report missing {needle!r}"


@pytest.mark.parametrize(
    ("section_header", "must_contain"),
    [
        (
            "### QUARTERLY GROWTH (YoY)",
            ["Reference rates: ≥ 10% strong, ≤ 0% contraction"],
        ),
        (
            "### QUARTERLY PROFITABILITY",
            [
                "Reference rates",
                "net margin ≥ 15% strong",
                "≤ 0 loss-making",
                "ROE ≥ 15% strong",
                "≤ 0 negative",
            ],
        ),
    ],
)
def test_fundamental_sections_cite_canonical_reference_rates(
    monkeypatch: pytest.MonkeyPatch,
    section_header: str,
    must_contain: list[str],
) -> None:
    rows = [_fund_row()]
    _install_fake(monkeypatch, {"fundamental_summary": _FakeResult(_FUND_COLS, rows)})
    report = build_fundamental_report("NVDA")
    assert section_header in report
    for needle in must_contain:
        assert needle in report, f"{section_header} missing {needle!r}"


def test_fundamental_own_history_range_position_renders_with_sufficient_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With 5+ quarterly rows, each valuation multiple shows range + position label."""
    rows = [
        _fund_row(
            period_end=date(2026, 3, 31),
            pe_ratio=32.0,
            ev_ebitda=25.0,
            price_to_book=8.0,
            price_to_sales=10.0,
        ),
        _fund_row(
            period_end=date(2025, 12, 31),
            pe_ratio=28.0,
            ev_ebitda=22.0,
            price_to_book=7.0,
            price_to_sales=9.0,
        ),
        _fund_row(
            period_end=date(2025, 9, 30),
            pe_ratio=30.0,
            ev_ebitda=24.0,
            price_to_book=7.5,
            price_to_sales=9.5,
        ),
        _fund_row(
            period_end=date(2025, 6, 30),
            pe_ratio=25.0,
            ev_ebitda=20.0,
            price_to_book=6.5,
            price_to_sales=8.5,
        ),
        _fund_row(
            period_end=date(2025, 3, 31),
            pe_ratio=24.0,
            ev_ebitda=19.0,
            price_to_book=6.0,
            price_to_sales=8.0,
        ),
    ]
    _install_fake(monkeypatch, {"fundamental_summary": _FakeResult(_FUND_COLS, rows)})
    report = build_fundamental_report("NVDA")
    assert "range" in report
    assert "over last 5y" in report
    # current P/E=32.0 is highest in history → near 5y high
    assert "near 5y high" in report


def test_fundamental_peer_context_rendered_for_nvda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NVDA (Technology, 4 peers) renders a sector median in PEER CONTEXT."""
    peer_rows = [
        (28.0, 20.0, 8.0),
        (30.0, 22.0, 9.0),
        (25.0, 18.0, 7.0),
        (27.0, 21.0, 8.5),
    ]
    _install_fake(
        monkeypatch,
        {
            "argMax": _FakeResult(("pe_ratio", "ev_ebitda", "price_to_sales"), peer_rows),
            "fundamental_summary": _FakeResult(_FUND_COLS, [_fund_row()]),
        },
    )
    report = build_fundamental_report("NVDA")
    assert "## PEER CONTEXT" in report
    assert "Technology" in report
    assert "Sector median P/E" in report
    # median of [28, 30, 25, 27] = (27+28)/2 = 27.50
    assert "27.50" in report


def test_fundamental_peer_context_na_for_amzn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AMZN (Consumer Discretionary, 1 sector peer < _MIN_PEERS_FOR_MEDIAN)
    renders N/A in PEER CONTEXT."""
    _install_fake(monkeypatch, {"fundamental_summary": _FakeResult(_FUND_COLS, [_fund_row()])})
    report = build_fundamental_report("AMZN")
    assert "## PEER CONTEXT" in report
    assert "N/A (insufficient peers in coverage" in report
    assert "Consumer Discretionary" in report


def test_fundamental_header_freshness(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake(monkeypatch, {"fundamental_summary": _FakeResult(_FUND_COLS, [_fund_row()])})
    report = build_fundamental_report("NVDA")
    assert "quarterly" in report
    assert "days old" in report


def test_fundamental_static_data_disclaimer_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake(monkeypatch, {"fundamental_summary": _FakeResult(_FUND_COLS, [_fund_row()])})
    report = build_fundamental_report("NVDA")
    assert "Data: latest available quarterly fundamentals as of" in report
    assert "2025-12-31" in report


# ---------- fundamental AC2 (QNT-207) ----------


def test_fundamental_renders_quarterly_annual_ttm_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: ## QUARTERLY, ## ANNUAL, ## TTM sections all appear when data exists."""
    rows = [
        _fund_row(period_type="quarterly", period_end=date(2025, 12, 31)),
        _fund_row(period_type="quarterly", period_end=date(2025, 9, 30), pe_ratio=22.0),
        _fund_row(period_type="annual", period_end=date(2025, 12, 31), pe_ratio=28.0),
        _fund_row(period_type="annual", period_end=date(2024, 12, 31), pe_ratio=24.0),
        _fund_row(period_type="ttm", period_end=date(2025, 12, 31), pe_ratio=26.0),
        _fund_row(period_type="ttm", period_end=date(2025, 9, 30), pe_ratio=23.0),
    ]
    _install_fake(monkeypatch, {"fundamental_summary": _FakeResult(_FUND_COLS, rows)})
    report = build_fundamental_report("NVDA")
    assert "## QUARTERLY" in report
    assert "## ANNUAL" in report
    assert "## TTM" in report
    # Period disclaimer + per-multiple label rule disclaimer
    assert "Quarterly captures execution trajectory" in report
    assert "Per-multiple label" in report
    # Sub-sections carry their period prefix so the LLM can't drop the scope
    # when quoting a sub-section name (QNT-207 follow-up).
    for period in ("QUARTERLY", "ANNUAL", "TTM"):
        assert f"### {period} VALUATION" in report
        assert f"### {period} GROWTH (YoY)" in report
        assert f"### {period} PROFITABILITY" in report
        assert f"### {period} CASH & LEVERAGE" in report
    # Per-line scope tags travel with each multiple so a thesis can't strip
    # the period when quoting a single number.
    assert "P/E (quarterly):" in report
    assert "P/E (annual):" in report
    assert "P/E (ttm):" in report


def test_fundamental_renders_premium_inline_discounted_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: at least one Premium/Inline/Discounted suffix renders on a multiple line."""
    # Need >=4 history points for own-history IQR derivation; the latest P/E
    # (50) is above own-history 75th pct → Premium.
    rows = [
        _fund_row(period_end=date(2026, 3, 31), pe_ratio=50.0),
        _fund_row(period_end=date(2025, 12, 31), pe_ratio=20.0),
        _fund_row(period_end=date(2025, 9, 30), pe_ratio=22.0),
        _fund_row(period_end=date(2025, 6, 30), pe_ratio=24.0),
        _fund_row(period_end=date(2025, 3, 31), pe_ratio=23.0),
    ]
    _install_fake(monkeypatch, {"fundamental_summary": _FakeResult(_FUND_COLS, rows)})
    report = build_fundamental_report("AMD")
    assert "— Premium" in report


def test_fundamental_no_value_trap_label_in_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: value-trap / growth-at-a-price asymmetry labels never render in the body.

    Even with a row pattern that would historically have triggered the named
    asymmetry label (cheap P/E + contracting revenue), the rendered body must
    not contain the strings. The vote logic itself stays in the codebase for
    downstream consumers (tested separately below).
    """
    rows = [_fund_row(pe_ratio=15.0, revenue_yoy_pct=-5.0, net_margin_pct=None, roe=None)]
    _install_fake(monkeypatch, {"fundamental_summary": _FakeResult(_FUND_COLS, rows)})
    report = build_fundamental_report("NVDA")
    assert "value-trap" not in report
    assert "growth-at-a-price" not in report
    # ## SIGNAL section itself is also gone from the body now
    assert "## SIGNAL" not in report


# ---------- signal verdict v2 (QNT-206 vote logic kept as a Python callable) ----------


from api.templates.fundamental import _signal_verdict  # noqa: E402


@pytest.mark.parametrize(
    ("pe", "rev_yoy", "margin", "roe", "expected_prefix"),
    [
        (15.0, 15.0, 20.0, 18.0, "BULLISH (6/6 weighted indicators agree)"),
        (45.0, -5.0, -5.0, -3.0, "BEARISH (6/6 weighted indicators agree)"),
        (15.0, 15.0, 10.0, 10.0, "BULLISH (4/6 weighted indicators agree)"),
    ],
)
def test_signal_verdict_v2_weighted_verdict_text(
    pe: float,
    rev_yoy: float,
    margin: float,
    roe: float,
    expected_prefix: str,
) -> None:
    """Vote logic kept as a callable -- thesis v2 (QNT-208) may consume it
    even though QNT-207 dropped the rendered output."""
    row = {"pe_ratio": pe, "revenue_yoy_pct": rev_yoy, "net_margin_pct": margin, "roe": roe}
    assert _signal_verdict(row) == expected_prefix


def test_signal_verdict_v2_value_trap_label() -> None:
    row = {"pe_ratio": 15.0, "revenue_yoy_pct": -5.0, "net_margin_pct": None, "roe": None}
    assert _signal_verdict(row) == "MIXED (value-trap risk)"


def test_signal_verdict_v2_growth_at_a_price_label() -> None:
    latest = {"pe_ratio": 45.0, "revenue_yoy_pct": 25.0, "net_margin_pct": None, "roe": None}
    assert _signal_verdict(latest) == "MIXED (growth-at-a-price)"


def test_signal_verdict_v2_growth_at_a_price_tsla_pattern() -> None:
    tsla_like = {"pe_ratio": 406.0, "revenue_yoy_pct": 15.8, "net_margin_pct": 2.1, "roe": 0.6}
    assert _signal_verdict(tsla_like) == "MIXED (growth-at-a-price)"


def test_signal_verdict_v2_profitability_breaks_tie_to_bullish() -> None:
    row = {"pe_ratio": 45.0, "revenue_yoy_pct": 15.0, "net_margin_pct": 20.0, "roe": 18.0}
    result = _signal_verdict(row)
    assert result.startswith("BULLISH")
    assert "weighted" in result


# ---------- news ----------


_NEWS_COLS = ("published_at", "source", "headline", "body_snippet")


def _news_row(
    *,
    published: datetime,
    source: str = "finnhub",
    headline: str = "headline",
    body_snippet: str = "",
) -> tuple[Any, ...]:
    return (published, source, headline, body_snippet)


def test_news_unknown_ticker_404() -> None:
    with pytest.raises(HTTPException) as exc:
        build_news_report("BOGUS")
    assert exc.value.status_code == 404


def test_news_empty_returns_nm_block(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake(
        monkeypatch,
        {"news_raw": _FakeResult(_NEWS_COLS, [])},
    )
    report = build_news_report("NVDA")
    assert "# NEWS REPORT — NVDA" in report
    assert "## RECENT HEADLINES" in report
    assert "N/M (no news ingested for NVDA" in report
    # SIGNAL section is gone post-QNT-207
    assert "## SIGNAL" not in report
    assert "## SOURCES" in report


def test_news_renders_headlines_without_sentiment(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: per-headline rendering omits the Sentiment: line entirely."""
    rows = [
        _news_row(
            published=datetime(2026, 4, 16, tzinfo=UTC),
            source="finnhub",
            headline="NVDA hits new high",
            body_snippet="Earnings blew past estimates as data centre demand surged",
        ),
        _news_row(
            published=datetime(2026, 4, 15, tzinfo=UTC),
            source="reuters",
            headline="Analyst flags margin risk",
            body_snippet="Bears pointed to compressing margins",
        ),
    ]
    _install_fake(monkeypatch, {"news_raw": _FakeResult(_NEWS_COLS, rows)})
    report = build_news_report("NVDA")
    assert "## RECENT HEADLINES" in report
    assert "Earnings blew past estimates" in report
    # Sentiment is removed
    assert "Sentiment:" not in report
    assert "## SIGNAL" not in report
    # Sources roll-up rendered
    assert "## SOURCES" in report
    assert "finnhub: 1" in report
    assert "reuters: 1" in report


def test_news_lookback_and_cap_constants() -> None:
    """AC3: lookback widened 7 -> 14, cap widened 10 -> 20 (QNT-207)."""
    from api.templates import news as news_module_local

    assert news_module_local._LOOKBACK_DAYS == 14
    assert news_module_local._MAX_HEADLINES == 20


def test_news_header_advertises_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake(monkeypatch, {"news_raw": _FakeResult(_NEWS_COLS, [])})
    report = build_news_report("NVDA")
    assert "Lookback: last 14 days, up to 20 headlines" in report


# ---------- company ----------


def _company_install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pe: float | None = 25.0,
    rev_yoy: float | None = 12.0,
    daily_rows: list[tuple[Any, ...]] | None = None,
) -> None:
    """Set up fakes so the company CONTEXT NOW block can render without a tunnel."""
    fund_rows = (
        [_fund_row(pe_ratio=pe, revenue_yoy_pct=rev_yoy)]
        if pe is not None or rev_yoy is not None
        else []
    )
    if daily_rows is None:
        daily_rows = [
            _daily_row(as_of=date(2026, 4, 16), close=185.0),
            _daily_row(as_of=date(2026, 4, 15), close=183.0),
        ]
    _install_fake(
        monkeypatch,
        {
            "fundamental_summary": _FakeResult(_FUND_COLS, fund_rows),
            "technical_indicators_daily": _tech_result(daily_rows),
        },
    )


def test_company_report_renders_static_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Company endpoint returns a rich static profile + live CONTEXT NOW (QNT-207)."""
    from api.templates.company import build_company_report

    _company_install(monkeypatch)
    report = build_company_report("NVDA")
    assert "# COMPANY REPORT — NVDA" in report
    assert "## BUSINESS" in report
    assert "## KEY COMPETITORS" in report
    assert "## KEY RISKS" in report
    assert "## WATCH" in report
    # Spot-check that real editorial content rendered, not just headers.
    assert "AMD" in report  # NVDA competitor
    assert "Data Center revenue growth" in report  # NVDA watch metric


def test_company_report_unknown_ticker_404() -> None:
    from api.templates.company import build_company_report

    with pytest.raises(HTTPException) as exc:
        build_company_report("BOGUS")
    assert exc.value.status_code == 404


def test_company_report_covers_all_tickers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every covered ticker has a static profile — none falls back to
    '(none recorded)' bullets."""
    from api.templates.company import build_company_report
    from shared.tickers import TICKERS

    _company_install(monkeypatch)
    for ticker in TICKERS:
        report = build_company_report(ticker)
        assert f"# COMPANY REPORT — {ticker}" in report
        assert "(none recorded)" not in report, (
            f"{ticker} is missing competitors/risks/watch in TICKER_METADATA"
        )


# ---------- company AC4 (QNT-207) ----------


def test_company_context_now_renders_with_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4: ## CONTEXT NOW block appears with at least one cited number."""
    from api.templates.company import build_company_report

    _company_install(monkeypatch, pe=25.0, rev_yoy=12.0)
    report = build_company_report("NVDA")
    assert "## CONTEXT NOW" in report
    # P/E cited verbatim
    assert "Latest P/E: 25.00" in report
    # Revenue YoY cited verbatim
    assert "Latest revenue YoY: +12.00%" in report
    # Trend label derived from daily data
    assert "Daily trend:" in report


def test_company_context_now_handles_missing_fundamentals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4: CONTEXT NOW gracefully degrades to N/A when fundamentals are missing."""
    from api.templates.company import build_company_report

    _install_fake(
        monkeypatch,
        {
            "fundamental_summary": _FakeResult(_FUND_COLS, []),
            "technical_indicators_daily": _tech_result(
                [
                    _daily_row(as_of=date(2026, 4, 16), close=185.0),
                    _daily_row(as_of=date(2026, 4, 15), close=183.0),
                ]
            ),
        },
    )
    report = build_company_report("NVDA")
    assert "## CONTEXT NOW" in report
    assert "Latest P/E: N/A" in report
    # Trend should still be cited (daily rows present) — a cited "trend label"
    # satisfies AC4 by itself.
    assert "Daily trend:" in report


# ---------- company compact profile (QNT-220 #8) ----------


def test_company_report_compact_keeps_numbers_drops_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compact profile keeps CONTEXT NOW numbers + business + risks verbatim but
    drops the competitor / watch lists, and is strictly smaller than full."""
    from api.templates.company import build_company_report

    _company_install(monkeypatch, pe=25.0, rev_yoy=12.0)
    full = build_company_report("NVDA", "full")
    compact = build_company_report("NVDA", "compact")

    # Numeric grounding block preserved verbatim (hallucination-scorer safe).
    assert "## CONTEXT NOW" in compact
    assert "Latest P/E: 25.00" in compact
    assert "Latest revenue YoY: +12.00%" in compact
    # Qualitative grounding kept.
    assert "## BUSINESS" in compact
    assert "## KEY RISKS" in compact
    # Trimmed sections.
    assert "## KEY COMPETITORS" not in compact
    assert "## WATCH" not in compact
    # Full report is unchanged and larger.
    assert "## KEY COMPETITORS" in full
    assert "## WATCH" in full
    assert len(compact) < len(full)


def test_company_report_default_profile_is_full(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.templates.company import build_company_report

    _company_install(monkeypatch)
    assert build_company_report("NVDA") == build_company_report("NVDA", "full")


def test_company_report_unknown_profile_400(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.templates.company import build_company_report

    _company_install(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        build_company_report("NVDA", "tiny")
    assert exc.value.status_code == 400


# ---------- summary ----------


def test_summary_composes_all_three_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    tech_rows = [
        _daily_row(as_of=date(2026, 4, 16), close=185.0),
        _daily_row(as_of=date(2026, 4, 15), close=183.0),
    ]
    _install_fake(
        monkeypatch,
        {
            "technical_indicators_daily": _tech_result(tech_rows),
            "fundamental_summary": _FakeResult(_FUND_COLS, [_fund_row()]),
            "news_raw": _FakeResult(_NEWS_COLS, []),
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
            "news_raw": _FakeResult(_NEWS_COLS, []),
        },
    )
    report = build_summary_report("NVDA")
    assert "N/M (No technical data for NVDA)" in report
    assert "# FUNDAMENTAL REPORT — NVDA" in report
