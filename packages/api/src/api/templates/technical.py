"""Technical report template — the canonical report shape for QNT-69.

Produces a structured, section-based text report over pre-computed daily
indicators. No arithmetic beyond trivial presentation math (daily change %,
threshold classifications) — see ADR-003.

Section shape is the template other reports parameterise over:
    # HEADER
    ticker, sector, as-of date

    ## PRICE ACTION
    close, daily change, trend vs SMA-20 / SMA-50

    ## MOMENTUM
    RSI with threshold context, MACD with signal-cross context

    ## VOLATILITY
    Bollinger Band position

    ## SIGNAL
    explicit bullish / bearish / neutral verdict
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import HTTPException
from shared.tickers import TICKER_METADATA, TICKERS

from api.clickhouse import get_client
from api.formatters import format_ratio, format_signed_pct

_INDICATOR_COLUMNS = (
    "date",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "sma_20",
    "sma_50",
    "bb_upper",
    "bb_middle",
    "bb_lower",
)


def _rsi_label(rsi: float | None) -> str:
    if rsi is None:
        return "N/M (insufficient history)"
    if rsi >= 70:
        return f"{rsi:.1f} — overbought (above 70 threshold)"
    if rsi >= 65:
        return f"{rsi:.1f} — approaching overbought"
    if rsi <= 30:
        return f"{rsi:.1f} — oversold (below 30 threshold)"
    if rsi <= 35:
        return f"{rsi:.1f} — approaching oversold"
    return f"{rsi:.1f} — neutral (30-70 range)"


def _macd_label(macd: float | None, signal: float | None, hist: float | None) -> str:
    if macd is None or signal is None or hist is None:
        return "N/M (insufficient history)"
    cross = "above" if macd > signal else "below"
    momentum = "expanding" if hist >= 0 else "contracting"
    bias = (
        "bullish"
        if macd > 0 and macd > signal
        else "bearish"
        if macd < 0 and macd < signal
        else "mixed"
    )
    return (
        f"MACD {macd:+.2f} {cross} signal {signal:+.2f} "
        f"(histogram {hist:+.2f}, {momentum}) — {bias}"
    )


def _trend_label(close: float, sma_20: float | None, sma_50: float | None) -> str:
    parts: list[str] = []
    if sma_50 is not None:
        diff_pct = (close - sma_50) / sma_50 * 100
        direction = "above" if close >= sma_50 else "below"
        label = "uptrend" if close >= sma_50 else "downtrend"
        parts.append(
            f"close {direction} SMA-50 ({format_ratio(sma_50, precision=2)}) "
            f"by {diff_pct:+.2f}% — {label}"
        )
    else:
        parts.append("SMA-50: N/M (insufficient history)")
    if sma_20 is not None:
        diff_pct = (close - sma_20) / sma_20 * 100
        direction = "above" if close >= sma_20 else "below"
        parts.append(
            f"close {direction} SMA-20 ({format_ratio(sma_20, precision=2)}) by {diff_pct:+.2f}%"
        )
    return "; ".join(parts)


def _bb_label(
    close: float,
    upper: float | None,
    middle: float | None,
    lower: float | None,
) -> str:
    if upper is None or middle is None or lower is None:
        return "N/M (insufficient history)"
    width_pct = (upper - lower) / middle * 100 if middle else 0.0
    if close >= upper:
        zone = "at/above upper band — stretched, mean-reversion risk"
    elif close <= lower:
        zone = "at/below lower band — stretched, mean-reversion risk"
    elif close >= middle:
        zone = "upper half — trending with the band"
    else:
        zone = "lower half — trending with the band"
    return (
        f"bands {format_ratio(lower, precision=2)} / "
        f"{format_ratio(middle, precision=2)} / "
        f"{format_ratio(upper, precision=2)} "
        f"(width {width_pct:.1f}% of middle) — {zone}"
    )


def _signal_verdict(
    close: float,
    sma_50: float | None,
    rsi: float | None,
    macd: float | None,
    macd_signal: float | None,
) -> str:
    votes: list[str] = []
    if sma_50 is not None:
        votes.append("bullish" if close >= sma_50 else "bearish")
    if rsi is not None:
        if rsi >= 70 or rsi <= 30:
            votes.append("neutral")  # extremes cut both ways — mean-reversion
        elif rsi >= 50:
            votes.append("bullish")
        else:
            votes.append("bearish")
    if macd is not None and macd_signal is not None:
        votes.append("bullish" if macd > macd_signal else "bearish")
    if not votes:
        return "N/M (insufficient history across all indicators)"
    bull = votes.count("bullish")
    bear = votes.count("bearish")
    if bull > bear and bull >= 2:
        return f"BULLISH ({bull}/{len(votes)} indicators agree)"
    if bear > bull and bear >= 2:
        return f"BEARISH ({bear}/{len(votes)} indicators agree)"
    return f"NEUTRAL (mixed: {bull} bullish, {bear} bearish, {len(votes) - bull - bear} neutral)"


def _rsi_trajectory(current: float | None, prior: float | None) -> str:
    if current is None or prior is None:
        return ""
    delta = current - prior
    direction = "up" if delta >= 0 else "down"
    return f" (prior session {prior:.1f}, {direction} {abs(delta):.1f})"


def _fetch_rows(ticker: str) -> list[dict[str, Any]]:
    """Return latest two indicator rows joined with OHLCV close, newest first."""
    client = get_client()
    cols = ", ".join(f"i.{c}" for c in _INDICATOR_COLUMNS)
    query = f"""
        SELECT {cols}, o.close AS close, o.volume AS volume
        FROM equity_derived.technical_indicators_daily AS i FINAL
        INNER JOIN equity_raw.ohlcv_raw AS o FINAL
          ON i.ticker = o.ticker AND i.date = o.date
        WHERE i.ticker = %(ticker)s
        ORDER BY i.date DESC
        LIMIT 2
    """
    result = client.query(query, parameters={"ticker": ticker})
    return [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]


def build_technical_report(ticker: str) -> str:
    """Build a human-readable technical analysis report for ``ticker``.

    Raises ``HTTPException(404)`` if the ticker is unknown or has no indicator
    rows in ClickHouse yet.
    """
    if ticker not in TICKERS:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")

    rows = _fetch_rows(ticker)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No technical data for {ticker}")

    latest = rows[0]
    prior = rows[1] if len(rows) > 1 else None
    meta = TICKER_METADATA.get(ticker, {})
    as_of: date = latest["date"]
    close: float = float(latest["close"])

    daily_change_pct: float | None = None
    if prior is not None:
        prior_close = float(prior["close"])
        if prior_close:
            daily_change_pct = (close - prior_close) / prior_close * 100

    rsi = latest["rsi_14"]
    macd = latest["macd"]
    macd_signal = latest["macd_signal"]
    macd_hist = latest["macd_hist"]
    sma_20 = latest["sma_20"]
    sma_50 = latest["sma_50"]
    bb_upper = latest["bb_upper"]
    bb_middle = latest["bb_middle"]
    bb_lower = latest["bb_lower"]
    rsi_trend = _rsi_trajectory(rsi, prior["rsi_14"] if prior else None)

    lines = [
        f"# TECHNICAL REPORT — {ticker}",
        f"As of {as_of.isoformat()} ({meta.get('sector', 'Unknown sector')}, "
        f"{meta.get('industry', 'Unknown industry')})",
        "",
        "## PRICE ACTION",
        f"Close: {format_ratio(close, precision=2)} "
        f"({format_signed_pct(daily_change_pct, na_reason='no prior session')} daily)",
        f"Trend: {_trend_label(close, sma_20, sma_50)}",
        "",
        "## MOMENTUM",
        f"RSI-14: {_rsi_label(rsi)}{rsi_trend}",
        f"MACD(12/26/9): {_macd_label(macd, macd_signal, macd_hist)}",
        "",
        "## VOLATILITY",
        f"Bollinger(20,2): {_bb_label(close, bb_upper, bb_middle, bb_lower)}",
        "",
        "## SIGNAL",
        _signal_verdict(close, sma_50, rsi, macd, macd_signal),
    ]
    return "\n".join(lines)
