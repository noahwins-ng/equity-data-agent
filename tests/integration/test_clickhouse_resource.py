import pandas as pd
import pytest
from dagster_pipelines.resources.clickhouse import ClickHouseResource


@pytest.fixture
def ch() -> ClickHouseResource:
    return ClickHouseResource()


@pytest.mark.integration
def test_execute_basic(ch: ClickHouseResource) -> None:
    result = ch.execute("SELECT 1 AS n")
    assert result.result_rows == [(1,)]


@pytest.mark.integration
def test_insert_df(ch: ClickHouseResource) -> None:
    ch.execute("CREATE DATABASE IF NOT EXISTS test_tmp")
    ch.execute("""
        CREATE TABLE IF NOT EXISTS test_tmp.resource_test (
            x Int32
        ) ENGINE = Memory
    """)
    try:
        df = pd.DataFrame({"x": [1, 2, 3]})
        ch.insert_df("test_tmp.resource_test", df)
        result = ch.execute("SELECT count() FROM test_tmp.resource_test")
        assert result.result_rows == [(3,)]
    finally:
        ch.execute("DROP TABLE IF EXISTS test_tmp.resource_test")
        ch.execute("DROP DATABASE IF EXISTS test_tmp")
