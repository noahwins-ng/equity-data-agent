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

from collections import Counter
from datetime import date
from typing import Any

from fastapi import HTTPException
from shared.tickers import TICKER_METADATA, TICKERS

from api.clickhouse import get_client
from api.formatters import format_as_of_footer, format_ratio, format_signed_pct

_INDICATOR_COLUMNS = (
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "sma_20",
    "sma_50",
    "sma_200",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "bb_pct_b",
    "adx_14",
    "atr_14",
    "macd_bullish_cross",
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
    pct_b: float | None = None,
) -> str:
    """Bollinger band read. ``pct_b`` (passed on the DAILY section only) folds
    the precise band position onto this line rather than a separate %B line, so
    the two-ticker comparison prompt stays under its synthesis token budget."""
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
    line = (
        f"bands {format_ratio(lower, precision=2)} / "
        f"{format_ratio(middle, precision=2)} / "
        f"{format_ratio(upper, precision=2)} "
        f"(width {width_pct:.1f}% of middle) — {zone}"
    )
    if pct_b is not None:
        line += f"; %B {pct_b:.2f} (0 = lower band, 1 = upper band)"
    return line


def _sma_200_label(close: float, sma_50: float | None, sma_200: float | None) -> str:
    """Close vs the 200-day plus the 50/200 golden-/death-cross regime word.

    The 200-day is the most-quoted MA on the street; printing the close's
    distance from it and the 50/200 relationship verbatim (ADR-012) lets the
    agent quote "above the 200-day" / "golden-cross regime" instead of reaching
    for the taxonomy from prior knowledge.
    """
    if sma_200 is None:
        return "N/M (insufficient history; needs 200 bars)"
    diff_pct = (close - sma_200) / sma_200 * 100
    direction = "above" if close >= sma_200 else "below"
    line = f"close {direction} SMA-200 ({format_ratio(sma_200, precision=2)}) {diff_pct:+.2f}%"
    if sma_50 is not None:
        line += f"; 50/200 {'golden cross' if sma_50 >= sma_200 else 'death cross'}"
    return line


def _adx_label(adx: float | None) -> str:
    """ADX-14 trend *strength* with canonical thresholds printed in-body.

    ADX qualifies the TREND label's conviction: a strong uptrend and a drift
    both read "Uptrend" without it. The 25-trending / 20-weak thresholds are
    printed on every branch (ADR-012) so the agent quotes rather than recalls.
    """
    if adx is None:
        return "N/M (insufficient history; ≥ 25 trending, < 20 weak)"
    if adx >= 25:
        strength = "trending"
    elif adx < 20:
        strength = "weak/rangebound"
    else:
        strength = "developing"
    return f"{adx:.1f} — {strength} (≥ 25 trending, < 20 weak)"


def _atr_label(atr: float | None, close: float) -> str:
    """ATR-14 (average true range) in price terms plus its % of close."""
    if atr is None:
        return "N/M (insufficient history)"
    pct = atr / close * 100 if close else 0.0
    return f"{format_ratio(atr, precision=2)} ({pct:.1f}% of close)"


def _macd_cross_label(flag: int | None) -> str:
    """macd_bullish_cross event flag — a ready-made bullish-cross signal."""
    if flag is None:
        return "N/M (insufficient history)"
    if int(flag) == 1:
        return "yes — crossed above signal on the latest bar"
    return "no"


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


