"""Data quality checks for equity_raw.ohlcv_raw.

Each check queries ClickHouse directly and reports pass/fail with context
metadata (row counts, problematic tickers) so failures are diagnosable from
the Dagster UI without needing to open ClickHouse Play.

Severity conventions:
- blocking=True + ERROR: integrity-breaking — row count, NULL close, future dates.
  Prevents downstream materialization in the same job run.
- WARN: stale or suspicious data — does not block downstream.
"""

from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    asset_check,
)

from dagster_pipelines.assets.ohlcv_raw import ohlcv_raw
from dagster_pipelines.resources.clickhouse import ClickHouseResource

# Max staleness for most recent ohlcv_raw row. yfinance is daily, but weekends
# and holidays make 7d a realistic upper bound before something is wrong.
_MAX_STALENESS_DAYS = 7


@asset_check(asset=ohlcv_raw, blocking=True)
def ohlcv_raw_has_rows(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Fail if equity_raw.ohlcv_raw is empty.

    Blocking: downstream indicators and aggregations require non-empty OHLCV.
    """
    result = clickhouse.execute("SELECT count() FROM equity_raw.ohlcv_raw FINAL")
    row_count = int(result.result_rows[0][0])
    return AssetCheckResult(
        passed=row_count > 0,
        metadata={"row_count": row_count},
        description=f"Found {row_count} rows in equity_raw.ohlcv_raw",
    )


@asset_check(asset=ohlcv_raw, blocking=True)
def ohlcv_raw_no_null_close(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Fail if any row has a NULL close price.

    Blocking: NULL close breaks every downstream price-based ratio and indicator.
    """
    result = clickhouse.execute(
        "SELECT count() FROM equity_raw.ohlcv_raw FINAL WHERE close IS NULL"
    )
    null_count = int(result.result_rows[0][0])
    return AssetCheckResult(
        passed=null_count == 0,
        metadata={"null_close_rows": null_count},
        description=f"{null_count} rows with NULL close price",
    )


@asset_check(asset=ohlcv_raw, blocking=True)
def ohlcv_raw_no_future_dates(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Fail if any row has a date in the future.

    Blocking: future dates indicate a data corruption / timezone bug that would
    poison downstream trend calculations.
    """
    result = clickhouse.execute(
        "SELECT count() FROM equity_raw.ohlcv_raw FINAL WHERE date > today()"
    )
    future_count = int(result.result_rows[0][0])
    return AssetCheckResult(
        passed=future_count == 0,
        metadata={"future_date_rows": future_count},
        description=f"{future_count} rows with future dates",
    )


@asset_check(asset=ohlcv_raw)
def ohlcv_raw_dates_fresh(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if the latest ohlcv_raw row is older than _MAX_STALENESS_DAYS.

    Non-blocking: staleness is worth flagging (yfinance silent failure or a
    missing scheduled run) but does not corrupt downstream — indicators
    compute correctly on old data, they just become stale too.
    """
    result = clickhouse.execute(
        "SELECT dateDiff('day', max(date), today()) FROM equity_raw.ohlcv_raw FINAL"
    )
    days_since = result.result_rows[0][0]
    if days_since is None:
        # No rows — handled by ohlcv_raw_has_rows; don't double-fail here.
        return AssetCheckResult(
            passed=True,
            severity=AssetCheckSeverity.WARN,
            description="No rows to measure freshness against",
        )
    days_since = int(days_since)
    passed = days_since <= _MAX_STALENESS_DAYS
    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "days_since_latest": days_since,
            "threshold_days": _MAX_STALENESS_DAYS,
        },
        description=(f"Latest row is {days_since} days old (threshold: {_MAX_STALENESS_DAYS}d)"),
    )
