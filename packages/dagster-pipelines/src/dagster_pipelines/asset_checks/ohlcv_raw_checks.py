"""Data quality checks for equity_raw.ohlcv_raw.

Each check queries ClickHouse directly and reports pass/fail with context
metadata (row counts, problematic tickers) so failures are diagnosable from
the Dagster UI without needing to open ClickHouse Play.

Severity conventions:
- blocking=True + ERROR: integrity-breaking — row count, NULL close, future dates.
  Fails the ohlcv_raw ingest run loudly (Dagster UI ERROR + run failure).
- WARN: stale or suspicious data — surfaced but does not fail the run.

What "blocking" does NOT do here (QNT-385, General-Enhancement #10): a blocking
check "prevents downstream materialization in the same job run", but the ohlcv
ingest jobs (ohlcv_daily_job / ohlcv_monthly_refresh_job) select ONLY ohlcv_raw
— there is no downstream asset in the same run for the block to protect. The
real derived pipeline (indicators / aggregations) runs in a SEPARATE
sensor-triggered job (ohlcv_downstream_job) keyed on the ohlcv_raw
ASSET_MATERIALIZATION event (sensors.py::_build_materialization_sensor), which
fires regardless of check outcome. The materialization event is emitted before
the check runs, so a bad row (NULL close, future date) still cascades into every
derived table. These checks ALERT loudly; they do not GATE the derived pipeline.

Decision — do NOT gate the sensor on check status (QNT-385):
- The blocking checks already fail the ingest run loudly, so an operator is
  alerted the moment bad data lands.
- Same-key corruption (a NULL close on an existing (ticker, date)) self-heals:
  the next good fetch supersedes that row via ReplacingMergeTree(fetched_at) and
  re-triggers the sensor, so the derived rows recompute correctly on merge.
- New-key corruption (a spurious future-dated row) does NOT self-heal:
  ReplacingMergeTree only collapses duplicate keys, never deletes one, so a
  bogus row no correct fetch reproduces — and its derived orphan — persists
  until a manual DELETE mutation. This is the rarer case, and the loud blocking
  failure is exactly what prompts that cleanup.
- Either way, gating would make the sensor query each partition's latest check
  evaluation before firing, adding cross-job coupling and a new failure mode
  (what does the sensor do if the status query itself errors?) disproportionate
  to an alerted, mostly-self-healing condition at 10-ticker scale.
- If a real NULL-close / future-date cascade is ever observed in prod, the clean
  fix is a design change (fold the derived assets into the gated job, or use an
  asset-check-conditioned automation), which earns its own ticket — not ad-hoc
  status-polling bolted onto the sensor here.
"""

from datetime import timedelta
from statistics import fmean, stdev

from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    asset_check,
)
from pandas import DataFrame

from dagster_pipelines.assets.ohlcv_raw import ohlcv_raw
from dagster_pipelines.resources.clickhouse import ClickHouseResource

# Max staleness for most recent ohlcv_raw row. yfinance is daily, but weekends
# and holidays make 7d a realistic upper bound before something is wrong.
_MAX_STALENESS_DAYS = 7
_ANOMALY_LOOKBACK_DAYS = 120
_ANOMALY_RECENT_DAYS = 7
_MIN_BASELINE_POINTS = 10
_ROLLING_WINDOW_POINTS = 30
_SIGMA_THRESHOLD = 4.0


def _volume_spike_anomalies(
    df: DataFrame,
    *,
    sigma_threshold: float = _SIGMA_THRESHOLD,
    min_baseline_points: int = _MIN_BASELINE_POINTS,
    window_points: int = _ROLLING_WINDOW_POINTS,
    recent_days: int = _ANOMALY_RECENT_DAYS,
) -> list[dict[str, object]]:
    if df.empty:
        return []
    scored = df.sort_values(["ticker", "date"]).copy()
    max_date = scored["date"].max()
    min_recent_date = max_date - timedelta(days=recent_days)
    anomalies: list[dict[str, object]] = []
    for ticker, group in scored.groupby("ticker", sort=False):
        volumes = [float(v) for v in group["volume"]]
        dates = list(group["date"])
        for index, volume in enumerate(volumes):
            if dates[index] < min_recent_date:
                continue
            baseline = volumes[max(0, index - window_points) : index]
            if len(baseline) < min_baseline_points:
                continue
            std = stdev(baseline)
            if std <= 0:
                continue
            mean = fmean(baseline)
            z_score = (volume - mean) / std
            if z_score > sigma_threshold:
                anomalies.append(
                    {
                        "ticker": str(ticker),
                        "date": str(dates[index]),
                        "volume": int(volume),
                        "baseline_mean": round(mean, 2),
                        "z_score": round(z_score, 2),
                    }
                )
    return anomalies


