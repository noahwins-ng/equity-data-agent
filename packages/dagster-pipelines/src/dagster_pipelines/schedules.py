import logging

from dagster import (
    AssetSelection,
    RunConfig,
    RunRequest,
    ScheduleEvaluationContext,
    define_asset_job,
    schedule,
)
from shared.tickers import TICKERS

from dagster_pipelines.assets.ohlcv_raw import OHLCVConfig

logger = logging.getLogger(__name__)

# ── Jobs ──────────────────────────────────────────────────────

ohlcv_daily_job = define_asset_job(
    name="ohlcv_daily_job",
    selection=AssetSelection.assets("ohlcv_raw"),
)

fundamentals_weekly_job = define_asset_job(
    name="fundamentals_weekly_job",
    selection=AssetSelection.assets("fundamentals"),
)


# ── Schedules ─────────────────────────────────────────────────


@schedule(
    job=ohlcv_daily_job,
    cron_schedule="0 17 * * 1-5",  # 5 PM ET, weekdays only
    execution_timezone="America/New_York",
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
