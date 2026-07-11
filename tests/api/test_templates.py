"""Tests for report templates.

Templates are tested against a fake ClickHouse client so tests don't require
the live SSH tunnel and run in CI. The shape of the query result matches what
``clickhouse_connect`` returns: an object with ``column_names`` and
``result_rows`` attributes.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
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


# Column order mirrors the _fetch_rows SELECT: as_of, *_INDICATOR_COLUMNS,
# close, volume. QNT-353 widened _INDICATOR_COLUMNS with sma_200, bb_pct_b,
# adx_14, atr_14, macd_bullish_cross, so the tuple grows here too.
_TECH_COLS = (
    "as_of",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "sma_20",
    "sma_50",
    "sma_200",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "bb_pct_b",
    "adx_14",
    "atr_14",
    "macd_bullish_cross",
    "close",
    "volume",
)


def _tech_result(rows: list[tuple[Any, ...]]) -> _FakeResult:
    return _FakeResult(_TECH_COLS, rows)


def _tech_row(**overrides: Any) -> tuple[Any, ...]:
    """Build a technical result row from named columns (QNT-353).

    The widened column set makes positional tuples error-prone; tests name only
    the columns they care about and inherit sensible defaults for the rest.
    """
    base = {
        "as_of": date(2026, 4, 16),
        "rsi_14": 55.0,
        "macd": 1.0,
        "macd_signal": 0.5,
        "macd_hist": 0.5,
        "sma_20": 180.0,
        "sma_50": 175.0,
        "sma_200": 170.0,
        "bb_upper": 200.0,
        "bb_middle": 180.0,
        "bb_lower": 160.0,
        "bb_pct_b": 0.6,
        "adx_14": 27.0,
        "atr_14": 4.5,
        "macd_bullish_cross": 0,
        "close": 185.0,
        "volume": 1000,
    }
    base.update(overrides)
    return tuple(base[c] for c in _TECH_COLS)


# QNT-353 daily price-context query (52-week range + window-return anchors +
# 20-day avg volume). Keyed on the unique ``high_52w`` column in _install_fake.
_PRICE_CTX_COLS = (
    "high_52w",
    "low_52w",
    "adj_now",
    "adj_1m",
    "adj_3m",
    "adj_1y",
    "adj_ytd",
    "avg_volume_20",
)


def _price_ctx(**overrides: Any) -> _FakeResult:
    base = {c: None for c in _PRICE_CTX_COLS}
    base.update(overrides)
    return _FakeResult(_PRICE_CTX_COLS, [tuple(base[c] for c in _PRICE_CTX_COLS)])


_EMPTY_PRICE_CTX = _price_ctx()
_EMPTY_PEER_RESULT = _FakeResult(("pe_ratio", "ev_ebitda", "price_to_sales"), [])
_EMPTY_TECH_RESULT = _tech_result([])
# QNT-357 follow-up: the fundamental report now fetches the next earnings date;
# default to a far-future date so terse tests render a line and never trip the
# no-try/except fundamental query on an unmatched fake.
_EARNINGS_CAL_RESULT = _FakeResult(("next_earnings_date",), [(date(2099, 8, 15),)])


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
    if "equity_raw.fundamentals" not in canned:
        canned = {**canned, "equity_raw.fundamentals": _MARKET_CAP_RESULT}
    if "earnings_calendar" not in canned:
        canned = {**canned, "earnings_calendar": _EARNINGS_CAL_RESULT}
    if "technical_indicators_weekly" not in canned:
        canned = {**canned, "technical_indicators_weekly": _EMPTY_TECH_RESULT}
    if "technical_indicators_monthly" not in canned:
        canned = {**canned, "technical_indicators_monthly": _EMPTY_TECH_RESULT}
    if "high_52w" not in canned:
        canned = {**canned, "high_52w": _EMPTY_PRICE_CTX}
    # The price-context query calls argMax(); prepend its unique high_52w key so
    # the fake matches it before the fundamental peer-median "argMax" key.
    canned = {"high_52w": canned["high_52w"], **canned}
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
        _tech_row(
            as_of=date(2026, 4, 16),
            rsi_14=72.5,
            macd=3.9,
            macd_signal=0.9,
            macd_hist=3.0,
            sma_20=180.0,
            sma_50=175.0,
            sma_200=170.0,
            bb_upper=200.0,
            bb_middle=180.0,
            bb_lower=160.0,
            bb_pct_b=0.83,
            adx_14=31.0,
            atr_14=4.5,
            macd_bullish_cross=1,
            close=198.0,
            volume=1000,
        ),
        _tech_row(
            as_of=date(2026, 4, 15),
            rsi_14=69.0,
            macd=3.1,
            macd_signal=0.2,
            macd_hist=2.9,
            sma_20=179.0,
            sma_50=174.5,
            bb_upper=196.0,
            bb_middle=179.0,
            bb_lower=162.0,
            close=195.0,
            volume=900,
        ),
    ]
    _install_fake(
        monkeypatch,
        {
            "technical_indicators_daily": _tech_result(rows),
            "high_52w": _price_ctx(
                high_52w=220.0,
                low_52w=150.0,
                adj_now=198.0,
                adj_1m=188.0,
                adj_3m=180.0,
                adj_1y=165.0,
                adj_ytd=190.0,
                avg_volume_20=800.0,
            ),
        },
    )
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
    # Period change (was "+1.54% daily" in v1; 1dp since QNT-361)
    assert "+1.5% vs prior period" in report
    # TREND replaces SIGNAL footer
    assert "### DAILY TREND" in report
    assert "Uptrend" in report
    assert "## SIGNAL" not in report
    # QNT-353 AC1 — widened indicator lines with canonical thresholds in-body.
    # Terser daily forms (comparison token budget): %B folds onto the Bollinger
    # line, ADX/ATR/SMA-200/MACD-cross compact.
    assert "SMA-200 (daily): close above SMA-200 (170.00) +16.5%; 50/200 golden cross" in report
    assert "ADX-14 (daily): 31.0 — trending (≥ 25 trending, < 20 weak)" in report
    assert "ATR-14 (daily): 4.50 (2.3% of close)" in report
    assert "; %B 0.83 (0 = lower band, 1 = upper band)" in report
    assert "MACD bullish cross (daily): yes — crossed above signal on the latest bar" in report
    # QNT-353 AC2 — 52-week range + PERFORMANCE + volume, all pre-computed.
    assert (
        "52-week (daily): 52-week range 150.00 - 220.00; close -10.0% from the 52-week high"
        in report
    )
    assert "Performance (daily): 1m +5.3%; 3m +10.0%; YTD +4.2%; 1y +20.0%" in report
    assert "Volume (daily): 1,000 shares - 1.25x the 20-day average" in report
    # QNT-353 AC3 — multi-timeframe consensus computed in the template header.
    assert "Multi-timeframe consensus: Sideways (daily Uptrend, weekly N/M, monthly N/M" in report
    # QNT-299: machine-parseable as-of footer, uses the DAILY section's date.
    assert report.rstrip().endswith("AS_OF: 2026-04-16")


def test_technical_null_indicators_render_as_nm(monkeypatch: pytest.MonkeyPatch) -> None:
    # Early history: everything except close is null.
    rows = [
        _tech_row(
            as_of=date(2026, 4, 16),
            rsi_14=None,
            macd=None,
            macd_signal=None,
            macd_hist=None,
            sma_20=None,
            sma_50=None,
            sma_200=None,
            bb_upper=None,
            bb_middle=None,
            bb_lower=None,
            bb_pct_b=None,
            adx_14=None,
            atr_14=None,
            macd_bullish_cross=None,
            close=100.0,
            volume=500,
        ),
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
    # QNT-353: the widened indicator lines also degrade to N/M with no history.
    # %B folds onto the Bollinger line, which is itself N/M with no bands.
    assert "SMA-200 (daily): N/M (insufficient history; needs 200 bars)" in report
    assert "ADX-14 (daily): N/M (insufficient history; ≥ 25 trending, < 20 weak)" in report
    assert "ATR-14 (daily): N/M" in report
    assert "MACD bullish cross (daily): N/M" in report


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
        _tech_row(
            as_of=date(2026, 4, 16),
            rsi_14=rsi_value,
            macd=3.9,
            macd_signal=0.9,
            macd_hist=3.0,
            close=198.0,
        ),
    ]
    _install_fake(monkeypatch, {"technical_indicators_daily": _tech_result(rows)})
    report = build_technical_report("NVDA")
    for needle in must_contain:
        assert needle in report, f"RSI={rsi_value} report missing {needle!r}"


def test_technical_header_freshness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Header carries timeframe + days-old count."""
    rows = [
        _tech_row(as_of=date(2026, 4, 16), rsi_14=72.5, macd=3.9, close=198.0),
    ]
    _install_fake(monkeypatch, {"technical_indicators_daily": _tech_result(rows)})
    report = build_technical_report("NVDA")
    assert "daily" in report
    assert "days old" in report


