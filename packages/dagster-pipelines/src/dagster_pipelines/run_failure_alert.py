"""QNT-62: Dagster run-failure -> Discord webhook alert.

Wires a ``run_failure_sensor`` to ``DISCORD_WEBHOOK_URL`` so asset
materialization failures alert in the same channel as container-level events
(QNT-101) and Grafana memory / restart-loop alerts (QNT-103).

The sensor fires once per failed run; transient retries inside a run are
absorbed by the asset / job ``RetryPolicy`` (``DEPLOY_WINDOW_RETRY``) and
``DEPLOY_WINDOW_RUN_RETRY_TAGS``, so by the time control reaches this sensor
the framework has already exhausted those layers.

Dedup scope -- (job, partition), not (asset, partition):
    The AC says "per (asset, partition)" but the implementation deduplicates
    on ``(job_name, partition)``. The two are equivalent for every job in this
    repo (each is either a single-asset job or a tightly-coupled multi-asset
    job that fails as one unit). Multi-asset jobs intentionally produce a
    single alert per stuck partition rather than one per asset -- the operator
    investigates the run, not each asset in isolation. For unpartitioned jobs
    (``vercel_deploy_job``) the partition string is empty and dedup degrades to
    "per-job within window," which is the desired behavior for one-shot jobs.
    Dagster refuses to launch a partitioned job without ``partition_key``
    (QNT-167), so a partitioned job can never fall into the empty-partition
    branch in practice.

Cursor-based dedup is unavailable here because ``RunStatusSensor`` reserves
the cursor for its own bookkeeping; querying recent runs by tag is the
supported substitute.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
from dagster import (
    DagsterRun,
    DefaultSensorStatus,
    RunFailureSensorContext,
    run_failure_sensor,
)
from dagster._core.storage.dagster_run import DagsterRunStatus, RunsFilter
from shared.config import settings

if TYPE_CHECKING:
    from dagster._core.instance import DagsterInstance

logger = logging.getLogger(__name__)

_DISCORD_TIMEOUT_SECONDS = 10.0
_DISCORD_MAX_CONTENT = 1900  # Discord caps `content` at 2000; leave JSON-escape headroom
_RATE_LIMIT_WINDOW_SECONDS = 600  # 10 min — matches AC


def _post_discord(webhook_url: str, content: str) -> bool:
    """POST a Discord webhook payload. Returns True on 2xx, False otherwise.

    All errors are swallowed: the alerting path must never crash a sensor tick,
    or the daemon enters a retry loop on the sensor itself.
    """
    if len(content) > _DISCORD_MAX_CONTENT:
        content = content[: _DISCORD_MAX_CONTENT - 3] + "..."
    try:
        with httpx.Client(timeout=_DISCORD_TIMEOUT_SECONDS) as client:
            resp = client.post(webhook_url, json={"content": content})
        return resp.status_code // 100 == 2
    except httpx.HTTPError as exc:
        logger.warning("discord post failed: %s", exc)
        return False


def _extract_failure_details(
    context: RunFailureSensorContext,
) -> tuple[str | None, str | None, str | None]:
    """Pull (step_key, exception_first_line, asset_key) from the run.

    Falls back to the run-level failure event message if no step-failure event
    is present. Returns (None, None, None) for any field we can't determine.
    """
    step_key: str | None = None
    exception_line: str | None = None
    asset_key: str | None = None

    msg = context.failure_event.message
    if msg:
        exception_line = msg.splitlines()[0][:300]

    step_events = context.get_step_failure_events()
    if step_events:
        first = step_events[0]
        step_key = first.step_key
        error = getattr(first.event_specific_data, "error", None)
        if error is not None:
            err_msg = getattr(error, "message", None)
            if err_msg:
                exception_line = err_msg.splitlines()[0][:300]

    selection = context.dagster_run.asset_selection
    if selection:
        keys = list(selection)
        if len(keys) == 1:
            asset_key = "/".join(keys[0].path)

    return step_key, exception_line, asset_key


def _format_message(
    *,
    run: DagsterRun,
    asset_key: str | None,
    partition: str,
    step_key: str | None,
    exception_line: str | None,
    run_url: str,
) -> str:
    """Build the Discord content string.

    Format mirrors ``docker-events-notify.sh`` (QNT-101) for visual consistency
    in the same channel: ``[LABEL] key=`val` ...`` header, fenced exception
    block, trailing run URL.
    """
    parts = [f"[ASSET FAILURE] job=`{run.job_name}`"]
    if asset_key:
        parts.append(f"asset=`{asset_key}`")
    if partition:
        parts.append(f"partition=`{partition}`")
    if step_key:
        parts.append(f"step=`{step_key}`")
    header = " ".join(parts)
    body = f"```\n{exception_line or '(no exception captured)'}\n```"
    return f"{header}\n{body}\nrun: {run_url}"


def _has_earlier_failure_in_window(
    *,
    instance: DagsterInstance,
    current_run_id: str,
    job_name: str,
    partition: str,
    window_seconds: float,
) -> bool:
    """True if a different run with matching (job_name, partition) reached
    FAILURE in the last ``window_seconds`` seconds. Used to suppress alert
    spam when the run-retry layer keeps relaunching a stuck partition.

    The current run's own FAILURE record is included in the query result
    (Dagster transitions to FAILURE before dispatching the sensor), so the
    loop must exclude it explicitly -- otherwise every fresh failure would
    self-suppress.

    ``limit=100`` is loose-but-bounded headroom: a worst-case burst is one
    failure per partition (10 tickers) inside the 10-min window, so 100 leaves
    an order of magnitude before the page truncates the current run off the
    end and the dedup query returns False on a run that should have suppressed.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=window_seconds)
    tags: dict[str, str] | None = {"dagster/partition": partition} if partition else None
    records = instance.get_run_records(
        filters=RunsFilter(
            job_name=job_name,
            statuses=[DagsterRunStatus.FAILURE],
            tags=tags,
            updated_after=cutoff,
        ),
        limit=100,
    )
    for record in records:
        if record.dagster_run.run_id != current_run_id:
            return True
    return False