def _fetch_price_context(ticker: str, anchor: date) -> dict[str, Any] | None:
    """Pre-compute the daily 52-week range, window-return anchors, and 20-day
    average volume for one ticker, all in ClickHouse (ADR-003: the math lives in
    SQL, never the agent).

    Everything is measured relative to ``anchor`` (the DAILY section's as-of
    bar) rather than ``today()`` so the windows line up with the report's shown
    close even when ingest trails by a few days. Window returns anchor on
    ``adj_close`` (split-safe -- only the resulting % is rendered, never the base
    price); the 52-week high/low use raw ``high``/``low`` so they match a quote
    page and the raw close shown alongside them. Conditional aggregates return
    0.0 when a window has no history (e.g. < 1y of bars); the render helpers
    treat that 0-sentinel as N/M.
    """
    client = get_client()
    # clickhouse_connect substitutes %(anchor)s as a quoted string literal;
    # toDate() casts it so the date functions (toYear/subtractYears/subtractMonths)
    # get a Date rather than a String (ILLEGAL_TYPE_OF_ARGUMENT otherwise).
    query = """
        SELECT
            maxIf(high, date > subtractYears(toDate(%(anchor)s), 1)) AS high_52w,
            minIf(low, date > subtractYears(toDate(%(anchor)s), 1)) AS low_52w,
            argMax(adj_close, date) AS adj_now,
            argMaxIf(adj_close, date, date <= subtractMonths(toDate(%(anchor)s), 1)) AS adj_1m,
            argMaxIf(adj_close, date, date <= subtractMonths(toDate(%(anchor)s), 3)) AS adj_3m,
            argMaxIf(adj_close, date, date <= subtractYears(toDate(%(anchor)s), 1)) AS adj_1y,
            argMaxIf(adj_close, date, toYear(date) < toYear(toDate(%(anchor)s))) AS adj_ytd,
            (
                SELECT avg(volume)
                FROM (
                    SELECT volume
                    FROM equity_raw.ohlcv_raw FINAL
                    WHERE ticker = %(ticker)s AND date <= toDate(%(anchor)s)
                    ORDER BY date DESC
                    LIMIT 20
                )
            ) AS avg_volume_20
        FROM equity_raw.ohlcv_raw FINAL
        WHERE ticker = %(ticker)s AND date <= toDate(%(anchor)s)
    """
    result = client.query(query, parameters={"ticker": ticker, "anchor": anchor})
    if not result.result_rows:
        return None
    return dict(zip(result.column_names, result.result_rows[0], strict=True))


def _return_pct(now: float | None, then: float | None) -> float | None:
    """Trivial window-return presentation math -- same boundary as the existing
    one-bar ``period_change_pct``. Missing/zero anchor -> None (rendered N/M)."""
    if now is None or then is None or not then:
        return None
    return (now - then) / then * 100


def _range_52w_line(close: float, ctx: dict[str, Any] | None) -> str:
    if ctx is None:
        return "N/M (52-week range unavailable)"
    high = ctx.get("high_52w")
    low = ctx.get("low_52w")
    if not high or not low:
        return "N/M (insufficient 52-week history)"
    from_high = (close - high) / high * 100
    return (
        f"52-week range {format_ratio(low, precision=2)} - {format_ratio(high, precision=2)}; "
        f"close {from_high:+.2f}% from the 52-week high"
    )


def _performance_line(ctx: dict[str, Any] | None) -> str:
    if ctx is None:
        return "N/M (price history unavailable)"
    now = ctx.get("adj_now")
    windows = (("1m", "adj_1m"), ("3m", "adj_3m"), ("YTD", "adj_ytd"), ("1y", "adj_1y"))
    parts = [
        f"{label} "
        f"{format_signed_pct(_return_pct(now, ctx.get(key)), na_reason='insufficient history')}"
        for label, key in windows
    ]
    return "; ".join(parts)


def _volume_line(volume: float | None, ctx: dict[str, Any] | None) -> str:
    if volume is None:
        return "N/M (volume unavailable)"
    shares = int(volume)
    avg = ctx.get("avg_volume_20") if ctx else None
    if not avg:
        return f"{shares:,} shares (20-day average unavailable)"
    ratio = volume / avg
    return f"{shares:,} shares - {ratio:.2f}x the 20-day average"


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


