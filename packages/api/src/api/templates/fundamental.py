"""Fundamental report template — quarterly + annual + TTM (QNT-207).

Pulls every period_type for ``ticker`` from ``equity_derived.fundamental_summary``
(no longer filtered to quarterly only) and emits three period-scoped sections
so the agent sees execution trajectory (quarterly), full-cycle results
(annual), and a smoothed rolling view (TTM) in one report body.

Each valuation multiple line carries an inline Premium / Inline / Discounted
label derived from own-history IQR (per period_type) and peer median (from
latest quarterly snapshots). The label rule is embedded verbatim in the
disclaimer (ADR-012) so the agent quotes it from the corpus instead of
leaking valuation prior knowledge.

PEER CONTEXT and SIGNAL retain their pre-QNT-207 logic — peer medians render
once at the top (latest quarterly snapshot per peer); the ``_signal_verdict``
weighted vote stays in the codebase as evidence weighting but no longer
renders into the report body. Thesis v2 (QNT-208) consumes per-multiple
labels instead of the composite verdict, so we drop the asymmetry strings
(``value-trap risk`` / ``growth-at-a-price``) from the rendered output to
keep the report focused on the per-multiple signal.
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

# Canonical valuation/quality reference rates surfaced in the report body so
# the agent quotes them from the corpus (ADR-012) instead of leaking the
# numbers from fundamental prior knowledge. ``_signal_verdict`` still uses
# these as its vote thresholds.
_PE_THRESHOLDS = "rich ≥ 40, cheap ≤ 20"
_GROWTH_REFERENCE = "Reference rates: ≥ 10% strong, ≤ 0% contraction"
_PROFITABILITY_REFERENCE = (
    "Reference rates: net margin ≥ 15% strong / ≤ 0 loss-making; ROE ≥ 15% strong / ≤ 0 negative"
)
_VALUATION_LABEL_RULE = (
    "Per-multiple label: Premium = above own-history 75th pct OR ≥ 25% above peer median; "
    "Discounted = below own-history 25th pct OR ≥ 25% below peer median; Inline = otherwise."
)
_PERIOD_DISCLAIMER = (
    "Quarterly captures execution trajectory; annual captures full-cycle results; "
    "TTM smooths quarter-to-quarter noise."
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

# Period sections rendered in this order. The pair is (label, period_type).
_PERIODS: tuple[tuple[str, str], ...] = (
    ("QUARTERLY", "quarterly"),
    ("ANNUAL", "annual"),
    ("TTM", "ttm"),
)

# Premium / Inline / Discounted thresholds (ticket spec).
_PEER_PREMIUM_DELTA_PCT = 25.0
_PEER_INLINE_DELTA_PCT = 15.0


def _history_stats(values: list[float]) -> tuple[float, float, str | None] | None:
    """Return (min, max, position_label) or None if fewer than 2 values.

    position_label is one of 'near 5y low' / 'mid 5y range' / 'near 5y high',
    based on where values[0] (the current observation) sits in the (lo, hi) range.
    Returns None label when hi == lo (degenerate single-value range).
    """
    if len(values) < 2:
        return None
    lo = min(values)
    hi = max(values)
    if hi == lo:
        return lo, hi, None
    ratio = (values[0] - lo) / (hi - lo)
    if ratio <= 0.25:
        label = "near 5y low"
    elif ratio >= 0.75:
        label = "near 5y high"
    else:
        label = "mid 5y range"
    return lo, hi, label


def _percentile_bounds(values: list[float]) -> tuple[float, float] | None:
    """Return (p25, p75) from ``values`` or None if too thin to be meaningful."""
    if len(values) < 4:
        return None
    sorted_vals = sorted(values)
    qs = statistics.quantiles(sorted_vals, n=4)  # returns 3 cut-points
    return qs[0], qs[2]


def _multiple_label(
    value: float | None,
    history_values: list[float],
    peer_median: float | None,
) -> str | None:
    """Return ``Premium`` / ``Inline`` / ``Discounted`` or None if insufficient context.

    Rule (per ticket scope A2):
      Premium    = value above own-history 75th pct OR value above peer median by >= 25%
      Discounted = value below own-history 25th pct OR value below peer median by >= 25%
      Inline     = within own-history IQR AND within +/- 15% of peer median
      Otherwise  = None (no label; caller suppresses the suffix)
    """
    if value is None or not math.isfinite(value):
        return None
    own_bounds = _percentile_bounds(history_values)
    own_premium = own_bounds is not None and value > own_bounds[1]
    own_discount = own_bounds is not None and value < own_bounds[0]
    own_inline = own_bounds is not None and own_bounds[0] <= value <= own_bounds[1]

    peer_premium = False
    peer_discount = False
    peer_inline = False
    if peer_median is not None and math.isfinite(peer_median) and peer_median != 0:
        pct = (value - peer_median) / abs(peer_median) * 100
        peer_premium = pct >= _PEER_PREMIUM_DELTA_PCT
        peer_discount = pct <= -_PEER_PREMIUM_DELTA_PCT
        peer_inline = abs(pct) <= _PEER_INLINE_DELTA_PCT

    if own_premium or peer_premium:
        return "Premium"
    if own_discount or peer_discount:
        return "Discounted"
    if own_inline and peer_inline:
        return "Inline"
    # When the two signals disagree (e.g. own-history says high, peer median
    # says cheap) or neither side has enough context, suppress the label.
    if own_inline and peer_median is None:
        return "Inline"
    if peer_inline and own_bounds is None:
        return "Inline"
    return None


def _valuation_label(
    value: float | None,
    history_values: list[float],
    thresholds: str = "",
    na_reason: str = "data unavailable",
    prior_value: float | None = None,
    peer_median: float | None = None,
) -> str:
    """Format a valuation multiple with history range, prior-period delta, and label.

    Canonical thresholds always appear in the output so the agent quotes them
    from the report rather than leaking them from prior knowledge (ADR-012).
    The trailing Premium / Inline / Discounted suffix (QNT-207) is appended
    when there is enough context (own-history IQR or peer median) to assign
    one.
    """
    if value is None or not math.isfinite(value):
        suffix = f"; {thresholds}" if thresholds else ""
        return f"N/M ({na_reason}{suffix})"
    parts: list[str] = []
    stats = _history_stats(history_values)
    if stats:
        lo, hi, pos = stats
        range_str = f"range {format_ratio(lo)}–{format_ratio(hi)} over last 5y"
        if pos is not None:
            range_str += f", {pos}"
        parts.append(range_str)
    if prior_value is not None and math.isfinite(prior_value):
        direction = (
            "expanding"
            if value > prior_value
            else "contracting"
            if value < prior_value
            else "steady"
        )
        parts.append(f"prior period {format_ratio(prior_value)}, {direction}")
    if thresholds:
        parts.append(thresholds)
    base = f"{format_ratio(value)} ({', '.join(parts)})" if parts else format_ratio(value)
    suffix = _multiple_label(value, history_values, peer_median)
    if suffix is None:
        return base
    return f"{base} — {suffix}"


def _pe_label(
    pe: float | None,
    eps: float | None,
    history_values: list[float],
    prior_value: float | None = None,
    peer_median: float | None = None,
) -> str:
    """Format P/E with 5y history range, prior-period delta, thresholds, and label."""
    return _valuation_label(
        pe,
        history_values,
        thresholds=_PE_THRESHOLDS,
        na_reason=pe_na_reason(eps),
        prior_value=prior_value,
        peer_median=peer_median,
    )


def _margin_line(value: float | None, prior: float | None, label: str) -> str:
    """Format a percentage metric with optional prior-period delta."""
    if value is None or not math.isfinite(value):
        return f"{label}: {format_pct(value)}"
    line = f"{label}: {format_pct(value)}"
    if prior is not None and math.isfinite(prior):
        direction = "expanding" if value > prior else "contracting" if value < prior else "steady"
        line += f" (prior period {format_pct(prior)}, {direction})"
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
    """Return up to 80 recent rows (all period_types) for ``ticker``, newest first.

    80 rows ≈ 5y of quarters (20) + 5y of annuals (5) + 5y of TTMs (20),
    with headroom for occasional same-date duplicates across period_types.
    """
    client = get_client()
    cols = ", ".join(_RATIO_COLUMNS)
    query = f"""
        SELECT {cols}
        FROM equity_derived.fundamental_summary FINAL
        WHERE ticker = %(ticker)s
        ORDER BY period_end DESC
        LIMIT 80
    """
    result = client.query(query, parameters={"ticker": ticker})
    return [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]


def _fetch_peer_medians(peers: list[str]) -> dict[str, float | None]:
    """Return median pe_ratio / ev_ebitda / price_to_sales across ``peers``.

    Uses argMax to get the latest quarterly snapshot per ticker, then computes
    medians in Python. Used by both the PEER CONTEXT section and the
    per-multiple Premium / Inline / Discounted label.
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
    medians: dict[str, float | None] | None,
    n_peers: int,
) -> list[str]:
    """Build ## PEER CONTEXT section lines from pre-computed peer medians."""
    lines: list[str] = ["## PEER CONTEXT"]

    if medians is None:
        n = n_peers
        noun = "peer" if n == 1 else "peers"
        lines.append(
            f"Sector median: N/A (insufficient peers in coverage -- "
            f"{sector}, {n} {noun} in coverage)"
        )
        return lines

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

    QNT-207 drops this from the rendered report body but keeps it as a Python
    callable for downstream consumers (thesis v2 may revisit). The named
    asymmetry labels stay so older consumers don't regress.

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


