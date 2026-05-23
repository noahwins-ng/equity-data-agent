"""Fundamental report template — parameterised from the technical template.

Structure mirrors technical.py (header, sections, signal) but pulls from
``equity_derived.fundamental_summary``. Shows the latest period alongside
up to 20 quarters of history so the reader sees own-history percentile rank
(AC1), peer context (AC2), prior-quarter deltas on valuation and profitability
(AC3), and a freshness signal in the header (AC4).
"""

from __future__ import annotations

import math
import statistics
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

# Minimum number of peer tickers (after dropping the target) required to
# compute a meaningful sector median — below this the N/A path is taken.
_MIN_PEERS_FOR_MEDIAN = 3


def _history_stats(values: list[float]) -> tuple[float, float, float, int] | None:
    """Return (min, max, median, pct_rank) or None if fewer than 2 values.

    Percentile rank = (count strictly below values[0]) / (n-1) * 100, rounded.
    values[0] is the current (newest) observation.
    """
    if len(values) < 2:
        return None
    lo = min(values)
    hi = max(values)
    med = statistics.median(values)
    current = values[0]
    n = len(values)
    rank = sum(1 for v in values if v < current)
    pct = round(rank / (n - 1) * 100)
    return lo, hi, med, pct


def _valuation_label(
    value: float | None,
    history_values: list[float],
    thresholds: str = "",
    na_reason: str = "data unavailable",
    prior_value: float | None = None,
) -> str:
    """Format a valuation multiple with 5y history range, percentile, prior-quarter delta.

    Canonical thresholds always appear in the output so the agent quotes them
    from the report rather than leaking them from prior knowledge (ADR-012).
    """
    if value is None or not math.isfinite(value):
        suffix = f"; {thresholds}" if thresholds else ""
        return f"N/M ({na_reason}{suffix})"
    parts: list[str] = []
    stats = _history_stats(history_values)
    if stats:
        lo, hi, _med, pct = stats
        parts.append(f"range {format_ratio(lo)}–{format_ratio(hi)} over last 5y, {pct}th pct")
    if prior_value is not None and math.isfinite(prior_value):
        direction = (
            "expanding"
            if value > prior_value
            else "contracting"
            if value < prior_value
            else "steady"
        )
        parts.append(f"prior quarter {format_ratio(prior_value)}, {direction}")
    if thresholds:
        parts.append(thresholds)
    if parts:
        return f"{format_ratio(value)} ({', '.join(parts)})"
    return format_ratio(value)


def _pe_label(
    pe: float | None,
    eps: float | None,
    history_values: list[float],
    prior_value: float | None = None,
) -> str:
    """Format P/E with 5y history range, percentile, prior-quarter delta, and thresholds.

    Mirrors ``_rsi_label`` in ``technical.py``: canonical valuation thresholds
    (rich ≥ 40, cheap ≤ 20) appear in every record so the agent's synthesize
    step quotes them from the report. See ADR-012.
    """
    return _valuation_label(
        pe,
        history_values,
        thresholds=_PE_THRESHOLDS,
        na_reason=pe_na_reason(eps),
        prior_value=prior_value,
    )


def _margin_line(value: float | None, prior: float | None, label: str) -> str:
    """Format a percentage metric with optional prior-quarter delta."""
    if value is None or not math.isfinite(value):
        return f"{label}: {format_pct(value)}"
    line = f"{label}: {format_pct(value)}"
    if prior is not None and math.isfinite(prior):
        direction = "expanding" if value > prior else "contracting" if value < prior else "steady"
        line += f" (prior quarter {format_pct(prior)}, {direction})"
    return line


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
    """Return up to 20 recent quarterly rows for ``ticker``, newest first.

    20 rows ≈ 5 years of quarters — enough for a meaningful own-history
    percentile rank across the valuation multiples.
    """
    client = get_client()
    cols = ", ".join(_RATIO_COLUMNS)
    query = f"""
        SELECT {cols}
        FROM equity_derived.fundamental_summary FINAL
        WHERE ticker = %(ticker)s AND period_type = 'quarterly'
        ORDER BY period_end DESC
        LIMIT 20
    """
    result = client.query(query, parameters={"ticker": ticker})
    return [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]


