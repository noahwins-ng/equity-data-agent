from dagster import Definitions

from dagster_pipelines.assets.fundamentals import fundamentals
from dagster_pipelines.assets.ohlcv_raw import ohlcv_raw
from dagster_pipelines.resources.clickhouse import ClickHouseResource

defs = Definitions(
    assets=[ohlcv_raw, fundamentals],
    resources={
        "clickhouse": ClickHouseResource(),
    },
)
