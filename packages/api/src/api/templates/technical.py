"""Technical report template -- daily + weekly + monthly timeframes (QNT-207).

Produces a structured, section-based text report over pre-computed indicators
at three timeframes. No arithmetic beyond trivial presentation math (period
change %, threshold classifications) -- see ADR-003.

Section shape:
    # HEADER
    ticker, sector, as-of date
    disclaimer (which timeframe captures what)

    ## DAILY
    ### PRICE ACTION / ### MOMENTUM / ### VOLATILITY / ### TREND

    ## WEEKLY
    (same sub-sections)

    ## MONTHLY
    (same sub-sections)

TREND replaces the previous per-timeframe SIGNAL footer. It carries an
explicit Uptrend / Sideways / Downtrend label derived from close vs SMA-50,
SMA-20 vs SMA-50, and close slope across the most recent two bars. The
derivation is embedded verbatim in the report text per ADR-012 so the agent
quotes it from the corpus instead of leaking TA prior knowledge.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import HTTPException
from shared.tickers import TICKER_METADATA, TICKERS

from api.clickhouse import get_client
from api.formatters import format_ratio, format_signed_pct

_INDICATOR_COLUMNS = (
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

# (indicators_table, ohlcv_table, date_col) per timeframe.
_TIMEFRAMES: tuple[tuple[str, str, str, str], ...] = (
    ("DAILY", "equity_derived.technical_indicators_daily", "equity_raw.ohlcv_raw", "date"),
    (
        "WEEKLY",
        "equity_derived.technical_indicators_weekly",
        "equity_derived.ohlcv_weekly",
        "week_start",
    ),
    (
        "MONTHLY",
        "equity_derived.technical_indicators_monthly",
        "equity_derived.ohlcv_monthly",
        "month_start",
    ),
)

_DISCLAIMER = (
    "Daily captures intraday-to-week swings; weekly captures multi-week regime; "
    "monthly captures cycle-level posture."
)

# Surfaced verbatim in the report header (ADR-012) so the agent quotes the
# rule from the corpus instead of leaking trend taxonomy from prior knowledge.
_TREND_LABEL_RULE = (
    "Trend label: Uptrend = close above SMA-50 AND SMA-20 above SMA-50 AND positive slope; "
    "Downtrend = close below SMA-50 AND SMA-20 below SMA-50 AND negative slope; "
    "Sideways = otherwise."
)


def _rsi_label(rsi: float | None) -> str:
    """Format an RSI-14 reading with bucket context.

    Always prints the canonical 70 (overbought) and 30 (oversold) thresholds
    in every non-N/M branch so the agent's synthesize step can quote them
    verbatim instead of reaching for them as TA prior knowledge (QNT-136 /
    ADR-012). Uses Unicode em-dash / ≥ / ≤ to match the in-corpus convention
    every prior snapshot and test asserts on.
    """
    if rsi is None:
        return "N/M (insufficient history; overbought ≥ 70, oversold ≤ 30)"
    if rsi >= 70:
        return f"{rsi:.1f} — overbought (above 70 threshold; oversold ≤ 30)"
    if rsi >= 65:
        return f"{rsi:.1f} — approaching overbought (70 threshold; oversold ≤ 30)"
    if rsi <= 30:
        return f"{rsi:.1f} — oversold (below 30 threshold; overbought ≥ 70)"
    if rsi <= 35:
        return f"{rsi:.1f} — approaching oversold (30 threshold; overbought ≥ 70)"
    return f"{rsi:.1f} — neutral (overbought ≥ 70, oversold ≤ 30)"


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


def _price_action_label(close: float, sma_20: float | None, sma_50: float | None) -> str:
    parts: list[str] = []
    if sma_50 is not None:
        diff_pct = (close - sma_50) / sma_50 * 100
        direction = "above" if close >= sma_50 else "below"
        parts.append(
            f"close {direction} SMA-50 ({format_ratio(sma_50, precision=2)}) by {diff_pct:+.2f}%"
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


def _trend_label(
    close: float,
    prior_close: float | None,
    sma_20: float | None,
    sma_50: float | None,
) -> str:
    """Return ``Uptrend`` / ``Sideways`` / ``Downtrend`` plus the derivation.

    Rule (ADR-012, embedded verbatim in the report body):
      Uptrend:   close > SMA-50 AND SMA-20 > SMA-50 AND positive slope over last 2 bars
      Downtrend: close < SMA-50 AND SMA-20 < SMA-50 AND negative slope
      Sideways:  otherwise
    """
    if sma_20 is None or sma_50 is None or prior_close is None:
        return "N/M (insufficient history; need SMA-20, SMA-50, and a prior bar)"
    slope = close - prior_close
    if close > sma_50 and sma_20 > sma_50 and slope > 0:
        label = "Uptrend"
    elif close < sma_50 and sma_20 < sma_50 and slope < 0:
        label = "Downtrend"
    else:
        label = "Sideways"
    slope_word = "positive" if slope > 0 else "negative" if slope < 0 else "flat"
    sma_relation = "above" if sma_20 > sma_50 else "below" if sma_20 < sma_50 else "level with"
    close_relation = "above" if close > sma_50 else "below" if close < sma_50 else "at"
    return (
        f"{label} (close {close_relation} SMA-50, SMA-20 {sma_relation} SMA-50, "
        f"{slope_word} slope vs prior bar)"
    )


def _rsi_trajectory(current: float | None, prior: float | None) -> str:
    if current is None or prior is None:
        return ""
    delta = current - prior
    direction = "up" if delta >= 0 else "down"
    return f" (prior period {prior:.1f}, {direction} {abs(delta):.1f})"


def _fetch_rows(
    ticker: str,
    indicators_table: str,
    ohlcv_table: str,
    date_col: str,
) -> list[dict[str, Any]]:
    """Return latest two indicator rows joined with OHLCV close, newest first."""
    client = get_client()
    cols = ", ".join(f"i.{c}" for c in _INDICATOR_COLUMNS)
    query = f"""
        SELECT i.{date_col} AS as_of, {cols}, o.close AS close, o.volume AS volume
        FROM {indicators_table} AS i FINAL
        INNER JOIN {ohlcv_table} AS o FINAL
          ON i.ticker = o.ticker AND i.{date_col} = o.{date_col}
        WHERE i.ticker = %(ticker)s
        ORDER BY i.{date_col} DESC
        LIMIT 2
    """
    result = client.query(query, parameters={"ticker": ticker})
    return [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]


def _render_section(label: str, rows: list[dict[str, Any]]) -> list[str]:
    """Render one timeframe section. Empty rows -> N/M block under the header."""
    if not rows:
        return [
            f"## {label}",
            f"N/M (no {label.lower()} indicator data ingested for this ticker)",
        ]
    latest = rows[0]
    prior = rows[1] if len(rows) > 1 else None
    as_of: date = latest["as_of"]
    close: float = float(latest["close"])

    period_change_pct: float | None = None
    if prior is not None:
        prior_close = float(prior["close"])
        if prior_close:
            period_change_pct = (close - prior_close) / prior_close * 100

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
    prior_close_val = float(prior["close"]) if prior else None

    return [
        f"## {label}",
        f"As of {as_of.isoformat()}",
        "",
        "### PRICE ACTION",
        f"Close: {format_ratio(close, precision=2)} "
        f"({format_signed_pct(period_change_pct, na_reason='no prior period')} vs prior period)",
        f"Trend: {_price_action_label(close, sma_20, sma_50)}",
        "",
        "### MOMENTUM",
        f"RSI-14: {_rsi_label(rsi)}{rsi_trend}",
        f"MACD(12/26/9): {_macd_label(macd, macd_signal, macd_hist)}",
        "",
        "### VOLATILITY",
        f"Bollinger(20,2): {_bb_label(close, bb_upper, bb_middle, bb_lower)}",
        "",
        "### TREND",
        _trend_label(close, prior_close_val, sma_20, sma_50),
    ]


def build_technical_report(ticker: str) -> str:
    """Build a daily + weekly + monthly technical report for ``ticker``.

    Raises ``HTTPException(404)`` if the ticker is unknown or if the DAILY
    timeframe has no rows. Weekly/monthly empty sections render as N/M
    in-place rather than 404-ing the whole report, since they trail daily by
    days or weeks of ingest.
    """
    if ticker not in TICKERS:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")

    rows_by_label: dict[str, list[dict[str, Any]]] = {}
    for label, ind_table, ohlcv_table, date_col in _TIMEFRAMES:
        rows_by_label[label] = _fetch_rows(ticker, ind_table, ohlcv_table, date_col)

    daily_rows = rows_by_label["DAILY"]
    if not daily_rows:
        raise HTTPException(status_code=404, detail=f"No technical data for {ticker}")

    meta = TICKER_METADATA.get(ticker, {})
    as_of: date = daily_rows[0]["as_of"]
    days_old = (date.today() - as_of).days

    lines = [
        f"# TECHNICAL REPORT — {ticker}",
        f"As of {as_of.isoformat()} (daily, {days_old} days old) — "
        f"{meta.get('sector', 'Unknown sector')}, {meta.get('industry', 'Unknown industry')}",
        _DISCLAIMER,
        _TREND_LABEL_RULE,
        "",
    ]
    for label, _, _, _ in _TIMEFRAMES:
        lines.extend(_render_section(label, rows_by_label[label]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