def _fetch_peer_medians(peers: list[str]) -> dict[str, float | None]:
    """Return median pe_ratio / ev_ebitda / price_to_sales across ``peers``.

    Uses argMax to get the latest quarterly snapshot per ticker, then computes
    medians in Python. Used by the PEER CONTEXT section.
    """
    if not peers:
        return {"pe_ratio": None, "ev_ebitda": None, "price_to_sales": None}
    client = get_client()
    query = """
        SELECT
            argMax(pe_ratio, period_end) AS pe_ratio,
            argMax(ev_ebitda, period_end) AS ev_ebitda,
            argMax(price_to_sales, period_end) AS price_to_sales
        FROM equity_derived.fundamental_summary FINAL
        WHERE ticker IN %(peers)s AND period_type = 'quarterly'
        GROUP BY ticker
    """
    result = client.query(query, parameters={"peers": peers})
    rows = result.result_rows

    def _median(vals: list[Any]) -> float | None:
        finite = [v for v in vals if v is not None and math.isfinite(v)]
        return statistics.median(finite) if finite else None

    return {
        "pe_ratio": _median([r[0] for r in rows]),
        "ev_ebitda": _median([r[1] for r in rows]),
        "price_to_sales": _median([r[2] for r in rows]),
    }


def _peer_context_lines(
    ticker: str,
    sector: str,
    latest: dict[str, Any],
) -> list[str]:
    """Build ## PEER CONTEXT section lines."""
    peers = [
        t for t in TICKERS if t != ticker and TICKER_METADATA.get(t, {}).get("sector") == sector
    ]
    lines: list[str] = ["## PEER CONTEXT"]

    if len(peers) < _MIN_PEERS_FOR_MEDIAN:
        n = len(peers)
        noun = "peer" if n == 1 else "peers"
        lines.append(
            f"Sector median: N/A (insufficient peers in coverage -- "
            f"{sector}, {n} {noun} in coverage)"
        )
        return lines

    medians = _fetch_peer_medians(peers)
    n_peers = len(peers)

    def _peer_line(label: str, metric: str) -> str:
        target = latest.get(metric)
        med = medians.get(metric)
        if (
            target is None
            or not math.isfinite(target)
            or med is None
            or not math.isfinite(med)
            or med == 0
        ):
            return f"Sector median {label} ({sector}, {n_peers} peers in coverage): N/A"
        pct = (target - med) / med * 100
        direction = "premium" if pct >= 0 else "discount"
        pct_str = f"{abs(pct):.1f}%"
        return (
            f"Sector median {label} ({sector}, {n_peers} peers in coverage): "
            f"{format_ratio(med)} -- {ticker} at {format_ratio(target)} ({pct_str} {direction})"
        )

    lines.append(_peer_line("P/E", "pe_ratio"))
    lines.append(_peer_line("EV/EBITDA", "ev_ebitda"))
    lines.append(_peer_line("P/S", "price_to_sales"))
    return lines