def _render_period_section(
    label: str,
    rows: list[dict[str, Any]],
    peer_medians: dict[str, float | None],
) -> list[str]:
    """Render the ## QUARTERLY / ## ANNUAL / ## TTM block.

    ``rows`` is the subset of fetched rows for one period_type, newest first.
    Empty rows render an inline N/M block under the section header so a
    partial dataset (e.g. no annual yet) doesn't 404 the whole report.
    """
    if not rows:
        return [
            f"## {label}",
            f"N/M (no {label.lower()} rows ingested for this ticker)",
        ]
    latest = rows[0]
    prior = rows[1] if len(rows) > 1 else None
    period_end: date = latest["period_end"]
    days_old = (date.today() - period_end).days

    def _p(key: str) -> Any:
        return prior[key] if prior else None

    def _hist(key: str) -> list[float]:
        return [r[key] for r in rows if r[key] is not None and math.isfinite(r[key])]

    pe_hist = _hist("pe_ratio")
    ev_hist = _hist("ev_ebitda")
    pb_hist = _hist("price_to_book")
    ps_hist = _hist("price_to_sales")

    scope = label.lower()
    return [
        f"## {label}",
        f"As of {period_end.isoformat()} ({scope}, {days_old} days old)",
        "",
        f"### {label} VALUATION",
        f"P/E ({scope}): "
        + _pe_label(
            latest["pe_ratio"],
            latest["eps"],
            pe_hist,
            _p("pe_ratio"),
            peer_medians.get("pe_ratio"),
        ),
        f"EV/EBITDA ({scope}): "
        + _valuation_label(
            latest["ev_ebitda"],
            ev_hist,
            prior_value=_p("ev_ebitda"),
            peer_median=peer_medians.get("ev_ebitda"),
        ),
        f"Price/Book ({scope}): "
        + _valuation_label(latest["price_to_book"], pb_hist, prior_value=_p("price_to_book")),
        f"Price/Sales ({scope}): "
        + _valuation_label(
            latest["price_to_sales"],
            ps_hist,
            prior_value=_p("price_to_sales"),
            peer_median=peer_medians.get("price_to_sales"),
        ),
        f"EPS ({scope}): {format_ratio(latest['eps'], na_reason='earnings unavailable')}",
        "",
        f"### {label} GROWTH (YoY)",
        _growth_label(latest["revenue_yoy_pct"], _p("revenue_yoy_pct"), f"Revenue ({scope})"),
        _growth_label(
            latest["net_income_yoy_pct"], _p("net_income_yoy_pct"), f"Net income ({scope})"
        ),
        _growth_label(latest["fcf_yoy_pct"], _p("fcf_yoy_pct"), f"Free cash flow ({scope})"),
        _GROWTH_REFERENCE,
        "",
        f"### {label} PROFITABILITY",
        _margin_line(latest["gross_margin_pct"], _p("gross_margin_pct"), f"Gross margin ({scope})"),
        _margin_line(latest["net_margin_pct"], _p("net_margin_pct"), f"Net margin ({scope})"),
        _margin_line(latest["roe"], _p("roe"), f"ROE ({scope})"),
        _margin_line(latest["roa"], _p("roa"), f"ROA ({scope})"),
        _PROFITABILITY_REFERENCE,
        "",
        f"### {label} CASH & LEVERAGE",
        f"FCF yield ({scope}): {format_pct(latest['fcf_yield'])}",
        f"Debt/Equity ({scope}): {format_ratio(latest['debt_to_equity'])}",
        f"Current ratio ({scope}): {format_ratio(latest['current_ratio'])}",
    ]