def _render_section(
    label: str,
    rows: list[dict[str, Any]],
    price_ctx: dict[str, Any] | None = None,
) -> list[str]:
    """Render one timeframe section. Empty rows -> N/M block under the header.

    ``price_ctx`` (52-week range, window returns, 20-day avg volume) is supplied
    for the DAILY section only -- those anchors are a daily-bar concept computed
    off ``equity_raw.ohlcv_raw`` -- and is None for weekly/monthly. Its presence
    also gates the widened indicator lines (SMA-200, %B, ATR-14, ADX-14,
    MACD-cross): those render on DAILY only. The weekly/monthly sections stay at
    their baseline size so a two-ticker comparison prompt -- which folds two full
    technical reports -- keeps its synthesis output under the QNT-351 1500-token
    cap (measured: rendering the extras on all three timeframes fail-closed the
    comparison card to its deterministic fallback). SMA-200 is a daily concept
    anyway (200 weekly/monthly bars exceed the backfill, so it was N/M there).
    """
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
    sma_200 = latest["sma_200"]
    bb_upper = latest["bb_upper"]
    bb_middle = latest["bb_middle"]
    bb_lower = latest["bb_lower"]
    bb_pct_b = latest["bb_pct_b"]
    adx = latest["adx_14"]
    atr = latest["atr_14"]
    macd_cross = latest["macd_bullish_cross"]
    volume = latest.get("volume")
    rsi_trend = _rsi_trajectory(rsi, prior["rsi_14"] if prior else None)
    prior_close_val = float(prior["close"]) if prior else None

    scope = label.lower()
    # The widened indicator extras (SMA-200, %B, ATR-14, ADX-14, MACD-cross) and
    # the price-context lines render on DAILY only; weekly/monthly stay baseline.
    daily = label == "DAILY"

    price_action = [
        f"### {label} PRICE ACTION",
        f"Close ({scope}): {format_ratio(close, precision=2)} "
        f"({format_signed_pct(period_change_pct, na_reason='no prior period')} vs prior period)",
        f"Trend ({scope}): {_price_action_label(close, sma_20, sma_50)}",
    ]
    if daily:
        price_action += [
            f"SMA-200 ({scope}): {_sma_200_label(close, sma_50, sma_200)}",
            f"52-week ({scope}): {_range_52w_line(close, price_ctx)}",
            f"Performance ({scope}): {_performance_line(price_ctx)}",
            f"Volume ({scope}): {_volume_line(volume, price_ctx)}",
        ]

    momentum = [
        f"### {label} MOMENTUM",
        f"RSI-14 ({scope}): {_rsi_label(rsi)}{rsi_trend}",
        f"MACD(12/26/9) ({scope}): {_macd_label(macd, macd_signal, macd_hist)}",
    ]
    if daily:
        momentum.append(f"MACD bullish cross ({scope}): {_macd_cross_label(macd_cross)}")

    volatility = [
        f"### {label} VOLATILITY",
        # %B folds onto the Bollinger line on DAILY (bb_pct_b); ATR-14 is its own
        # short line. Weekly/monthly render the baseline Bollinger line only.
        f"Bollinger(20,2) ({scope}): "
        f"{_bb_label(close, bb_upper, bb_middle, bb_lower, bb_pct_b if daily else None)}",
    ]
    if daily:
        volatility.append(f"ATR-14 ({scope}): {_atr_label(atr, close)}")

    trend = [
        f"### {label} TREND",
        _trend_label(close, prior_close_val, sma_20, sma_50),
    ]
    if daily:
        trend.append(f"ADX-14 ({scope}): {_adx_label(adx)}")

    return [
        f"## {label}",
        f"As of {as_of.isoformat()} ({scope})",
        "",
        *price_action,
        "",
        *momentum,
        "",
        *volatility,
        "",
        *trend,
    ]


# QNT-224 follow-up: timeframe -> (indicators_table, ohlcv_table, date_col),
# read off the report's own _TIMEFRAMES so the lean trend agrees with the
# report section of the same scope verbatim.
_TREND_TIMEFRAMES: dict[str, tuple[str, str, str]] = {
    label.lower(): (ind, ohlcv, date_col) for label, ind, ohlcv, date_col in _TIMEFRAMES
}


