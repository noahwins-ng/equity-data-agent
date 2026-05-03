from dagster import Definitions

from dagster_pipelines.asset_checks import ALL_ASSET_CHECKS
from dagster_pipelines.assets.aggregation import ohlcv_monthly, ohlcv_weekly
from dagster_pipelines.assets.fundamental_summary import fundamental_summary
from dagster_pipelines.assets.fundamentals import fundamentals
from dagster_pipelines.assets.indicators import (
    technical_indicators_daily,
    technical_indicators_monthly,
    technical_indicators_weekly,
)
from dagster_pipelines.assets.news_embeddings import news_embeddings
from dagster_pipelines.assets.news_raw import news_raw
from dagster_pipelines.assets.ohlcv_raw import ohlcv_raw
from dagster_pipelines.resources.clickhouse import ClickHouseResource
from dagster_pipelines.resources.qdrant import QdrantResource
from dagster_pipelines.schedules import (
    fundamentals_weekly_job,
    fundamentals_weekly_schedule,
    news_raw_job,
    news_raw_schedule,
    ohlcv_daily_job,
    ohlcv_daily_schedule,
)
from dagster_pipelines.sensors import (
    fundamentals_downstream_job,
    fundamentals_sensor,
    news_downstream_job,
    news_raw_sensor,
    ohlcv_downstream_job,
    ohlcv_raw_sensor,
)
from dagster_pipelines.vercel_deploy import (
    vercel_deploy_after_fundamentals,
    vercel_deploy_after_news,
    vercel_deploy_after_ohlcv,
    vercel_deploy_job,
)

defs = Definitions(
    assets=[
        ohlcv_raw,
        fundamentals,
        news_raw,
        news_embeddings,
        ohlcv_weekly,
        ohlcv_monthly,
        fundamental_summary,
        technical_indicators_daily,
        technical_indicators_weekly,
        technical_indicators_monthly,
    ],
    asset_checks=ALL_ASSET_CHECKS,
    jobs=[
        ohlcv_daily_job,
        fundamentals_weekly_job,
        news_raw_job,
        ohlcv_downstream_job,
        fundamentals_downstream_job,
        news_downstream_job,
        vercel_deploy_job,
    ],
    schedules=[
        ohlcv_daily_schedule,
        fundamentals_weekly_schedule,
        news_raw_schedule,
        vercel_deploy_after_ohlcv,
        vercel_deploy_after_news,
        vercel_deploy_after_fundamentals,
    ],
    sensors=[ohlcv_raw_sensor, fundamentals_sensor, news_raw_sensor],
    resources={
        "clickhouse": ClickHouseResource(),
        "qdrant": QdrantResource(),
    },
)
