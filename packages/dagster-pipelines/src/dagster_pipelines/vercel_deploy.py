"""Vercel Deploy Hook trigger (QNT-168).

The frontend is statically rendered (`/ticker/[symbol]` and `/` use
``dynamic = "force-static"``); freshness is driven by Dagster posting a
Vercel Deploy Hook URL after each ingest cycle. Vercel rebuilds the site
with the freshly ingested OHLCV / news / fundamentals data and the new
deployment goes live.

Why this pattern instead of ISR + revalidateTag?
* ISR was burning ~150K Vercel ISR Writes/24h for data that changes once
  per day; QNT-166 ratcheted the TTL three times without solving the
  underlying mismatch (TTL-based revalidation against deterministic
  EOD-cadence data).
* Build-time SSG + deploy-on-data-change drops the cache layer entirely.
  Mental model matches reality: data changed -> deploy.
* The hook URL itself is the auth -- no shared secret across two control
  planes (Hetzner-side .env.sops and Vercel project env).

Three schedules trigger this op, each ~35 min after an ingest schedule's
cron:

* 17:35 ET weekdays   -- after ohlcv_daily_schedule (17:00 ET)
* 02:35 ET daily       -- after news_raw_schedule (02:00 ET)
* 22:35 ET Sundays     -- after fundamentals_weekly_schedule (Sun 22:00 ET)

35 min is a comfortable window: a 10-ticker ohlcv backfill takes ~3-5 min
serialized, indicators + summary downstream <2 min, well clear of the
trigger.

Failure handling: any HTTP error / non-2xx response logs at WARNING and
the run completes successfully. A missed deploy leaves the prior deploy
serving (still correct, just one cycle stale); the next scheduled tick
will retry.
"""

import httpx
from dagster import (
    DefaultScheduleStatus,
    OpExecutionContext,
    RunRequest,
    ScheduleEvaluationContext,
    job,
    op,
    schedule,
)
from shared.config import settings

_DEPLOY_HOOK_TIMEOUT_SECONDS = 10.0


@op
def trigger_vercel_deploy(context: OpExecutionContext) -> None:
    """POST the Vercel Deploy Hook to rebuild the frontend.

    Idempotent from the caller's perspective: Vercel debounces hook calls
    that arrive within a short window, so accidental double-fires (a
    schedule retry, a manual re-trigger from the UI) coalesce into one
    deploy.
    """
    url = settings.VERCEL_DEPLOY_HOOK_URL
    if not url:
        context.log.info("VERCEL_DEPLOY_HOOK_URL not set; skipping deploy trigger")
        return

    try:
        with httpx.Client(timeout=_DEPLOY_HOOK_TIMEOUT_SECONDS) as client:
            response = client.post(url)
        if response.status_code // 100 == 2:
            context.log.info("vercel deploy triggered (%s)", response.status_code)
            return
        context.log.warning(
            "vercel deploy non-2xx: status=%s body=%s",
            response.status_code,
            response.text[:200],
        )
    except httpx.HTTPError as exc:
        context.log.warning("vercel deploy HTTP error: %s", exc)


@job
def vercel_deploy_job() -> None:
    """One-op job that fires the Deploy Hook. Intentionally tiny -- the job
    exists so the schedules below have something to attach to."""
    trigger_vercel_deploy()


# Naming: the suffix names the upstream ingest cron each schedule trails,
# so the Dagster UI shows the relationship at a glance.


@schedule(
    job=vercel_deploy_job,
    # 35 min after ohlcv_daily_schedule (17:00 ET, weekdays).
    cron_schedule="35 17 * * 1-5",
    execution_timezone="America/New_York",
    default_status=DefaultScheduleStatus.RUNNING,
)
def vercel_deploy_after_ohlcv(context: ScheduleEvaluationContext) -> RunRequest:
    ts = context.scheduled_execution_time.isoformat() if context.scheduled_execution_time else ""
    return RunRequest(run_key=f"vercel_deploy_after_ohlcv_{ts}")


@schedule(
    job=vercel_deploy_job,
    # 35 min after news_raw_schedule (02:00 ET, daily 7d/wk).
    cron_schedule="35 2 * * *",
    execution_timezone="America/New_York",
    default_status=DefaultScheduleStatus.RUNNING,
)
def vercel_deploy_after_news(context: ScheduleEvaluationContext) -> RunRequest:
    ts = context.scheduled_execution_time.isoformat() if context.scheduled_execution_time else ""
    return RunRequest(run_key=f"vercel_deploy_after_news_{ts}")


@schedule(
    job=vercel_deploy_job,
    # 35 min after fundamentals_weekly_schedule (Sun 22:00 ET).
    cron_schedule="35 22 * * 0",
    execution_timezone="America/New_York",
    default_status=DefaultScheduleStatus.RUNNING,
)
def vercel_deploy_after_fundamentals(
    context: ScheduleEvaluationContext,
) -> RunRequest:
    ts = context.scheduled_execution_time.isoformat() if context.scheduled_execution_time else ""
    return RunRequest(run_key=f"vercel_deploy_after_fundamentals_{ts}")
