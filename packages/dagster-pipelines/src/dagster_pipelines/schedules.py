import logging

from dagster import (
    AssetSelection,
    DefaultScheduleStatus,
    RunConfig,
    RunRequest,
    ScheduleEvaluationContext,
    define_asset_job,
    schedule,
)
from shared.tickers import ALL_OHLCV_TICKERS, TICKERS

from dagster_pipelines.assets.ohlcv_raw import OHLCVConfig
from dagster_pipelines.retry import DEPLOY_WINDOW_RUN_RETRY_TAGS

logger = logging.getLogger(__name__)

# ── Jobs ──────────────────────────────────────────────────────
# Run-level retry tags from QNT-110 protect schedule-triggered runs from the
# same deploy-window gRPC UNAVAILABLE failure class as sensor-triggered runs.
# (Op-level retry is scoped to sensor jobs per ticket — schedule jobs materialize
# fresh yfinance data, so in-run op retry adds less value than re-launching.)

ohlcv_daily_job = define_asset_job(
    name="ohlcv_daily_job",
    selection=AssetSelection.assets("ohlcv_raw"),
    tags=DEPLOY_WINDOW_RUN_RETRY_TAGS,
)

# Separate job name (same asset) so monthly full-refresh runs are filterable in
# the Dagster run history apart from the daily incremental runs (QNT-235).
ohlcv_monthly_refresh_job = define_asset_job(
    name="ohlcv_monthly_refresh_job",
    selection=AssetSelection.assets("ohlcv_raw"),
    tags=DEPLOY_WINDOW_RUN_RETRY_TAGS,
)

fundamentals_weekly_job = define_asset_job(
    name="fundamentals_weekly_job",
    selection=AssetSelection.assets("fundamentals"),
    tags=DEPLOY_WINDOW_RUN_RETRY_TAGS,
)

news_raw_job = define_asset_job(
    name="news_raw_job",
    selection=AssetSelection.assets("news_raw"),
    tags=DEPLOY_WINDOW_RUN_RETRY_TAGS,
)


# ── Schedules ─────────────────────────────────────────────────


@schedule(
    job=ohlcv_daily_job,
    cron_schedule="0 17 * * 1-5",  # 5 PM ET, weekdays only
    execution_timezone="America/New_York",
    default_status=DefaultScheduleStatus.RUNNING,
)
def ohlcv_daily_schedule(context: ScheduleEvaluationContext):
    """Daily OHLCV refresh at market close (5 PM ET, Mon-Fri).

    Uses period='5d' for incremental fetches instead of full backfill.
    Weekday-only cron ensures no runs on weekends. Benchmark tickers (SPY)
    ride the same schedule — they're partitions of ohlcv_raw too.
    """
    ts = context.scheduled_execution_time.isoformat() if context.scheduled_execution_time else ""
    for ticker in ALL_OHLCV_TICKERS:
        yield RunRequest(
            run_key=f"ohlcv_{ticker}_{ts}",
            partition_key=ticker,
            run_config=RunConfig(ops={"ohlcv_raw": OHLCVConfig(period="5d")}),
        )