def _price_gap_anomalies(
    df: DataFrame,
    *,
    sigma_threshold: float = _SIGMA_THRESHOLD,
    min_baseline_points: int = _MIN_BASELINE_POINTS,
    window_points: int = _ROLLING_WINDOW_POINTS,
    recent_days: int = _ANOMALY_RECENT_DAYS,
) -> list[dict[str, object]]:
    if df.empty:
        return []
    scored = df.sort_values(["ticker", "date"]).copy()
    max_date = scored["date"].max()
    min_recent_date = max_date - timedelta(days=recent_days)
    anomalies: list[dict[str, object]] = []
    for ticker, group in scored.groupby("ticker", sort=False):
        closes = [float(v) for v in group["close"]]
        dates = list(group["date"])
        gaps: list[float | None] = [None]
        for previous, current in zip(closes, closes[1:], strict=False):
            if previous <= 0:
                gaps.append(None)
            else:
                gaps.append(abs((current - previous) / previous))
        for index, gap in enumerate(gaps):
            if gap is None or dates[index] < min_recent_date:
                continue
            baseline = [g for g in gaps[max(0, index - window_points) : index] if g is not None]
            if len(baseline) < min_baseline_points:
                continue
            std = stdev(baseline)
            if std <= 0:
                continue
            mean = fmean(baseline)
            z_score = (gap - mean) / std
            if z_score > sigma_threshold:
                anomalies.append(
                    {
                        "ticker": str(ticker),
                        "date": str(dates[index]),
                        "gap_pct": round(gap * 100, 2),
                        "baseline_mean_pct": round(mean * 100, 2),
                        "z_score": round(z_score, 2),
                    }
                )
    return anomalies


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


@asset_check(asset=ohlcv_raw)
def ohlcv_raw_volume_spike_anomaly(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn when recent per-ticker volume is a high-sigma outlier."""
    df = clickhouse.query_df(
        "SELECT ticker, date, sum(volume) AS volume "
        "FROM equity_raw.ohlcv_raw FINAL "
        f"WHERE date >= today() - INTERVAL {_ANOMALY_LOOKBACK_DAYS} DAY "
        "GROUP BY ticker, date "
        "ORDER BY ticker, date"
    )
    anomalies = _volume_spike_anomalies(df)
    return AssetCheckResult(
        passed=len(anomalies) == 0,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "anomalies": anomalies,
            "anomaly_count": len(anomalies),
            "sigma_threshold": _SIGMA_THRESHOLD,
        },
        description=f"{len(anomalies)} recent ticker-days had volume > {_SIGMA_THRESHOLD} sigma",
    )


@asset_check(asset=ohlcv_raw)
def ohlcv_raw_price_gap_anomaly(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn when recent close-to-close moves are high-sigma outliers."""
    df = clickhouse.query_df(
        "SELECT ticker, date, close "
        "FROM equity_raw.ohlcv_raw FINAL "
        f"WHERE date >= today() - INTERVAL {_ANOMALY_LOOKBACK_DAYS} DAY "
        "ORDER BY ticker, date"
    )
    anomalies = _price_gap_anomalies(df)
    return AssetCheckResult(
        passed=len(anomalies) == 0,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "anomalies": anomalies,
            "anomaly_count": len(anomalies),
            "sigma_threshold": _SIGMA_THRESHOLD,
        },
        description=(
            f"{len(anomalies)} recent ticker-days had close-to-close gaps "
            f"> {_SIGMA_THRESHOLD} sigma"
        ),
    )
