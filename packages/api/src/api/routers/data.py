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
from shared.tickers import TICKERS

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


@router.get("/ohlcv/{ticker}")
def get_ohlcv(
    ticker: str,
    timeframe: Timeframe = Timeframe.daily,
) -> list[dict[str, Any]]:
    """Return OHLCV rows for ``ticker`` at the requested ``timeframe``.

    Response shape matches TradingView Lightweight Charts' candlestick input:
    ``{time, open, high, low, close, adj_close, volume}[]`` where ``time`` is
    an ISO date string (``YYYY-MM-DD``) — the library accepts this directly, so
    the frontend needs no transformation.
    """
    ticker = ticker.upper()
    if ticker not in TICKERS:
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