def compute_trend_label(ticker: str, timeframe: str = "daily") -> str | None:
    """QNT-224 follow-up: the Uptrend / Sideways / Downtrend word the technical
    report derives for ``timeframe`` (``"daily"`` or ``"weekly"``), computed
    standalone for the lean N-way comparison.

    Reuses the report's exact path -- ``_fetch_rows`` (latest two bars of the
    timeframe) + ``_trend_label`` -- then strips the parenthetical derivation,
    so the lean table's trend agrees with the technical report's section of the
    same scope verbatim. Returns None when the ticker/timeframe is unknown or
    there is not enough history (no rows, or ``_trend_label`` returned N/M).
    """
    if ticker not in TICKERS:
        return None
    tables = _TREND_TIMEFRAMES.get(timeframe)
    if tables is None:
        return None
    ind_table, ohlcv_table, date_col = tables
    rows = _fetch_rows(ticker, ind_table, ohlcv_table, date_col)
    if not rows:
        return None
    prior = rows[1] if len(rows) > 1 else None
    label = _trend_label(
        float(rows[0]["close"]),
        float(prior["close"]) if prior else None,
        rows[0]["sma_20"],
        rows[0]["sma_50"],
    )
    word = label.split(" ", 1)[0]
    return word if word in ("Uptrend", "Sideways", "Downtrend") else None


def _trend_word(rows: list[dict[str, Any]]) -> str | None:
    """The bare Uptrend / Sideways / Downtrend word for an already-fetched
    timeframe's latest bar, or None when there is not enough history."""
    if not rows:
        return None
    latest = rows[0]
    prior = rows[1] if len(rows) > 1 else None
    label = _trend_label(
        float(latest["close"]),
        float(prior["close"]) if prior else None,
        latest["sma_20"],
        latest["sma_50"],
    )
    word = label.split(" ", 1)[0]
    return word if word in ("Uptrend", "Sideways", "Downtrend") else None


def _consensus_line(rows_by_label: dict[str, list[dict[str, Any]]]) -> str:
    """Multi-timeframe trend consensus computed in the template (ADR-012 /
    report-v1 C-7).

    Replaces the majority-rule counting the synthesis prompts used to delegate
    to the LLM: the template already derives each timeframe's TREND word, so it
    also does the counting here and prints both the verdict and its derivation
    verbatim for the agent to quote. Majority rule over the timeframes that have
    a label; ties (or fewer than two agreeing) resolve to Sideways.
    """
    words = {
        tf: _trend_word(rows_by_label.get(tf.upper(), [])) for tf in ("daily", "weekly", "monthly")
    }
    counts = Counter(word for word in words.values() if word)
    consensus = next((word for word, count in counts.items() if count >= 2), "Sideways")
    detail = ", ".join(f"{tf} {words[tf] or 'N/M'}" for tf in ("daily", "weekly", "monthly"))
    return f"Multi-timeframe consensus: {consensus} ({detail}; majority rule, ties to Sideways)"


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
    daily_ctx = _fetch_price_context(ticker, as_of)

    lines = [
        f"# TECHNICAL REPORT — {ticker}",
        f"As of {as_of.isoformat()} (daily, {days_old} days old) — "
        f"{meta.get('sector', 'Unknown sector')}, {meta.get('industry', 'Unknown industry')}",
        _DISCLAIMER,
        _TREND_LABEL_RULE,
        _consensus_line(rows_by_label),
        "",
    ]
    for label, _, _, _ in _TIMEFRAMES:
        price_ctx = daily_ctx if label == "DAILY" else None
        lines.extend(_render_section(label, rows_by_label[label], price_ctx))
        lines.append("")
    lines.append(format_as_of_footer(as_of))
    return "\n".join(lines).rstrip() + "\n"
