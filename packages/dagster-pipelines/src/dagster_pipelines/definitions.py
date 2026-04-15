from dagster import Definitions

from dagster_pipelines.assets.ohlcv_raw import ohlcv_raw
from dagster_pipelines.resources.clickhouse import ClickHouseResource

defs = Definitions(
    assets=[ohlcv_raw],
    resources={
        "clickhouse": ClickHouseResource(),
    },
)
