"""Central formatting helpers for report templates.

All null/N/M handling for report endpoints goes through these helpers — no
endpoint-specific null handling. Every null value renders as ``N/M (<reason>)``
so reports never contain a blank field, the literal string "None", or a
misleading 0 where data is missing.

See QNT-69 (report template design) and QNT-87 (|EPS| < $0.10 → P/E = N/M).
"""

from __future__ import annotations

import math


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
    precision: int = 2,
    na_reason: str = "data unavailable",
) -> str:
    """Format a signed percentage with explicit ``+``/``-`` sign."""
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
    """Format a USD amount with thousands separators."""
    if _is_missing(value):
        return f"N/M ({na_reason})"
    assert value is not None
    return f"${value:,.{precision}f}"


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


__all__ = [
    "format_currency",
    "format_pct",
    "format_ratio",
    "format_signed_pct",
    "pe_na_reason",
]
