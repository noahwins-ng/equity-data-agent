"""Lean comparison-metrics builder (QNT-224).

The rich two-ticker comparison fetches a full company / fundamental /
technical / news bundle per ticker — a token explosion past two names. For a
3-4 way comparison the analyst wants a compact metrics row per ticker, not a
full thesis each. This module computes that row (P/E, RSI, net margin, latest
price) straight from ClickHouse and hands back formatted strings.

ADR-003 / CLAUDE.md: all math lives in SQL. These helpers only pick the
latest snapshot per ticker (``argMax``) and format the result — no cross-ticker
arithmetic, no synthetic deltas. The agent renders the row verbatim and lets
the narrate node speak the qualitative contrast.

Snapshot choices mirror the existing report layer so the lean numbers agree
with what the rich path already shows:

* P/E + net margin: latest ``period_type = 'quarterly'`` row, exactly the
  snapshot ``fundamental._fetch_peer_medians`` uses for peer P/E.
* RSI: latest daily ``rsi_14``.
* Price: latest daily close.

``argMax(col, key)`` is aliased to a DISTINCT name (``pe``/``margin``/``rsi``/
``price``) to stay clear of the QNT-148 CTE-alias trap (aliasing
``max(col) AS col`` makes an inner filter bind to the aggregate).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from shared.tickers import TICKERS

from api.clickhouse import get_client
from api.formatters import format_currency, format_pct, format_ratio

# Cap mirrors the agent's N-way cap (graph._resolve_comparison_tickers). The
# endpoint is lenient about extra symbols but never builds more than this.
_MAX_TICKERS = 4
_MIN_TICKERS = 2


class ComparisonMetricRow(BaseModel):
    """One ticker's compact metrics row, pre-formatted for table display."""

    ticker: str = Field(description="Ticker symbol this row describes.")
    pe: str = Field(description="Latest quarterly P/E, e.g. '28.4' or 'N/M (...)'.")
    rsi: str = Field(description="Latest daily RSI-14, e.g. '65.2'.")
    net_margin: str = Field(description="Latest quarterly net margin, e.g. '24.1%'.")
    price: str = Field(description="Latest daily close, e.g. '$182.50'.")


class ComparisonMetricsResponse(BaseModel):
    """Lean N-way comparison payload: one metrics row per ticker, in order."""

    rows: list[ComparisonMetricRow] = Field(
        description="Metrics rows in the order the tickers were requested.",
    )


def _resolve_tickers(raw: str) -> list[str]:
    """Parse + validate the comma-separated ``tickers`` query param.

    Uppercases, drops blanks and unknown symbols, de-dupes preserving order,
    and caps at ``_MAX_TICKERS``. Returns the cleaned list; the caller decides
    whether too few survived.
    """
    seen: list[str] = []
    for part in raw.split(","):
        symbol = part.strip().upper()
        if symbol and symbol in TICKERS and symbol not in seen:
            seen.append(symbol)
    return seen[:_MAX_TICKERS]


def _fetch_fundamentals(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Latest quarterly P/E + net margin per ticker."""
    client = get_client()
    query = """
        SELECT
            ticker,
            argMax(pe_ratio, period_end) AS pe,
            argMax(net_margin_pct, period_end) AS margin
        FROM equity_derived.fundamental_summary FINAL
        WHERE ticker IN %(tickers)s AND period_type = 'quarterly'
        GROUP BY ticker
    """
    result = client.query(query, parameters={"tickers": tickers})
    return {row[0]: {"pe": row[1], "margin": row[2]} for row in result.result_rows}


def _fetch_rsi(tickers: list[str]) -> dict[str, float | None]:
    """Latest daily RSI-14 per ticker."""
    client = get_client()
    query = """
        SELECT ticker, argMax(rsi_14, date) AS rsi
        FROM equity_derived.technical_indicators_daily FINAL
        WHERE ticker IN %(tickers)s
        GROUP BY ticker
    """
    result = client.query(query, parameters={"tickers": tickers})
    return {row[0]: row[1] for row in result.result_rows}


def _fetch_price(tickers: list[str]) -> dict[str, float | None]:
    """Latest daily close per ticker."""
    client = get_client()
    query = """
        SELECT ticker, argMax(close, date) AS price
        FROM equity_raw.ohlcv_raw FINAL
        WHERE ticker IN %(tickers)s
        GROUP BY ticker
    """
    result = client.query(query, parameters={"tickers": tickers})
    return {row[0]: row[1] for row in result.result_rows}


def build_comparison_metrics(tickers: list[str]) -> ComparisonMetricsResponse:
    """Assemble one formatted metrics row per ticker, in the given order.

    ``tickers`` is assumed pre-validated (see :func:`_resolve_tickers`). A
    ticker with no row in a given table renders its cell as ``N/M`` rather
    than dropping the whole row — a half-empty row still anchors the
    comparison.
    """
    fundamentals = _fetch_fundamentals(tickers)
    rsi_by_ticker = _fetch_rsi(tickers)
    price_by_ticker = _fetch_price(tickers)

    rows: list[ComparisonMetricRow] = []
    for ticker in tickers:
        fund = fundamentals.get(ticker, {})
        rows.append(
            ComparisonMetricRow(
                ticker=ticker,
                pe=format_ratio(fund.get("pe"), precision=1, na_reason="data unavailable"),
                rsi=format_ratio(rsi_by_ticker.get(ticker), precision=1),
                net_margin=format_pct(fund.get("margin"), precision=1),
                price=format_currency(price_by_ticker.get(ticker)),
            )
        )
    return ComparisonMetricsResponse(rows=rows)


__all__ = [
    "ComparisonMetricRow",
    "ComparisonMetricsResponse",
    "build_comparison_metrics",
    "_resolve_tickers",
    "_MIN_TICKERS",
    "_MAX_TICKERS",
]