# ---------- technical AC1 (QNT-207) ----------


def _daily_row(*, as_of: date, close: float, prior_close: float | None = None) -> tuple[Any, ...]:
    return _tech_row(as_of=as_of, close=close)


def test_technical_renders_all_three_timeframes(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: ## DAILY, ## WEEKLY, ## MONTHLY all appear; each has PRICE ACTION /
    MOMENTUM / VOLATILITY / TREND; ## SIGNAL does not appear."""
    daily_rows = [
        _daily_row(as_of=date(2026, 4, 16), close=185.0),
        _daily_row(as_of=date(2026, 4, 15), close=184.0),
    ]
    weekly_rows = [
        _tech_row(
            as_of=date(2026, 4, 13),
            rsi_14=60.0,
            macd=2.0,
            sma_20=178.0,
            sma_50=172.0,
            bb_upper=205.0,
            bb_middle=182.0,
            bb_lower=160.0,
            close=187.0,
            volume=5000,
        ),
        _tech_row(
            as_of=date(2026, 4, 6),
            rsi_14=58.0,
            macd=1.8,
            macd_signal=0.4,
            macd_hist=0.4,
            sma_20=175.0,
            sma_50=170.0,
            bb_upper=200.0,
            bb_middle=180.0,
            bb_lower=158.0,
            close=184.0,
            volume=4900,
        ),
    ]
    monthly_rows = [
        _tech_row(
            as_of=date(2026, 4, 1),
            rsi_14=65.0,
            macd=5.0,
            macd_signal=1.0,
            macd_hist=1.0,
            sma_20=170.0,
            sma_50=160.0,
            bb_upper=220.0,
            bb_middle=175.0,
            bb_lower=140.0,
            close=190.0,
            volume=20000,
        ),
        _tech_row(
            as_of=date(2026, 3, 1),
            rsi_14=62.0,
            macd=4.0,
            macd_signal=0.8,
            macd_hist=0.8,
            sma_20=165.0,
            sma_50=155.0,
            bb_upper=210.0,
            bb_middle=170.0,
            bb_lower=135.0,
            close=180.0,
            volume=19000,
        ),
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
        _tech_row(
            as_of=date(2026, 4, 16),
            sma_20=sma_20,
            sma_50=sma_50,
            bb_upper=220.0,
            bb_middle=180.0,
            bb_lower=140.0,
            close=close,
        ),
        _tech_row(
            as_of=date(2026, 4, 15),
            rsi_14=54.0,
            macd=0.9,
            macd_signal=0.4,
            macd_hist=0.4,
            sma_20=sma_20 - 1,
            sma_50=sma_50 - 1,
            bb_upper=218.0,
            bb_middle=178.0,
            bb_lower=138.0,
            close=prior_close,
            volume=900,
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


# ---------- technical QNT-353: consensus + 52w/performance/volume ----------


def _uptrend_pair(as_of: date) -> list[tuple[Any, ...]]:
    """Two bars that resolve to Uptrend (close > SMA-50, SMA-20 > SMA-50, +slope)."""
    return [
        _tech_row(as_of=as_of, sma_20=180.0, sma_50=175.0, close=198.0),
        _tech_row(sma_20=179.0, sma_50=174.0, close=195.0),
    ]


def _downtrend_pair(as_of: date) -> list[tuple[Any, ...]]:
    """Two bars that resolve to Downtrend (close < SMA-50, SMA-20 < SMA-50, -slope)."""
    return [
        _tech_row(as_of=as_of, sma_20=155.0, sma_50=165.0, close=150.0),
        _tech_row(sma_20=156.0, sma_50=166.0, close=160.0),
    ]


def _sideways_pair(as_of: date) -> list[tuple[Any, ...]]:
    """Two bars that resolve to Sideways (SMA-20 > SMA-50 but close < SMA-50)."""
    return [
        _tech_row(as_of=as_of, sma_20=160.0, sma_50=155.0, close=150.0),
        _tech_row(sma_20=159.0, sma_50=154.0, close=149.0),
    ]


def test_technical_consensus_majority_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: two timeframes agreeing (Uptrend + Uptrend vs Downtrend) win."""
    _install_fake(
        monkeypatch,
        {
            "technical_indicators_daily": _tech_result(_uptrend_pair(date(2026, 4, 16))),
            "technical_indicators_weekly": _tech_result(_uptrend_pair(date(2026, 4, 13))),
            "technical_indicators_monthly": _tech_result(_downtrend_pair(date(2026, 4, 1))),
        },
    )
    report = build_technical_report("NVDA")
    assert (
        "Multi-timeframe consensus: Uptrend "
        "(daily Uptrend, weekly Uptrend, monthly Downtrend; majority rule, ties to Sideways)"
    ) in report


def test_technical_consensus_three_way_tie_resolves_sideways(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: no label reaching two votes resolves to Sideways."""
    _install_fake(
        monkeypatch,
        {
            "technical_indicators_daily": _tech_result(_uptrend_pair(date(2026, 4, 16))),
            "technical_indicators_weekly": _tech_result(_downtrend_pair(date(2026, 4, 13))),
            "technical_indicators_monthly": _tech_result(_sideways_pair(date(2026, 4, 1))),
        },
    )
    report = build_technical_report("NVDA")
    assert (
        "Multi-timeframe consensus: Sideways "
        "(daily Uptrend, weekly Downtrend, monthly Sideways; majority rule, ties to Sideways)"
    ) in report


def test_technical_performance_window_na_without_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: a missing window anchor (0-sentinel from argMaxIf) renders N/M for
    that window only, not a fabricated return."""
    _install_fake(
        monkeypatch,
        {
            "technical_indicators_daily": _tech_result(_uptrend_pair(date(2026, 4, 16))),
            # adj_1y absent (0.0 sentinel) -> 1y N/M; the others compute.
            "high_52w": _price_ctx(
                high_52w=220.0,
                low_52w=150.0,
                adj_now=198.0,
                adj_1m=188.0,
                adj_3m=180.0,
                adj_1y=0.0,
                adj_ytd=190.0,
                avg_volume_20=800.0,
            ),
        },
    )
    report = build_technical_report("NVDA")
    assert "1y N/M (insufficient history)" in report
    assert "1m +5.3%" in report


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
    "gross_margin_bps_yoy",
    "net_margin_bps_yoy",
    "ebitda_margin_pct",
    "revenue_ttm",
    "net_income_ttm",
    "fcf_ttm",
    "roe",
    "roa",
    "fcf_yield",
    "debt_to_equity",
    "current_ratio",
)

# The SCALE block derives market cap as latest_close * shares_outstanding
# (matching the report's valuation multiples), a single scalar the fake serves
# via the equity_raw.fundamentals substring in the query. Fixture: NVDA-scale.
_MARKET_CAP_RESULT = _FakeResult(("market_cap",), [(3_000_000_000_000.0,)])


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
        "gross_margin_bps_yoy": 150.0,
        "net_margin_bps_yoy": 120.0,
        "ebitda_margin_pct": None,
        "revenue_ttm": None,
        "net_income_ttm": None,
        "fcf_ttm": None,
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
    assert "Revenue (quarterly): +25.0% YoY" in report
    assert "prior period +18.0%, accelerating" in report


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
    # Peer delta renders at integer precision (QNT-361 follow-up 4): every
    # observed narrator rounding of a peer delta spoke round(x), so the
    # report prints that form. P/E 25.0 vs median 27.50 → 9% discount.
    assert "(9% discount)" in report


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


def test_fundamental_report_renders_next_earnings_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """QNT-357 follow-up: the fundamental header carries the next-earnings date verbatim.

    A bare quick_fact "when does X report earnings" strips the company report and
    routes to the fundamental lens on the "earnings" keyword, so the date must live
    here too to answer that literal ask.
    """
    _install_fake(
        monkeypatch,
        {
            "fundamental_summary": _FakeResult(_FUND_COLS, [_fund_row()]),
            "earnings_calendar": _FakeResult(("next_earnings_date",), [(date(2099, 8, 15),)]),
        },
    )
    report = build_fundamental_report("NVDA")
    assert "Next earnings: 2099-08-15" in report


def test_fundamental_report_next_earnings_na_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """No scheduled (or stale-filtered) date degrades to N/A, matching company.py."""
    _install_fake(
        monkeypatch,
        {
            "fundamental_summary": _FakeResult(_FUND_COLS, [_fund_row()]),
            "earnings_calendar": _FakeResult(("next_earnings_date",), []),
        },
    )
    report = build_fundamental_report("NVDA")
    assert "Next earnings: N/A" in report


def test_fundamental_static_data_disclaimer_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake(monkeypatch, {"fundamental_summary": _FakeResult(_FUND_COLS, [_fund_row()])})
    report = build_fundamental_report("NVDA")
    assert "Data: latest available quarterly fundamentals as of" in report
    assert "2025-12-31" in report


def test_fundamental_as_of_footer(monkeypatch: pytest.MonkeyPatch) -> None:
    # QNT-299: machine-parseable as-of footer, uses period_end.
    _install_fake(monkeypatch, {"fundamental_summary": _FakeResult(_FUND_COLS, [_fund_row()])})
    report = build_fundamental_report("NVDA")
    assert report.rstrip().endswith("AS_OF: 2025-12-31")


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


# ---------- fundamental SCALE + margin bps (QNT-354) ----------


def _ttm_row(**overrides: Any) -> tuple[Any, ...]:
    base = {
        "period_type": "ttm",
        "revenue_ttm": 130_500_000_000.0,
        "net_income_ttm": 72_880_000_000.0,
        "fcf_ttm": 60_850_000_000.0,
        "ebitda_margin_pct": 61.5,
        # bps-YoY columns are never emitted on TTM rows.
        "gross_margin_bps_yoy": None,
        "net_margin_bps_yoy": None,
    }
    base.update(overrides)
    return _fund_row(**base)


def test_fundamental_scale_block_renders_absolute_figures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: SCALE block prints absolute revenue / net income / FCF (TTM) + market cap."""
    rows = [_fund_row(period_type="quarterly"), _ttm_row()]
    _install_fake(monkeypatch, {"fundamental_summary": _FakeResult(_FUND_COLS, rows)})
    report = build_fundamental_report("NVDA")
    assert "## SCALE" in report
    # Scale-suffixed at 1dp (QNT-361 follow-up): the report prints the
    # speakable form so the narrator quotes it verbatim instead of rounding
    # a raw $130,500,000,000 into an ungrounded "$130.5B".
    assert "Revenue (TTM): $130.5B" in report
    assert "Net income (TTM): $72.9B" in report
    assert "Free cash flow (TTM): $60.9B" in report
    assert "Market cap: $3.0T" in report


def test_fundamental_scale_block_nm_without_ttm_or_market_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCALE degrades to N/M when there is no TTM row and no market cap row."""
    _install_fake(
        monkeypatch,
        {
            "fundamental_summary": _FakeResult(_FUND_COLS, [_fund_row(period_type="quarterly")]),
            "equity_raw.fundamentals": _FakeResult(("market_cap",), []),
        },
    )
    report = build_fundamental_report("NVDA")
    assert "## SCALE" in report
    assert "Revenue (TTM): N/M" in report
    assert "Market cap: N/M" in report


def test_fundamental_scale_market_cap_zero_sentinel_renders_nm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing close or shares collapses the close*shares scalar subquery to
    0; the 0-sentinel must render N/M, not a misleading "$0"."""
    _install_fake(
        monkeypatch,
        {
            "fundamental_summary": _FakeResult(_FUND_COLS, [_fund_row(period_type="quarterly")]),
            "equity_raw.fundamentals": _FakeResult(("market_cap",), [(0.0,)]),
        },
    )
    report = build_fundamental_report("NVDA")
    assert "Market cap: N/M" in report


def test_fundamental_profitability_carries_bps_yoy_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: gross/net margin lines carry the pre-computed bps-YoY suffix."""
    rows = [
        _fund_row(period_type="quarterly", net_margin_bps_yoy=120.0, gross_margin_bps_yoy=-40.0)
    ]
    _install_fake(monkeypatch, {"fundamental_summary": _FakeResult(_FUND_COLS, rows)})
    report = build_fundamental_report("NVDA")
    assert "Net margin (quarterly): 25.0%" in report
    assert "+120 bps YoY" in report
    assert "-40 bps YoY" in report


def test_fundamental_ttm_section_renders_ebitda_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: TTM section renders ebitda_margin_pct where the asset emits it."""
    rows = [_fund_row(period_type="quarterly"), _ttm_row(ebitda_margin_pct=61.5)]
    _install_fake(monkeypatch, {"fundamental_summary": _FakeResult(_FUND_COLS, rows)})
    report = build_fundamental_report("NVDA")
    assert "EBITDA margin (ttm): 61.5%" in report
    # Quarterly section (no ebitda_margin_pct) omits the EBITDA line.
    quarterly_block = report.split("## TTM")[0]
    assert "EBITDA margin" not in quarterly_block
    # The asset never emits bps-YoY on TTM rows, so the TTM PROFITABILITY
    # section must carry no bps-YoY suffix (invariant lock).
    ttm_block = report.split("## TTM")[1]
    assert "bps YoY" not in ttm_block


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


_NEWS_COLS = ("published_at", "source", "publisher_name", "headline", "body_snippet")


def _news_row(
    *,
    published: datetime,
    source: str = "finnhub",
    publisher_name: str = "Yahoo",
    headline: str = "headline",
    body_snippet: str = "",
) -> tuple[Any, ...]:
    return (published, source, publisher_name, headline, body_snippet)


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
            publisher_name="CNBC",
            headline="NVDA hits new high",
            body_snippet="Earnings blew past estimates as data centre demand surged",
        ),
        _news_row(
            published=datetime(2026, 4, 15, tzinfo=UTC),
            publisher_name="Reuters",
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
    # Outlet (publisher_name) roll-up rendered — not the finnhub feed label.
    assert "## SOURCES" in report
    assert "CNBC: 1" in report
    assert "Reuters: 1" in report
    # QNT-299: as-of footer uses the newest headline's own date, not "today".
    assert report.rstrip().endswith("AS_OF: 2026-04-16")


def test_news_as_of_footer_nm_when_no_headlines(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake(monkeypatch, {"news_raw": _FakeResult(_NEWS_COLS, [])})
    report = build_news_report("NVDA")
    assert report.rstrip().endswith("AS_OF: N/M (no dated data available)")


def test_news_lookback_and_cap_constants() -> None:
    """AC3: lookback widened 7 -> 14, cap widened 10 -> 20 (QNT-207)."""
    from api.templates import news as news_module_local

    assert news_module_local._LOOKBACK_DAYS == 14
    assert news_module_local._MAX_HEADLINES == 20


def test_news_header_advertises_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake(monkeypatch, {"news_raw": _FakeResult(_NEWS_COLS, [])})
    report = build_news_report("NVDA")
    assert "Lookback: last 14 days, up to 20 headlines" in report


def test_news_snippet_budget_pinned_to_fold(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: the digest snippet budget is the shared constant (280), pinned to the
    RAG fold's news-body budget so they cannot drift silently."""
    from agent.support import _NEWS_BODY_MAX_CHARS
    from shared.retrieval import NEWS_BODY_SNIPPET_CHARS

    assert NEWS_BODY_SNIPPET_CHARS == 280
    assert _NEWS_BODY_MAX_CHARS == NEWS_BODY_SNIPPET_CHARS
    # The digest's SQL substring truncates at exactly the shared budget.
    captured: dict[str, Any] = {}

    def fake_client() -> Any:
        class _C:
            def query(self, query: str, parameters: dict[str, Any]) -> _FakeResult:
                captured["query"] = query
                return _FakeResult(_NEWS_COLS, [])

        return _C()

    monkeypatch.setattr(news_module, "get_client", fake_client)
    build_news_report("NVDA")
    assert f"substring(body, 1, {NEWS_BODY_SNIPPET_CHARS})" in captured["query"]


def test_news_dedups_near_duplicate_headlines(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: near-dup headlines from several outlets collapse to one bullet with an
    'also covered by N sources' suffix; a distinct story stays separate."""
    rows = [
        # One wire story republished across three outlets (headline reworded /
        # re-cased slightly -- normalisation makes the token sets near-identical).
        # The ingestion feed (source) is finnhub for all; breadth keys off the
        # publisher_name outlet.
        _news_row(
            published=datetime(2026, 4, 16, tzinfo=UTC),
            publisher_name="Reuters",
            headline="Nvidia unveils new Blackwell Ultra AI chip",
            body_snippet="Reuters take on the launch",
        ),
        _news_row(
            published=datetime(2026, 4, 16, tzinfo=UTC),
            publisher_name="Bloomberg",
            headline="Nvidia Unveils New Blackwell Ultra AI Chip",
            body_snippet="Bloomberg take on the launch",
        ),
        _news_row(
            published=datetime(2026, 4, 15, tzinfo=UTC),
            publisher_name="CNBC",
            headline="Nvidia unveils new Blackwell Ultra AI chip.",
            body_snippet="CNBC take on the launch",
        ),
        _news_row(
            published=datetime(2026, 4, 14, tzinfo=UTC),
            publisher_name="Benzinga",
            headline="Analyst flags margin risk at Nvidia",
            body_snippet="Bears point to compressing margins",
        ),
    ]
    _install_fake(monkeypatch, {"news_raw": _FakeResult(_NEWS_COLS, rows)})
    report = build_news_report("NVDA")

    headlines_block = report.split("## SOURCES")[0]
    # The three near-dup chip stories collapse to a single representative bullet
    # (the newest, Reuters) carrying the coverage-breadth suffix for the 2 others.
    assert "[Reuters] Nvidia unveils new Blackwell Ultra AI chip" in headlines_block
    assert "(also covered by 2 sources)" in headlines_block
    assert headlines_block.count("Blackwell") == 1
    # Only the representative's snippet renders for the collapsed story.
    assert "Bloomberg take on the launch" not in headlines_block
    assert "CNBC take on the launch" not in headlines_block
    # The distinct story survives as its own bullet with no suffix.
    assert "Analyst flags margin risk at Nvidia" in headlines_block
    # SOURCES roll-up reflects per-outlet article volume (publisher_name).
    assert "Reuters: 1" in report
    assert "Bloomberg: 1" in report
    assert "Benzinga: 1" in report


def test_news_single_source_dup_collapses_without_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two near-identical headlines from the SAME outlet collapse to one bullet,
    but with no 'also covered by' suffix (no coverage breadth to report)."""
    rows = [
        _news_row(
            published=datetime(2026, 4, 16, tzinfo=UTC),
            publisher_name="Yahoo",
            headline="Nvidia announces new AI chip lineup today",
            body_snippet="first",
        ),
        _news_row(
            published=datetime(2026, 4, 16, tzinfo=UTC),
            publisher_name="Yahoo",
            headline="Nvidia announces new AI chip lineup",
            body_snippet="second",
        ),
    ]
    _install_fake(monkeypatch, {"news_raw": _FakeResult(_NEWS_COLS, rows)})
    report = build_news_report("NVDA")
    headlines_block = report.split("## SOURCES")[0]
    assert "also covered by" not in headlines_block
    assert "first" in headlines_block
    assert "second" not in headlines_block


def test_news_dedup_keeps_distinct_but_similar_stories_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headlines that share most tokens but differ in a KEY discriminator must NOT
    merge -- merging would hide a materially different story. Q3-vs-Q4 earnings
    (Jaccard 0.71) and a same-day rise-vs-fall reversal (0.60) both sit below the
    0.8 threshold and stay as separate bullets with no 'also covered by' suffix."""
    rows = [
        _news_row(
            published=datetime(2026, 4, 16, 12, 0, tzinfo=UTC),
            publisher_name="Reuters",
            headline="Nvidia Reports Record Q3 2026 Earnings",
        ),
        _news_row(
            published=datetime(2026, 4, 16, 11, 0, tzinfo=UTC),
            publisher_name="Bloomberg",
            headline="Nvidia Reports Record Q4 2026 Earnings",
        ),
        _news_row(
            published=datetime(2026, 4, 16, 10, 0, tzinfo=UTC),
            publisher_name="CNBC",
            headline="Nvidia shares rise on strong data center demand",
        ),
        _news_row(
            published=datetime(2026, 4, 16, 9, 0, tzinfo=UTC),
            publisher_name="Yahoo",
            headline="Nvidia shares fall on weak data center demand",
        ),
    ]
    _install_fake(monkeypatch, {"news_raw": _FakeResult(_NEWS_COLS, rows)})
    report = build_news_report("NVDA")
    headlines_block = report.split("## SOURCES")[0]
    # All four survive as their own bullets; none collapse.
    assert "Q3 2026 Earnings" in headlines_block
    assert "Q4 2026 Earnings" in headlines_block
    assert "shares rise on strong" in headlines_block
    assert "shares fall on weak" in headlines_block
    assert "also covered by" not in headlines_block
    assert headlines_block.count("\n- ") == 4


def test_news_dedup_frees_slots_for_crowded_out_stories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-9: collapsing a syndicated story frees its slots for the next distinct
    story. Dedup runs over the fetch pool, so a distinct story a raw LIMIT-20 would
    crowd out still renders. The five copies of story A occupy the newest slots; a
    raw top-20 would show A x5 + only 15 distinct, dropping distinct 16-19."""
    from api.templates import news as news_module_local

    rows = [
        # 5 outlets, one syndicated story, newest timestamps.
        _news_row(
            published=datetime(2026, 4, 20, 12, i, tzinfo=UTC),
            publisher_name=pub,
            headline="Nvidia unveils Blackwell Ultra AI accelerator",
            body_snippet=f"copy from {pub}",
        )
        for i, pub in enumerate(["Reuters", "Bloomberg", "CNBC", "Yahoo", "Benzinga"])
    ] + [
        # 20 genuinely distinct stories (token-disjoint so they don't cluster),
        # progressively older so they follow the syndicated cluster.
        _news_row(
            published=datetime(2026, 4, 19, 12, 0, tzinfo=UTC) - timedelta(hours=k),
            publisher_name="Yahoo",
            headline=f"story{k} alpha{k} bravo{k} charlie{k}",
            body_snippet=f"body {k}",
        )
        for k in range(1, 21)
    ]
    assert len(rows) == 25
    _install_fake(monkeypatch, {"news_raw": _FakeResult(_NEWS_COLS, rows)})
    report = build_news_report("NVDA")
    headlines_block = report.split("## SOURCES")[0]

    # The syndicated story collapses to one bullet + a 4-outlet breadth suffix.
    assert headlines_block.count("Blackwell Ultra AI accelerator") == 1
    assert "(also covered by 4 sources)" in headlines_block
    # Exactly _MAX_HEADLINES bullets render (1 cluster + 19 distinct = 20).
    assert headlines_block.count("\n- ") == news_module_local._MAX_HEADLINES
    # Distinct story 19 would be dropped by a raw LIMIT-20 (A x5 + 15 distinct);
    # dedup backfills it. Story 20 is the one that legitimately falls off the cap.
    assert "story19 alpha19" in headlines_block
    assert "story20 alpha20" not in headlines_block


# ---------- company ----------


def _company_install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pe: float | None = 25.0,
    rev_yoy: float | None = 12.0,
    daily_rows: list[tuple[Any, ...]] | None = None,
    next_earnings: date | None = date(2099, 8, 15),
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
    earnings_rows = [(next_earnings,)] if next_earnings is not None else []
    _install_fake(
        monkeypatch,
        {
            "fundamental_summary": _FakeResult(_FUND_COLS, fund_rows),
            "technical_indicators_daily": _tech_result(daily_rows),
            "earnings_calendar": _FakeResult(("next_earnings_date",), earnings_rows),
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
    # QNT-299: static profile is evergreen -- as-of footer is today's date.
    assert report.rstrip().endswith(f"AS_OF: {date.today().isoformat()}")


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

    _company_install(monkeypatch, pe=25.0, rev_yoy=12.0, next_earnings=date(2099, 8, 15))
    report = build_company_report("NVDA")
    assert "## CONTEXT NOW" in report
    # P/E cited verbatim
    assert "Latest P/E: 25.00" in report
    # Revenue YoY cited verbatim
    assert "Latest revenue YoY: +12.0%" in report
    # Trend label derived from daily data
    assert "Daily trend:" in report
    # QNT-357: next earnings date rendered verbatim (ADR-012)
    assert "Next earnings: 2099-08-15" in report


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
            "earnings_calendar": _FakeResult(("next_earnings_date",), []),
        },
    )
    report = build_company_report("NVDA")
    assert "## CONTEXT NOW" in report
    assert "Latest P/E: N/A" in report
    # Trend should still be cited (daily rows present) — a cited "trend label"
    # satisfies AC4 by itself.
    assert "Daily trend:" in report
    # QNT-357: no scheduled date degrades to N/A like its siblings.
    assert "Next earnings: N/A" in report


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
    assert "Latest revenue YoY: +12.0%" in compact
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