def _signal_verdict(latest: dict[str, Any]) -> str:
    """Weighted-vote signal: valuation 2x, growth 2x, profitability 1x each indicator.

    Two profitability indicators (margin + ROE) means effective category weights are
    valuation:growth:profitability = 2:2:2 when all four metrics are present.

    When valuation and growth disagree and neither side wins the weighted vote,
    emits a named asymmetry label instead of plain NEUTRAL:
      - P/E < 20 + contracting revenue   -> MIXED (value-trap risk)
      - P/E > 40 + strong revenue (>10%) -> MIXED (growth-at-a-price)
    Named labels require both pe and rev_yoy to be non-None; without both axes
    the asymmetry cannot be classified and we fall back to generic NEUTRAL.
    """
    pe = latest.get("pe_ratio")
    rev_yoy = latest.get("revenue_yoy_pct")
    margin = latest.get("net_margin_pct")
    roe = latest.get("roe")

    bull = 0
    bear = 0
    total = 0

    # Valuation — 2x weight
    if pe is not None:
        total += 2
        if pe < 20:
            bull += 2
        elif pe > 40:
            bear += 2

    # Growth — 2x weight
    if rev_yoy is not None:
        total += 2
        if rev_yoy > 10:
            bull += 2
        elif rev_yoy < 0:
            bear += 2

    # Profitability: net margin — 1x weight
    if margin is not None:
        total += 1
        if margin > 15:
            bull += 1
        elif margin < 0:
            bear += 1

    # Profitability: ROE — 1x weight
    if roe is not None:
        total += 1
        if roe > 15:
            bull += 1
        elif roe < 0:
            bear += 1

    if total == 0:
        return "N/M (insufficient ratios)"

    if bull > bear and bull >= 2:
        return f"BULLISH ({bull}/{total} weighted indicators agree)"
    if bear > bull and bear >= 2:
        return f"BEARISH ({bear}/{total} weighted indicators agree)"

    # Named asymmetry labels when vote is tied/mixed
    if pe is not None and rev_yoy is not None:
        if pe < 20 and rev_yoy < 0:
            return "MIXED (value-trap risk)"
        if pe > 40 and rev_yoy > 10:
            return "MIXED (growth-at-a-price)"

    return f"NEUTRAL (mixed: {bull} bullish, {bear} bearish, {total - bull - bear} neutral weight)"


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
    sector = str(meta.get("sector", "Unknown sector"))
    industry = str(meta.get("industry", "Unknown industry"))
    days_old = (date.today() - period_end).days

    def _p(key: str) -> Any:
        return prior[key] if prior else None

    def _hist(key: str) -> list[float]:
        return [r[key] for r in rows if r[key] is not None and math.isfinite(r[key])]

    pe_hist = _hist("pe_ratio")
    ev_hist = _hist("ev_ebitda")
    pb_hist = _hist("price_to_book")
    ps_hist = _hist("price_to_sales")

    peer_lines = _peer_context_lines(ticker, sector, latest)

    lines = [
        f"# FUNDAMENTAL REPORT — {ticker}",
        f"As of {period_end.isoformat()} (quarterly, {days_old} days old) — {sector}, {industry}",
        "",
        "## VALUATION",
        f"P/E: {_pe_label(latest['pe_ratio'], latest['eps'], pe_hist, _p('pe_ratio'))}",
        f"EV/EBITDA: {_valuation_label(latest['ev_ebitda'], ev_hist, prior_value=_p('ev_ebitda'))}",
        "Price/Book: "
        f"{_valuation_label(latest['price_to_book'], pb_hist, prior_value=_p('price_to_book'))}",
        "Price/Sales: "
        f"{_valuation_label(latest['price_to_sales'], ps_hist, prior_value=_p('price_to_sales'))}",
        f"EPS: {format_ratio(latest['eps'], na_reason='earnings unavailable')}",
        "",
        *peer_lines,
        "",
        "## GROWTH (YoY)",
        _growth_label(latest["revenue_yoy_pct"], _p("revenue_yoy_pct"), "Revenue"),
        _growth_label(latest["net_income_yoy_pct"], _p("net_income_yoy_pct"), "Net income"),
        _growth_label(latest["fcf_yoy_pct"], _p("fcf_yoy_pct"), "Free cash flow"),
        _GROWTH_REFERENCE,
        "",
        "## PROFITABILITY",
        _margin_line(latest["gross_margin_pct"], _p("gross_margin_pct"), "Gross margin"),
        _margin_line(latest["net_margin_pct"], _p("net_margin_pct"), "Net margin"),
        _margin_line(latest["roe"], _p("roe"), "ROE"),
        _margin_line(latest["roa"], _p("roa"), "ROA"),
        _PROFITABILITY_REFERENCE,
        "",
        "## CASH & LEVERAGE",
        f"FCF yield: {format_pct(latest['fcf_yield'])}",
        f"Debt/Equity: {format_ratio(latest['debt_to_equity'])}",
        f"Current ratio: {format_ratio(latest['current_ratio'])}",
        "",
        "## SIGNAL",
        _signal_verdict(latest),
        "",
        f"Data: latest available quarterly fundamentals as of {period_end.isoformat()}.",
    ]
    return "\n".join(lines)
