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