# Corporate-action history correction (QNT-235). yfinance returns split- and
# dividend-adjusted series and retroactively rewrites the ENTIRE history on a
# corporate action; the daily period="5d" incremental only overwrites the last
# few rows, leaving a pre-action history spliced onto a post-action tail. A
# monthly full refetch (period="2y") rewrites every (ticker, date) row;
# ReplacingMergeTree(fetched_at) dedup-replaces the stale rows for free, so the
# stored series is made self-consistent again with no detection logic. This is
# self-healing on a monthly cadence: a corporate action's bad splice survives at
# most until the next monthly run.
#
# The correction propagates downstream for free: ohlcv_raw_sensor watches every
# ohlcv_raw materialization regardless of source job (sensors.py), so each
# refreshed ticker auto-triggers ohlcv_downstream_job — indicators/aggregations
# re-materialize off the corrected base with no extra wiring.
#
# Concurrency pre-flight (per docs/patterns.md): fan-out = 11 refresh partitions,
# plus ~10 sensor-triggered ohlcv_downstream runs (SPY skipped) once the refresh
# materializations land — ~21 runs total on the month boundary. Fires at 06:00 ET
# on the 1st, clear of ohlcv_daily (17:00 weekdays), news_raw (02:00 daily) and
# fundamentals (Sun 22:00), so nothing else competes; the runs simply drain
# 3-at-a-time under max_concurrent_runs: 3 (QNT-113) — slower, not unsafe (per-run
# isolation at mem_limit: 3g, QNT-116). Fetch cost is identical to the original 2y
# backfill: one yfinance request per ticker — see QNT-235 PR.
@schedule(
    job=ohlcv_monthly_refresh_job,
    cron_schedule="0 6 1 * *",  # 06:00 ET, 1st of each month
    execution_timezone="America/New_York",
    default_status=DefaultScheduleStatus.RUNNING,
)
def ohlcv_monthly_refresh_schedule(context: ScheduleEvaluationContext):
    """Monthly full-history OHLCV refresh (period="2y") for all tickers.

    Corrects split/dividend-induced history splices left by the daily period="5d"
    incremental. ReplacingMergeTree(fetched_at) replaces the stale rows on merge.
    """
    ts = context.scheduled_execution_time.isoformat() if context.scheduled_execution_time else ""
    for ticker in ALL_OHLCV_TICKERS:
        yield RunRequest(
            run_key=f"ohlcv_refresh_{ticker}_{ts}",
            partition_key=ticker,
            run_config=RunConfig(ops={"ohlcv_raw": OHLCVConfig(period="2y")}),
        )


@schedule(
    job=fundamentals_weekly_job,
    cron_schedule="0 22 * * 0",  # 10 PM ET, Sunday night
    execution_timezone="America/New_York",
    default_status=DefaultScheduleStatus.RUNNING,
)
def fundamentals_weekly_schedule(context: ScheduleEvaluationContext):
    """Weekly fundamentals refresh (Sunday 10 PM ET).

    Quarterly data changes infrequently, so weekly is sufficient.
    """
    ts = context.scheduled_execution_time.isoformat() if context.scheduled_execution_time else ""
    for ticker in TICKERS:
        yield RunRequest(
            run_key=f"fundamentals_{ticker}_{ts}",
            partition_key=ticker,
        )


# 02:00 ET aligns with the design v2 EOD framing (docs/design-frontend-plan.md
# push-back #1 + QNT-72 bottom-left status `EOD · 02:00 ET`). Clean of overlap
# with ohlcv_daily_schedule (17:00 ET) and fundamentals_weekly_schedule
# (Sun 22:00 ET); post-QNT-116 each run is an isolated container so daemon
# mem_limit no longer gates concurrency.
@schedule(
    job=news_raw_job,
    cron_schedule="0 2 * * *",  # 02:00 ET daily, 7 days/week
    execution_timezone="America/New_York",
    default_status=DefaultScheduleStatus.RUNNING,
)
def news_raw_schedule(context: ScheduleEvaluationContext):
    """News refresh daily at 02:00 ET via Finnhub /company-news (QNT-141, ADR-015).

    Finnhub free tier is 60 RPM with 1y historical backfill. 10 tickers × 1
    tick/day = 10 calls/day — trivial against the per-minute ceiling, and
    delta-only upsert (QNT-142) keeps Qdrant inference at ~70k tokens/month.
    7-day cron: news happens on weekends too (after-hours announcements,
    weekend macro, earnings warnings). Requires FINNHUB_API_KEY in env /
    SOPS prod (QNT-102).
    """
    ts = context.scheduled_execution_time.isoformat() if context.scheduled_execution_time else ""
    for ticker in TICKERS:
        yield RunRequest(
            run_key=f"news_raw_{ticker}_{ts}",
            partition_key=ticker,
        )
