import logging
from datetime import datetime

import numpy as np
import pandas as pd
from dagster import (
    AssetExecutionContext,
    Backoff,
    RetryPolicy,
    StaticPartitionsDefinition,
    asset,
)
from shared.tickers import TICKERS

from dagster_pipelines.resources.clickhouse import ClickHouseResource

logger = logging.getLogger(__name__)

ticker_partitions = StaticPartitionsDefinition(TICKERS)


# Indicator columns emitted to ClickHouse. The date column varies per timeframe
# (date / week_start / month_start) so the rest of the schema lives in one
# place — the asset functions only pick the right time key.
_INDICATOR_BASE_COLS: tuple[str, ...] = (
    "sma_20",
    "sma_50",
    "sma_200",
    "ema_12",
    "ema_26",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "macd_bullish_cross",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "bb_pct_b",
    "adx_14",
    "atr_14",
    "obv",
)

_INDICATOR_OUTPUT_COLS: dict[str, list[str]] = {
    time_col: ["ticker", time_col, *_INDICATOR_BASE_COLS, "computed_at"]
    for time_col in ("date", "week_start", "month_start")
}


def _coerce_for_clickhouse(df: pd.DataFrame) -> pd.DataFrame:
    """Map pandas dtypes that don't survive clickhouse_connect's auto-conversion.

    `macd_bullish_cross` is built as a pandas nullable boolean (so warm-up rows
    can carry NA). clickhouse_connect emits Bool/UInt8 from python ints; cast
    to UInt8 with NaN-aware fill (warm-up = 0, no signal known yet).
    """
    if "macd_bullish_cross" in df.columns:
        df["macd_bullish_cross"] = df["macd_bullish_cross"].fillna(False).astype("uint8")
    return df


