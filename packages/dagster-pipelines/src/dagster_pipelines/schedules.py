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
from shared.tickers import TICKERS

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
    Weekday-only cron ensures no runs on weekends.
    """
    ts = context.scheduled_execution_time.isoformat() if context.scheduled_execution_time else ""
    for ticker in TICKERS:
        yield RunRequest(
            run_key=f"ohlcv_{ticker}_{ts}",
            partition_key=ticker,
            run_config=RunConfig(ops={"ohlcv_raw": OHLCVConfig(period="5d")}),
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


# QNT-53 concurrency pre-flight (docs/patterns.md §"Adding a Dagster Asset"):
#   dagster-daemon mem_limit = 3g (QNT-115). safe_concurrent_runs =
#   (3072 − 660) / 360 ≈ 6. max_concurrent_runs = 3 (dagster.yaml, QNT-113).
#   4-hour cron fans out 10 news partitions but the QueuedRunCoordinator
#   serializes to 3 at a time. Overlap with ohlcv_daily_schedule (17:00 ET) and
#   fundamentals_weekly_schedule (Sun 22:00 ET) stays within the cap — no
#   mem_limit or max_concurrent_runs bump required.
@schedule(
    job=news_raw_job,
    cron_schedule="0 */4 * * *",  # every 4 hours on the hour
    execution_timezone="America/New_York",
    default_status=DefaultScheduleStatus.RUNNING,
)
def news_raw_schedule(context: ScheduleEvaluationContext):
    """News refresh every 4 hours via Finnhub /company-news (QNT-141, ADR-015).

    Finnhub free tier is 60 RPM with 1y historical backfill. 10 tickers × 6
    ticks/day = 60 calls/day — ~1% of the per-minute budget, well clear of
    the ceiling. Requires FINNHUB_API_KEY in env / SOPS prod (QNT-102).
    """
    ts = context.scheduled_execution_time.isoformat() if context.scheduled_execution_time else ""
    for ticker in TICKERS:
        yield RunRequest(
            run_key=f"news_raw_{ticker}_{ts}",
            partition_key=ticker,
        )
