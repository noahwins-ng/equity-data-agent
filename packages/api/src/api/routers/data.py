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