def compute_indicators(df: pd.DataFrame, price_col: str = "adj_close") -> pd.DataFrame:
    """Compute technical indicators from OHLCV data.

    Momentum/MA indicators (RSI, MACD, SMA, EMA, BB) use ``adj_close`` to avoid
    false signals at stock split boundaries. Range-based indicators (ADX, ATR)
    and OBV use raw ``high``/``low``/``close``/``volume`` — these are the prices
    a trader sees on TradingView (default chart, splits unadjusted), so the
    ticker-detail spot-checks against TV match within rounding.

    Returns a DataFrame with indicator columns added. NaN values represent
    warm-up periods where insufficient data exists for the indicator.

    Indicators: RSI-14, SMA-20/50/200, EMA-12/26, MACD(12/26/9) +
    macd_bullish_cross, BB(20,2) + bb_pct_b, ADX-14, ATR-14, OBV.

    The high/low/close/volume columns are optional — if absent, the
    range-based indicators (adx_14, atr_14, obv) are not emitted. Tests that
    feed in the legacy two-column fixture (date + adj_close) continue to work.
    """
    price = df[price_col]

    # SMA
    df["sma_20"] = price.rolling(window=20, min_periods=20).mean()
    df["sma_50"] = price.rolling(window=50, min_periods=50).mean()
    df["sma_200"] = price.rolling(window=200, min_periods=200).mean()

    # EMA
    df["ema_12"] = price.ewm(span=12, min_periods=12, adjust=False).mean()
    df["ema_26"] = price.ewm(span=26, min_periods=26, adjust=False).mean()

    # RSI-14 (Wilder's smoothing: alpha = 1/14)
    delta = price.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi_14"] = 100 - (100 / (1 + rs))

    # MACD (12/26/9)
    df["macd"] = df["ema_12"] - df["ema_26"]
    df["macd_signal"] = df["macd"].ewm(span=9, min_periods=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]
    # Bullish cross: macd crosses above signal on this bar (prev macd <= prev signal,
    # current macd > current signal). Stored as UInt8 (0/1); pandas nullable
    # boolean is used while computing so warm-up rows (current/prior signal
    # NaN) carry NA rather than a false-positive False before _coerce_for_clickhouse
    # collapses the NA to 0 at write time.
    prev_macd = df["macd"].shift(1)
    prev_signal = df["macd_signal"].shift(1)
    cross = ((df["macd"] > df["macd_signal"]) & (prev_macd <= prev_signal)).astype("boolean")
    cross[prev_signal.isna() | df["macd_signal"].isna()] = pd.NA
    df["macd_bullish_cross"] = cross

    # Bollinger Bands (20, 2)
    df["bb_middle"] = df["sma_20"]
    bb_std = price.rolling(window=20, min_periods=20).std()
    df["bb_upper"] = df["bb_middle"] + 2 * bb_std
    df["bb_lower"] = df["bb_middle"] - 2 * bb_std
    # %B locates price within the band: 0 = lower, 0.5 = middle, 1 = upper, >1
    # = above upper (breakout), <0 = below lower. Using the same price series
    # the bands were built on keeps the ratio numerically consistent.
    bb_range = df["bb_upper"] - df["bb_lower"]
    df["bb_pct_b"] = (price - df["bb_lower"]) / bb_range.replace(0, np.nan)

    # Range-based indicators only run when high/low/close/volume are present —
    # this keeps the legacy adj_close-only test fixture path working while the
    # production assets feed in the full OHLCV frame.
    if {"high", "low", "close", "volume"}.issubset(df.columns):
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)

        # True Range = max(high-low, |high-prev_close|, |low-prev_close|)
        tr = pd.concat(
            [
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        # ATR-14 (Wilder smoothing α=1/14, same convention as RSI)
        df["atr_14"] = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()

        # ADX-14 (Wilder)
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        atr_for_dx = df["atr_14"]
        plus_di = (
            100
            * plus_dm.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
            / atr_for_dx.replace(0, np.nan)
        )
        minus_di = (
            100
            * minus_dm.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
            / atr_for_dx.replace(0, np.nan)
        )
        di_sum = (plus_di + minus_di).replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / di_sum
        df["adx_14"] = dx.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()

        # OBV — cumulative signed volume. yfinance reports raw share count
        # (not split-adjusted), so for tickers with splits the cumulative sum
        # is artificially low for pre-split bars (1 pre-split share == N
        # post-split shares for an N:1 split). TradingView back-adjusts
        # volume to current-share-basis by default, so to match what a
        # trader sees on the chart we multiply by close/adj_close — the
        # cumulative split factor (close = pre-split nominal price,
        # adj_close = post-split-equivalent price). Folds in dividend
        # adjustments too but those are tiny vs splits like NVDA's 10:1.
        # For tickers with no splits adj_close ≈ close so this is a no-op.
        adj_factor = df["close"] / df["adj_close"].replace(0, np.nan)
        adj_volume = df["volume"] * adj_factor
        sign = np.sign(close.diff().fillna(0)).astype("int64")
        df["obv"] = (sign * adj_volume).cumsum().astype("float64")

    return df


@asset(
    deps=["ohlcv_raw"],
    partitions_def=ticker_partitions,
    retry_policy=RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL),
    group_name="derived",
)
def technical_indicators_daily(
    context: AssetExecutionContext,
    clickhouse: ClickHouseResource,
) -> None:
    """Compute technical indicators from daily OHLCV bars.

    Reads adj_close from equity_raw.ohlcv_raw, computes RSI-14, SMA-20/50,
    EMA-12/26, MACD(12/26/9), BB(20,2). Writes to equity_derived.technical_indicators_daily.
    """
    ticker = context.partition_key

    df = clickhouse.query_df(
        "SELECT date, high, low, close, adj_close, volume "
        "FROM equity_raw.ohlcv_raw FINAL "
        "WHERE ticker = {ticker:String} "
        "ORDER BY date",
        parameters={"ticker": ticker},
    )

    if df.empty:
        context.log.warning("No ohlcv_raw data for %s — skipping daily indicators", ticker)
        return

    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = compute_indicators(df)

    df["ticker"] = ticker
    df["computed_at"] = datetime.utcnow()

    result = pd.DataFrame(df[_INDICATOR_OUTPUT_COLS["date"]])
    result = _coerce_for_clickhouse(result)

    clickhouse.insert_df("equity_derived.technical_indicators_daily", result)
    context.log.info("Inserted %d daily indicator rows for %s", len(result), ticker)


@asset(
    deps=["ohlcv_weekly"],
    partitions_def=ticker_partitions,
    retry_policy=RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL),
    group_name="derived",
)
def technical_indicators_weekly(
    context: AssetExecutionContext,
    clickhouse: ClickHouseResource,
) -> None:
    """Compute technical indicators from weekly OHLCV bars.

    Reads adj_close from equity_derived.ohlcv_weekly, writes to
    equity_derived.technical_indicators_weekly.
    """
    ticker = context.partition_key

    df = clickhouse.query_df(
        "SELECT week_start, high, low, close, adj_close, volume "
        "FROM equity_derived.ohlcv_weekly FINAL "
        "WHERE ticker = {ticker:String} "
        "ORDER BY week_start",
        parameters={"ticker": ticker},
    )

    if df.empty:
        context.log.warning("No ohlcv_weekly data for %s — skipping weekly indicators", ticker)
        return

    df["week_start"] = pd.to_datetime(df["week_start"]).dt.date
    df = compute_indicators(df)

    df["ticker"] = ticker
    df["computed_at"] = datetime.utcnow()

    result = pd.DataFrame(df[_INDICATOR_OUTPUT_COLS["week_start"]])
    result = _coerce_for_clickhouse(result)

    clickhouse.insert_df("equity_derived.technical_indicators_weekly", result)
    context.log.info("Inserted %d weekly indicator rows for %s", len(result), ticker)


@asset(
    deps=["ohlcv_monthly"],
    partitions_def=ticker_partitions,
    retry_policy=RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL),
    group_name="derived",
)
def technical_indicators_monthly(
    context: AssetExecutionContext,
    clickhouse: ClickHouseResource,
) -> None:
    """Compute technical indicators from monthly OHLCV bars.

    Reads adj_close from equity_derived.ohlcv_monthly, writes to
    equity_derived.technical_indicators_monthly.
    """
    ticker = context.partition_key

    df = clickhouse.query_df(
        "SELECT month_start, high, low, close, adj_close, volume "
        "FROM equity_derived.ohlcv_monthly FINAL "
        "WHERE ticker = {ticker:String} "
        "ORDER BY month_start",
        parameters={"ticker": ticker},
    )

    if df.empty:
        context.log.warning("No ohlcv_monthly data for %s — skipping monthly indicators", ticker)
        return

    df["month_start"] = pd.to_datetime(df["month_start"]).dt.date
    df = compute_indicators(df)

    df["ticker"] = ticker
    df["computed_at"] = datetime.utcnow()

    result = pd.DataFrame(df[_INDICATOR_OUTPUT_COLS["month_start"]])
    result = _coerce_for_clickhouse(result)

    clickhouse.insert_df("equity_derived.technical_indicators_monthly", result)
    context.log.info("Inserted %d monthly indicator rows for %s", len(result), ticker)
