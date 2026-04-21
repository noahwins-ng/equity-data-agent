import logging
from typing import Any

from dagster import (
    AssetKey,
    AssetSelection,
    DagsterEventType,
    DefaultSensorStatus,
    EventRecordsFilter,
    RunRequest,
    SensorEvaluationContext,
    define_asset_job,
    sensor,
)

from dagster_pipelines.retry import DEPLOY_WINDOW_RETRY, DEPLOY_WINDOW_RUN_RETRY_TAGS

logger = logging.getLogger(__name__)

# ── Jobs for sensor-triggered downstream recomputation ────────
# Both layers of deploy-window protection from QNT-110:
#   op_retry_policy — in-run retry for flaky ops
#   tags             — instance-level re-launch on launch-time failures (gRPC UNAVAILABLE)

ohlcv_downstream_job = define_asset_job(
    name="ohlcv_downstream_job",
    selection=AssetSelection.assets(
        "ohlcv_weekly",
        "ohlcv_monthly",
        "technical_indicators_daily",
        "technical_indicators_weekly",
        "technical_indicators_monthly",
        "fundamental_summary",
    ),
    op_retry_policy=DEPLOY_WINDOW_RETRY,
    tags=DEPLOY_WINDOW_RUN_RETRY_TAGS,
)

fundamentals_downstream_job = define_asset_job(
    name="fundamentals_downstream_job",
    selection=AssetSelection.assets("fundamental_summary"),
    op_retry_policy=DEPLOY_WINDOW_RETRY,
    tags=DEPLOY_WINDOW_RUN_RETRY_TAGS,
)

# QNT-54: embedding happens server-side via Qdrant Cloud Inference (ADR-009),
# so the run-worker is I/O-bound (one HTTP POST per batch) rather than memory-
# bound. No tag_concurrency_limits rule is needed; this job fans out under
# max_concurrent_runs: 3 alongside OHLCV / fundamentals.
news_downstream_job = define_asset_job(
    name="news_downstream_job",
    selection=AssetSelection.assets("news_embeddings"),
    op_retry_policy=DEPLOY_WINDOW_RETRY,
    tags=DEPLOY_WINDOW_RUN_RETRY_TAGS,
)


# ── Sensors ───────────────────────────────────────────────────


def _build_materialization_sensor(
    name: str,
    asset_key: AssetKey,
    job: Any,
):
    """Build a sensor that watches for materialization events on a partitioned asset
    and triggers a downstream job for each partition that materialized.

    Unlike @asset_sensor which processes one event per tick, this queries ALL
    new events since the cursor in a single evaluation.
    """

    @sensor(name=name, job=job, default_status=DefaultSensorStatus.RUNNING)
    def _sensor(context: SensorEvaluationContext):
        cursor = int(context.cursor) if context.cursor else None

        events = context.instance.get_event_records(
            EventRecordsFilter(
                event_type=DagsterEventType.ASSET_MATERIALIZATION,
                asset_key=asset_key,
                after_cursor=cursor,
            ),
            ascending=True,
            limit=100,
        )

        if not events:
            return

        for event in events:
            partition = event.partition_key
            if partition:
                yield RunRequest(
                    run_key=f"{name}_{partition}_{event.storage_id}",
                    partition_key=partition,
                )

        # Advance cursor past all processed events
        context.update_cursor(str(events[-1].storage_id))

    return _sensor


ohlcv_raw_sensor = _build_materialization_sensor(
    name="ohlcv_raw_sensor",
    asset_key=AssetKey("ohlcv_raw"),
    job=ohlcv_downstream_job,
)

fundamentals_sensor = _build_materialization_sensor(
    name="fundamentals_sensor",
    asset_key=AssetKey("fundamentals"),
    job=fundamentals_downstream_job,
)

news_raw_sensor = _build_materialization_sensor(
    name="news_raw_sensor",
    asset_key=AssetKey("news_raw"),
    job=news_downstream_job,
)
