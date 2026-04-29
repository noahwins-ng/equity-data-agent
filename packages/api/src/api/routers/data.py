"""Data endpoints — JSON arrays consumed by the Next.js frontend.

These are the counterpart to the report endpoints: the reports return
pre-rendered text for the LangGraph agent, while these return structured
arrays for chart rendering (TradingView Lightweight Charts, etc.).
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, HTTPException
from shared.tickers import ALL_OHLCV_TICKERS, TICKER_METADATA, TICKERS

from api.clickhouse import get_client

router = APIRouter(prefix="/api/v1", tags=["data"])


class Timeframe(StrEnum):
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"


_TIMEFRAME_QUERY: dict[Timeframe, tuple[str, str]] = {
    Timeframe.daily: ("equity_raw.ohlcv_raw", "date"),
    Timeframe.weekly: ("equity_derived.ohlcv_weekly", "week_start"),
    Timeframe.monthly: ("equity_derived.ohlcv_monthly", "month_start"),
}

_INDICATOR_COLUMNS = (
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

_INDICATOR_TIMEFRAME_QUERY: dict[Timeframe, tuple[str, str]] = {
    Timeframe.daily: ("equity_derived.technical_indicators_daily", "date"),
    Timeframe.weekly: ("equity_derived.technical_indicators_weekly", "week_start"),
    Timeframe.monthly: ("equity_derived.technical_indicators_monthly", "month_start"),
}

_RSI_OVERBOUGHT = 70.0
_RSI_OVERSOLD = 30.0

# Sparkline window length on the dashboard (~3 trading months) — matches the
# 60-bar context the design v2 watchlist cards render. Computed server-side
# in one query so the frontend page-load avoids the N+1 fan-out per ticker.
_SPARKLINE_BARS = 60


def _rsi_signal(rsi: float | None) -> str:
    if rsi is None:
        return "neutral"
    if rsi >= _RSI_OVERBOUGHT:
        return "overbought"
    if rsi <= _RSI_OVERSOLD:
        return "oversold"
    return "neutral"


def _trend_status(price: float, sma_50: float | None) -> str:
    if sma_50 is None:
        return "neutral"
    return "bullish" if price >= sma_50 else "bearish"


_FUNDAMENTAL_COLUMNS = (
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


@router.get("/dashboard/summary")
def get_dashboard_summary() -> list[dict[str, Any]]:
    """Return a compact summary row per ticker for the dashboard landing page.

    One JSON array covering all configured tickers — avoids the N+1 request
    fan-out the frontend would otherwise need on page load. Each row carries
    the company short ``name`` (sourced from ``TICKER_METADATA``), today's
    actual ``close`` (not ``adj_close`` — we want market price), the
    day-over-day change, the latest RSI-14 + SMA-50, pre-categorized
    ``rsi_signal`` / ``trend_status`` labels, and a 60-bar ``sparkline``
    array (recent daily closes, oldest first) so the watchlist sparkline
    chart renders without an extra ``/ohlcv`` round-trip per ticker. Tickers
    without at least one OHLCV row are omitted.
    """
    query = """
        WITH
        ohlcv_ranked AS (
            SELECT
                ticker,
                date,
                close,
                row_number() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
            FROM equity_raw.ohlcv_raw FINAL
            WHERE ticker IN %(tickers)s
        ),
        ohlcv_recent AS (
            SELECT
                ticker,
                anyIf(close, rn = 1) AS price,
                anyIf(close, rn = 2) AS prior_close
            FROM ohlcv_ranked
            WHERE rn <= 2
            GROUP BY ticker
        ),
        sparkline_recent AS (
            -- ClickHouse's groupArray is documented as having an implementation-
            -- defined order, so we materialize (date, close) pairs, sort by date
            -- ascending, then project close. Ascending date = oldest-first =
            -- left-to-right chart order.
            SELECT
                ticker,
                arrayMap(t -> t.2, arraySort(t -> t.1, groupArray((date, close)))) AS sparkline
            FROM ohlcv_ranked
            WHERE rn <= %(bars)s
            GROUP BY ticker
        ),
        indicators_latest AS (
            SELECT
                ticker,
                argMax(rsi_14, date) AS rsi_14,
                argMax(sma_50, date) AS sma_50
            FROM equity_derived.technical_indicators_daily FINAL
            WHERE ticker IN %(tickers)s
            GROUP BY ticker
        )
        SELECT
            o.ticker AS ticker,
            o.price AS price,
            o.prior_close AS prior_close,
            i.rsi_14 AS rsi_14,
            i.sma_50 AS sma_50,
            s.sparkline AS sparkline
        FROM ohlcv_recent AS o
        LEFT JOIN indicators_latest AS i ON o.ticker = i.ticker
        LEFT JOIN sparkline_recent AS s ON o.ticker = s.ticker
    """
    result = get_client().query(
        query,
        parameters={"tickers": list(TICKERS), "bars": _SPARKLINE_BARS},
    )

    order = {ticker: idx for idx, ticker in enumerate(TICKERS)}
    rows: list[dict[str, Any]] = []
    for row in result.result_rows:
        record = dict(zip(result.column_names, row, strict=True))
        price = float(record["price"])
        prior_close = record["prior_close"]
        rsi = record["rsi_14"]
        sma_50 = record["sma_50"]

        daily_change_pct: float | None = None
        if prior_close is not None and float(prior_close) != 0.0:
            daily_change_pct = (price - float(prior_close)) / float(prior_close) * 100

        sparkline = record["sparkline"] or []
        meta = TICKER_METADATA.get(record["ticker"], {})
        rows.append(
            {
                "ticker": record["ticker"],
                "name": meta.get("name", record["ticker"]),
                "price": price,
                "daily_change_pct": daily_change_pct,
                "rsi_14": rsi,
                "rsi_signal": _rsi_signal(rsi),
                "trend_status": _trend_status(price, sma_50),
                "sparkline": [float(v) for v in sparkline],
            }
        )
    rows.sort(key=lambda r: order.get(r["ticker"], len(order)))
    return rows


@router.get("/ohlcv/{ticker}")
def get_ohlcv(
    ticker: str,
    timeframe: Timeframe = Timeframe.daily,
) -> list[dict[str, Any]]:
    """Return OHLCV rows for ``ticker`` at the requested ``timeframe``.

    Response shape matches TradingView Lightweight Charts' candlestick input:
    ``{time, open, high, low, close, adj_close, volume}[]`` where ``time`` is
    an ISO date string (``YYYY-MM-DD``) — the library accepts this directly, so
    the frontend needs no transformation. Benchmark tickers (SPY) are valid
    here but rejected by ``/fundamentals`` and ``/search/news``.
    """
    ticker = ticker.upper()
    if ticker not in ALL_OHLCV_TICKERS:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")

    table, date_col = _TIMEFRAME_QUERY[timeframe]
    query = f"""
        SELECT {date_col} AS time, open, high, low, close, adj_close, volume
        FROM {table} FINAL
        WHERE ticker = %(ticker)s
        ORDER BY {date_col} ASC
    """
    result = get_client().query(query, parameters={"ticker": ticker})

    rows: list[dict[str, Any]] = []
    for row in result.result_rows:
        record = dict(zip(result.column_names, row, strict=True))
        time_value = record["time"]
        if isinstance(time_value, date):
            record["time"] = time_value.isoformat()
        rows.append(record)
    return rows


@router.get("/fundamentals/{ticker}")
def get_fundamentals(ticker: str) -> list[dict[str, Any]]:
    """Return computed fundamental ratios for ``ticker``.

    Response shape is ``{ticker, period_end, period_type, pe_ratio, ev_ebitda,
    price_to_book, price_to_sales, eps, revenue_yoy_pct, net_income_yoy_pct,
    fcf_yoy_pct, net_margin_pct, gross_margin_pct, roe, roa, fcf_yield,
    debt_to_equity, current_ratio}[]`` where ``period_end`` is an ISO date
    string (``YYYY-MM-DD``). Rows are returned most-recent-first to match the
    ticker-detail ratios table layout, and every ratio column is nullable
    (undefined when the denominator is zero or data is missing).
    """
    ticker = ticker.upper()
    if ticker not in TICKERS:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")

    columns = ", ".join(_FUNDAMENTAL_COLUMNS)
    query = f"""
        SELECT {columns}
        FROM equity_derived.fundamental_summary FINAL
        WHERE ticker = %(ticker)s
        ORDER BY period_end DESC, period_type ASC
    """
    result = get_client().query(query, parameters={"ticker": ticker})

    rows: list[dict[str, Any]] = []
    for row in result.result_rows:
        record = dict(zip(result.column_names, row, strict=True))
        period_end = record["period_end"]
        if isinstance(period_end, date):
            record["period_end"] = period_end.isoformat()
        rows.append(record)
    return rows


@router.get("/indicators/{ticker}")
def get_indicators(
    ticker: str,
    timeframe: Timeframe = Timeframe.daily,
) -> list[dict[str, Any]]:
    """Return pre-computed technical indicator rows for ``ticker``.

    Response shape is ``{time, sma_20, sma_50, ema_12, ema_26, rsi_14, macd,
    macd_signal, macd_hist, bb_upper, bb_middle, bb_lower}[]`` where ``time``
    is an ISO date string (``YYYY-MM-DD``). Indicator fields are nullable during
    the warm-up period (e.g. SMA-50 needs 50 prior closes) and those nulls are
    preserved in the response — the frontend whitepaints them on the overlay.
    """
    ticker = ticker.upper()
    if ticker not in TICKERS:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")

    table, date_col = _INDICATOR_TIMEFRAME_QUERY[timeframe]
    columns = ", ".join(_INDICATOR_COLUMNS)
    query = f"""
        SELECT {date_col} AS time, {columns}
        FROM {table} FINAL
        WHERE ticker = %(ticker)s
        ORDER BY {date_col} ASC
    """
    result = get_client().query(query, parameters={"ticker": ticker})

    rows: list[dict[str, Any]] = []
    for row in result.result_rows:
        record = dict(zip(result.column_names, row, strict=True))
        time_value = record["time"]
        if isinstance(time_value, date):
            record["time"] = time_value.isoformat()
        rows.append(record)
    return rows