def build_fundamental_report(ticker: str) -> str:
    """Build a quarterly + annual + TTM fundamental report for ``ticker``."""
    if ticker not in TICKERS:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")

    rows = _fetch_rows(ticker)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No fundamental data for {ticker}")

    by_period: dict[str, list[dict[str, Any]]] = {pt: [] for _, pt in _PERIODS}
    for row in rows:
        pt = row.get("period_type")
        if pt in by_period:
            by_period[pt].append(row)

    quarterly_rows = by_period["quarterly"]
    if not quarterly_rows:
        raise HTTPException(status_code=404, detail=f"No quarterly fundamentals for {ticker}")
    latest_q = quarterly_rows[0]

    meta = TICKER_METADATA.get(ticker, {})
    period_end: date = latest_q["period_end"]
    sector = str(meta.get("sector", "Unknown sector"))
    industry = str(meta.get("industry", "Unknown industry"))
    days_old = (date.today() - period_end).days

    peers = [
        t for t in TICKERS if t != ticker and TICKER_METADATA.get(t, {}).get("sector") == sector
    ]
    n_peers = len(peers)
    peer_medians: dict[str, float | None]
    if n_peers >= _MIN_PEERS_FOR_MEDIAN:
        peer_medians = _fetch_peer_medians(peers)
        peer_medians_for_context: dict[str, float | None] | None = peer_medians
    else:
        peer_medians = {"pe_ratio": None, "ev_ebitda": None, "price_to_sales": None}
        peer_medians_for_context = None
    peer_lines = _peer_context_lines(
        ticker,
        sector,
        latest_q,
        peer_medians_for_context,
        n_peers,
    )

    lines = [
        f"# FUNDAMENTAL REPORT — {ticker}",
        f"As of {period_end.isoformat()} (quarterly, {days_old} days old) — {sector}, {industry}",
        _PERIOD_DISCLAIMER,
        _VALUATION_LABEL_RULE,
        "",
        *peer_lines,
        "",
    ]
    for label, pt in _PERIODS:
        lines.extend(_render_period_section(label, by_period[pt], peer_medians))
        lines.append("")
    lines.append(f"Data: latest available quarterly fundamentals as of {period_end.isoformat()}.")
    return "\n".join(lines)
