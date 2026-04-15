import logging

from dagster import (
    AssetKey,
    AssetSelection,
    EventLogEntry,
    RunRequest,
    SensorEvaluationContext,
    asset_sensor,
    define_asset_job,
)

logger = logging.getLogger(__name__)

# ── Jobs for sensor-triggered downstream recomputation ────────

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
)

fundamentals_downstream_job = define_asset_job(
    name="fundamentals_downstream_job",
    selection=AssetSelection.assets("fundamental_summary"),
)


# ── Sensors ───────────────────────────────────────────────────


@asset_sensor(asset_key=AssetKey("ohlcv_raw"), job=ohlcv_downstream_job)
def ohlcv_raw_sensor(context: SensorEvaluationContext, asset_event: EventLogEntry):
    """Trigger downstream recomputation when ohlcv_raw materializes.

    ohlcv_raw → ohlcv_weekly, ohlcv_monthly, technical_indicators (all timeframes),
    fundamental_summary. Dagster respects the dependency DAG within the job,
    so aggregation runs before indicators.
    """
    dagster_event = asset_event.dagster_event
    partition = dagster_event.partition if dagster_event else None
    if partition:
        yield RunRequest(
            run_key=f"ohlcv_downstream_{partition}_{context.cursor}",
            partition_key=partition,
        )


@asset_sensor(asset_key=AssetKey("fundamentals"), job=fundamentals_downstream_job)
def fundamentals_sensor(context: SensorEvaluationContext, asset_event: EventLogEntry):
    """Trigger fundamental_summary recomputation when fundamentals materializes.

    Price-based ratios update daily (via ohlcv_raw sensor), while statement-based
    ratios (margins, growth) update weekly with fresh fundamentals.
    """
    dagster_event = asset_event.dagster_event
    partition = dagster_event.partition if dagster_event else None
    if partition:
        yield RunRequest(
            run_key=f"fundamentals_downstream_{partition}_{context.cursor}",
            partition_key=partition,
        )
