"""Central formatting helpers for report templates.

All null/N/M handling for report endpoints goes through these helpers — no
endpoint-specific null handling. Every null value renders as ``N/M (<reason>)``
so reports never contain a blank field, the literal string "None", or a
misleading 0 where data is missing.

See QNT-69 (report template design) and QNT-87 (|EPS| < $0.10 → P/E = N/M).
"""

from __future__ import annotations

import math
from datetime import date


def _is_missing(value: float | None) -> bool:
    return value is None or not math.isfinite(value)


def format_ratio(
    value: float | None,
    *,
    precision: int = 2,
    suffix: str = "",
    na_reason: str = "data unavailable",
) -> str:
    """Format a numeric ratio. Returns ``N/M (<na_reason>)`` when missing.

    A ratio is "missing" when the value is None or non-finite (NaN, ±inf).
    Callers pass a domain-specific ``na_reason`` so the reader always learns
    *why* a cell is not meaningful (e.g. ``"near-zero earnings"`` for P/E).
    """
    if _is_missing(value):
        return f"N/M ({na_reason})"
    assert value is not None
    return f"{value:.{precision}f}{suffix}"


def format_pct(
    value: float | None,
    *,
    precision: int = 1,
    na_reason: str = "data unavailable",
) -> str:
    """Format a percentage value (value is already in percent units)."""
    return format_ratio(value, precision=precision, suffix="%", na_reason=na_reason)


def format_signed_pct(
    value: float | None,
    *,
    precision: int = 1,
    na_reason: str = "data unavailable",
) -> str:
    """Format a signed percentage with explicit ``+``/``-`` sign.

    Percentages render at one decimal (QNT-361): growth rates are quoted at
    one decimal by finance convention, and the narrator repeats report values
    verbatim — a 2dp report value invited "spoken" 1dp rounding that the
    grounding check correctly flagged as drift.
    """
    if _is_missing(value):
        return f"N/M ({na_reason})"
    assert value is not None
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.{precision}f}%"


def format_currency(
    value: float | None,
    *,
    precision: int = 2,
    na_reason: str = "data unavailable",
) -> str:
    """Format a USD amount with thousands separators.

    The sign leads the currency symbol (``-$500`` not ``$-500``) — the
    conventional accounting placement, and the only sensible reading now that
    format_currency is used on values that can legitimately be negative
    (net income / FCF in the SCALE block, QNT-354).
    """
    if _is_missing(value):
        return f"N/M ({na_reason})"
    assert value is not None
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.{precision}f}"


def format_currency_compact(
    value: float | None,
    *,
    na_reason: str = "data unavailable",
) -> str:
    """Scale-suffixed USD amount at one decimal ($129.2B, -$1.5B, $4.7T).

    QNT-361 follow-up: the SCALE block printed raw dollars
    ($129,174,000,000) which the narrator inevitably speaks as "$129.2B" —
    genuine rounding the grounding check correctly flags. The report layer
    owns rounding, so the report prints the speakable form. One decimal
    mirrors the percentage convention; the grounding checker's trailing-zero
    canonicalisation makes a printed "$129.0B" and a spoken "$129B" compare
    equal. Values under $1M fall back to the exact comma-separated figure.
    """
    if _is_missing(value):
        return f"N/M ({na_reason})"
    assert value is not None
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if magnitude >= threshold:
            return f"{sign}${magnitude / threshold:.1f}{suffix}"
    return f"{sign}${magnitude:,.0f}"


def pe_na_reason(eps: float | None) -> str:
    """Explain why a P/E ratio is N/M given the EPS context.

    Mirrors the nulling rule from compute_fundamental_ratios (QNT-87):
    |EPS| < $0.10 → P/E is nulled as "near-zero earnings".
    """
    if _is_missing(eps):
        return "EPS unavailable"
    assert eps is not None
    if abs(eps) < 0.10:
        return "near-zero earnings"
    return "data unavailable"


def format_as_of_footer(value: date | None) -> str:
    """Machine-parseable as-of date line, appended to every report body.

    Distinct from each report's human-readable "As of ..." header prose
    (which varies in wording per report kind) -- this single fixed-format
    line lets any consumer (the agent's freshness read, tests) pull a
    report's data date with one regex instead of parsing per-kind header
    prose (QNT-299).
    """
    if value is None:
        return "AS_OF: N/M (no dated data available)"
    return f"AS_OF: {value.isoformat()}"


__all__ = [
    "format_as_of_footer",
    "format_currency",
    "format_currency_compact",
    "format_pct",
    "format_ratio",
    "format_signed_pct",
    "pe_na_reason",
]
