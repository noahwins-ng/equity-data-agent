"""Data quality checks for equity_derived.technical_indicators_{daily,weekly,monthly}.

Indicators are stored as Nullable(Float64) — pandas NaN becomes SQL NULL.
NULL is expected during the warm-up period (SMA-50 needs 50 bars, MACD signal
needs 35 bars). These checks distinguish expected warm-up NULLs from
computational bugs.

Timeframe coverage:
- daily   (~500 bars/2y): all indicators populated post-warm-up. Check recent 30.
- weekly  (~104 bars/2y): all indicators populated post-warm-up. Check recent 30.
- monthly (~24 bars/2y):  SMA-50 / MACD never complete warm-up. Only check RSI.
"""

from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    asset_check,
)

from dagster_pipelines.assets.indicators import (
    technical_indicators_daily,
    technical_indicators_monthly,
    technical_indicators_weekly,
)
from dagster_pipelines.resources.clickhouse import ClickHouseResource

_DAILY_TABLE = "equity_derived.technical_indicators_daily"
_WEEKLY_TABLE = "equity_derived.technical_indicators_weekly"
_MONTHLY_TABLE = "equity_derived.technical_indicators_monthly"

# Columns required post-warm-up for daily + weekly (>=50 bars available).
_POST_WARMUP_COLS = ("rsi_14", "sma_20", "ema_12", "ema_26", "macd", "macd_signal")

# Bars considered "recent" for the no-NaN check.
_RECENT_BARS = 30


def _rsi_out_of_range_count(clickhouse: ClickHouseResource, table: str) -> int:
    """Count rows where RSI-14 is present but outside [0, 100]."""
    result = clickhouse.execute(
        f"SELECT count() FROM {table} FINAL "
        f"WHERE rsi_14 IS NOT NULL AND (rsi_14 < 0 OR rsi_14 > 100)"
    )
    return int(result.result_rows[0][0])


def _rsi_check_result(clickhouse: ClickHouseResource, table: str) -> AssetCheckResult:
    bad = _rsi_out_of_range_count(clickhouse, table)
    return AssetCheckResult(
        passed=bad == 0,
        severity=AssetCheckSeverity.WARN,
        metadata={"rows_outside_rsi_range": bad, "table": table},
        description=f"{bad} rows with RSI outside [0, 100]",
    )


def _macd_signal_coherence_count(clickhouse: ClickHouseResource, table: str, date_col: str) -> int:
    """Count tickers whose most-recent row has macd non-null but macd_signal null.

    Past the 35-bar signal warm-up, macd_signal should exist whenever macd does.
    Checking the most-recent row per ticker keeps the query simple while
    catching the computational-bug case (e.g. ewm window lost).
    """
    # argMax(macd, date) returns the MACD for the latest date; same for signal.
    result = clickhouse.execute(
        f"SELECT countIf(latest_macd IS NOT NULL AND latest_signal IS NULL) FROM ("
        f"  SELECT ticker, "
        f"         argMax(macd, {date_col}) AS latest_macd, "
        f"         argMax(macd_signal, {date_col}) AS latest_signal "
        f"  FROM {table} FINAL "
        f"  GROUP BY ticker"
        f")"
    )
    return int(result.result_rows[0][0])


def _macd_signal_check_result(
    clickhouse: ClickHouseResource, table: str, date_col: str
) -> AssetCheckResult:
    bad = _macd_signal_coherence_count(clickhouse, table, date_col)
    return AssetCheckResult(
        passed=bad == 0,
        severity=AssetCheckSeverity.WARN,
        metadata={"tickers_with_macd_no_signal": bad, "table": table},
        description=(f"{bad} tickers have macd populated but macd_signal NULL on latest row"),
    )


def _recent_bars_nan_count(
    clickhouse: ClickHouseResource,
    table: str,
    date_col: str,
) -> tuple[int, dict[str, int]]:
    """Count NULL values per indicator column in the most recent N bars per ticker.

    Returns (total_nulls, per_column_nulls).
    """
    is_null_exprs = ", ".join(f"countIf({col} IS NULL) AS {col}_nulls" for col in _POST_WARMUP_COLS)
    query = (
        f"WITH ranked AS ("
        f"  SELECT *, row_number() OVER (PARTITION BY ticker ORDER BY {date_col} DESC) AS rn "
        f"  FROM {table} FINAL"
        f") "
        f"SELECT {is_null_exprs} FROM ranked WHERE rn <= {_RECENT_BARS}"
    )
    result = clickhouse.execute(query)
    row = result.result_rows[0] if result.result_rows else tuple(0 for _ in _POST_WARMUP_COLS)
    per_col = {col: int(row[i]) for i, col in enumerate(_POST_WARMUP_COLS)}
    return sum(per_col.values()), per_col


def _recent_nan_check_result(
    clickhouse: ClickHouseResource, table: str, date_col: str
) -> AssetCheckResult:
    total, per_col = _recent_bars_nan_count(clickhouse, table, date_col)
    return AssetCheckResult(
        passed=total == 0,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "total_nulls_in_recent_bars": total,
            "recent_bars": _RECENT_BARS,
            "per_column": per_col,
            "table": table,
        },
        description=(
            f"{total} NULL values across core indicators in most recent "
            f"{_RECENT_BARS} bars per ticker"
        ),
    )


# ---- daily ----


@asset_check(asset=technical_indicators_daily)
def daily_rsi_in_range(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any daily RSI-14 value is outside [0, 100]."""
    return _rsi_check_result(clickhouse, _DAILY_TABLE)


@asset_check(asset=technical_indicators_daily)
def daily_macd_signal_coherent(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any ticker's latest daily row has MACD but no MACD signal."""
    return _macd_signal_check_result(clickhouse, _DAILY_TABLE, "date")


@asset_check(asset=technical_indicators_daily)
def daily_recent_no_nan(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any core indicator is NULL in the recent 30 daily bars per ticker."""
    return _recent_nan_check_result(clickhouse, _DAILY_TABLE, "date")


# ---- weekly ----


@asset_check(asset=technical_indicators_weekly)
def weekly_rsi_in_range(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any weekly RSI-14 value is outside [0, 100]."""
    return _rsi_check_result(clickhouse, _WEEKLY_TABLE)


@asset_check(asset=technical_indicators_weekly)
def weekly_macd_signal_coherent(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any ticker's latest weekly row has MACD but no MACD signal."""
    return _macd_signal_check_result(clickhouse, _WEEKLY_TABLE, "week_start")


@asset_check(asset=technical_indicators_weekly)
def weekly_recent_no_nan(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any core indicator is NULL in the recent 30 weekly bars per ticker."""
    return _recent_nan_check_result(clickhouse, _WEEKLY_TABLE, "week_start")


# ---- monthly ----
# Monthly only gets 2y ≈ 24 bars, which is shorter than SMA-50 / MACD warm-up.
# Skip the "recent no NaN" and "MACD signal coherent" checks for monthly — they
# would spuriously fail on a valid but sparse history. RSI-14 (warm-up = 14)
# is the only indicator guaranteed to be populated on monthly.


@asset_check(asset=technical_indicators_monthly)
def monthly_rsi_in_range(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any monthly RSI-14 value is outside [0, 100]."""
    return _rsi_check_result(clickhouse, _MONTHLY_TABLE)


__all__ = [
    "daily_macd_signal_coherent",
    "daily_recent_no_nan",
    "daily_rsi_in_range",
    "monthly_rsi_in_range",
    "weekly_macd_signal_coherent",
    "weekly_recent_no_nan",
    "weekly_rsi_in_range",
]