@run_failure_sensor(
    name="dagster_run_failure_alert_sensor",
    default_status=DefaultSensorStatus.RUNNING,
    description=(
        "QNT-62: POSTs a one-line summary to DISCORD_WEBHOOK_URL when a run "
        "fails after the asset RetryPolicy is exhausted. Per-(job, partition) "
        "rate-limited for 10 min so a stuck partition retrying back-to-back "
        "does not spam the channel."
    ),
)
def dagster_run_failure_alert_sensor(context: RunFailureSensorContext) -> None:
    webhook_url = settings.DISCORD_WEBHOOK_URL
    if not webhook_url:
        context.log.info("DISCORD_WEBHOOK_URL not set; skipping Discord alert")
        return

    run = context.dagster_run
    partition = run.tags.get("dagster/partition", "")

    if _has_earlier_failure_in_window(
        instance=context.instance,
        current_run_id=run.run_id,
        job_name=run.job_name,
        partition=partition,
        window_seconds=_RATE_LIMIT_WINDOW_SECONDS,
    ):
        context.log.info(
            "rate-limited Discord alert for job=%s partition=%s (within %ds window)",
            run.job_name,
            partition or "<none>",
            _RATE_LIMIT_WINDOW_SECONDS,
        )
        return

    step_key, exception_line, asset_key = _extract_failure_details(context)
    base = settings.DAGSTER_BASE_URL.rstrip("/")
    run_url = f"{base}/runs/{run.run_id}" if base else f"runs/{run.run_id}"
    content = _format_message(
        run=run,
        asset_key=asset_key,
        partition=partition,
        step_key=step_key,
        exception_line=exception_line,
        run_url=run_url,
    )

    if not _post_discord(webhook_url, content):
        context.log.warning("discord alert not delivered for run %s", run.run_id)
