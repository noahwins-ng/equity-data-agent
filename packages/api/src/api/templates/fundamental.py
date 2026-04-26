"""Fundamental report template — parameterised from the technical template.

Structure mirrors technical.py (header, sections, signal) but pulls from
``equity_derived.fundamental_summary``. Shows the latest period alongside the
prior period so the reader sees trend acceleration/deceleration, not just a
snapshot.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from fastapi import HTTPException
from shared.tickers import TICKER_METADATA, TICKERS

from api.clickhouse import get_client
from api.formatters import format_pct, format_ratio, format_signed_pct, pe_na_reason

# Canonical valuation/quality reference rates used by ``_signal_verdict``.
# Surfaced verbatim in the report body (ADR-012) so the agent quotes them from
# the corpus instead of reaching for them as fundamental prior knowledge — the
# same fix QNT-136 applied to RSI 70/30 in the technical template.
_PE_THRESHOLDS = "rich ≥ 40, cheap ≤ 20"
_GROWTH_REFERENCE = "Reference rates: ≥ 10% strong, ≤ 0% contraction"
_PROFITABILITY_REFERENCE = (
    "Reference rates: net margin ≥ 15% strong / ≤ 0 loss-making; ROE ≥ 15% strong / ≤ 0 negative"
)

_RATIO_COLUMNS = (
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


def _pe_label(pe: float | None, eps: float | None) -> str:
    """Format a P/E ratio with the canonical rich/cheap thresholds always cited.

    Mirrors ``_rsi_label`` in ``technical.py``: the canonical valuation
    thresholds (rich ≥ 40, cheap ≤ 20) are surfaced in every record so the
    agent's synthesize step quotes them from the report instead of reaching
    for them as fundamental prior knowledge. Fundamental sweeps
    (``20260426T085600Z-9433e1``) showed ``40`` and ``20`` leaking onto
    P/E action lines whenever the report omitted the digits — same root
    cause as the QNT-136 RSI 70/30 leak. See ADR-012.
    """
    if pe is None or not math.isfinite(pe):
        return f"N/M ({pe_na_reason(eps)}; {_PE_THRESHOLDS})"
    return f"{format_ratio(pe)} ({_PE_THRESHOLDS})"


def _growth_label(current: float | None, prior: float | None, metric: str) -> str:
    if current is None:
        return f"{metric}: {format_signed_pct(current, na_reason='data unavailable')}"
    line = f"{metric}: {format_signed_pct(current)} YoY"
    if prior is not None:
        delta = current - prior
        trend = "accelerating" if delta > 1 else "decelerating" if delta < -1 else "steady"
        line += f" (prior period {format_signed_pct(prior)}, {trend})"
    return line


def _fetch_rows(ticker: str) -> list[dict[str, Any]]:
    """Return the two most recent quarterly rows for ``ticker``, newest first.

    Quarterly rows carry the freshest ratios and growth numbers; annual rows
    are useful context but lag the quarterly snapshot.
    """
    client = get_client()
    cols = ", ".join(_RATIO_COLUMNS)
    query = f"""
        SELECT {cols}
        FROM equity_derived.fundamental_summary FINAL
        WHERE ticker = %(ticker)s AND period_type = 'quarterly'
        ORDER BY period_end DESC
        LIMIT 2
    """
    result = client.query(query, parameters={"ticker": ticker})
    return [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]


def _signal_verdict(latest: dict[str, Any]) -> str:
    """Coarse bullish/bearish/neutral vote across valuation, growth, quality."""
    votes: list[str] = []
    pe = latest["pe_ratio"]
    if pe is not None:
        votes.append("bullish" if pe < 20 else "bearish" if pe > 40 else "neutral")
    rev_yoy = latest["revenue_yoy_pct"]
    if rev_yoy is not None:
        votes.append("bullish" if rev_yoy > 10 else "bearish" if rev_yoy < 0 else "neutral")
    margin = latest["net_margin_pct"]
    if margin is not None:
        votes.append("bullish" if margin > 15 else "bearish" if margin < 0 else "neutral")
    roe = latest["roe"]
    if roe is not None:
        votes.append("bullish" if roe > 15 else "bearish" if roe < 0 else "neutral")
    if not votes:
        return "N/M (insufficient ratios)"
    bull = votes.count("bullish")
    bear = votes.count("bearish")
    if bull > bear and bull >= 2:
        return f"BULLISH ({bull}/{len(votes)} indicators agree)"
    if bear > bull and bear >= 2:
        return f"BEARISH ({bear}/{len(votes)} indicators agree)"
    return f"NEUTRAL (mixed: {bull} bullish, {bear} bearish, {len(votes) - bull - bear} neutral)"


def build_fundamental_report(ticker: str) -> str:
    """Build a human-readable fundamental analysis report for ``ticker``."""
    if ticker not in TICKERS:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")

    rows = _fetch_rows(ticker)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No fundamental data for {ticker}")

    latest = rows[0]
    prior = rows[1] if len(rows) > 1 else None
    meta = TICKER_METADATA.get(ticker, {})
    period_end: date = latest["period_end"]

    lines = [
        f"# FUNDAMENTAL REPORT — {ticker}",
        f"As of {period_end.isoformat()} (quarterly) — "
        f"{meta.get('sector', 'Unknown sector')}, "
        f"{meta.get('industry', 'Unknown industry')}",
        "",
        "## VALUATION",
        f"P/E: {_pe_label(latest['pe_ratio'], latest['eps'])}",
        f"EV/EBITDA: {format_ratio(latest['ev_ebitda'])}",
        f"Price/Book: {format_ratio(latest['price_to_book'])}",
        f"Price/Sales: {format_ratio(latest['price_to_sales'])}",
        f"EPS: {format_ratio(latest['eps'], na_reason='earnings unavailable')}",
        "",
        "## GROWTH (YoY)",
        _growth_label(
            latest["revenue_yoy_pct"],
            prior["revenue_yoy_pct"] if prior else None,
            "Revenue",
        ),
        _growth_label(
            latest["net_income_yoy_pct"],
            prior["net_income_yoy_pct"] if prior else None,
            "Net income",
        ),
        _growth_label(
            latest["fcf_yoy_pct"],
            prior["fcf_yoy_pct"] if prior else None,
            "Free cash flow",
        ),
        _GROWTH_REFERENCE,
        "",
        "## PROFITABILITY",
        f"Gross margin: {format_pct(latest['gross_margin_pct'])}",
        f"Net margin: {format_pct(latest['net_margin_pct'])}",
        f"ROE: {format_pct(latest['roe'])}",
        f"ROA: {format_pct(latest['roa'])}",
        _PROFITABILITY_REFERENCE,
        "",
        "## CASH & LEVERAGE",
        f"FCF yield: {format_pct(latest['fcf_yield'])}",
        f"Debt/Equity: {format_ratio(latest['debt_to_equity'])}",
        f"Current ratio: {format_ratio(latest['current_ratio'])}",
        "",
        "## SIGNAL",
        _signal_verdict(latest),
    ]
    return "\n".join(lines)
