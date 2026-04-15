from dagster import Definitions

from dagster_pipelines.assets.aggregation import ohlcv_monthly, ohlcv_weekly
from dagster_pipelines.assets.fundamental_summary import fundamental_summary
from dagster_pipelines.assets.fundamentals import fundamentals
from dagster_pipelines.assets.indicators import (
    technical_indicators_daily,
    technical_indicators_monthly,
    technical_indicators_weekly,
)
from dagster_pipelines.assets.ohlcv_raw import ohlcv_raw
from dagster_pipelines.resources.clickhouse import ClickHouseResource
from dagster_pipelines.schedules import (
    fundamentals_weekly_job,
    fundamentals_weekly_schedule,
    ohlcv_daily_job,
    ohlcv_daily_schedule,
)

defs = Definitions(
    assets=[
        ohlcv_raw,
        fundamentals,
        ohlcv_weekly,
        ohlcv_monthly,
        fundamental_summary,
        technical_indicators_daily,
        technical_indicators_weekly,
        technical_indicators_monthly,
    ],
    jobs=[ohlcv_daily_job, fundamentals_weekly_job],
    schedules=[ohlcv_daily_schedule, fundamentals_weekly_schedule],
    resources={
        "clickhouse": ClickHouseResource(),
    },
)
