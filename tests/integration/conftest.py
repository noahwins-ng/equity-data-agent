"""Auto-skip integration tests when ClickHouse is not reachable.

Locally: tests skip unless SSH tunnel is active (make tunnel).
CI: ClickHouse service container is always up — tests always run.
"""

import clickhouse_connect
import pytest
from shared.config import settings


def pytest_runtest_setup(item: pytest.Item) -> None:
    if "integration" not in item.keywords:
        return
    try:
        clickhouse_connect.get_client(
            host=settings.CLICKHOUSE_HOST,
            port=settings.CLICKHOUSE_PORT,
            connect_timeout=2,
        ).query("SELECT 1")
    except Exception:
        pytest.skip("ClickHouse not reachable — run 'make tunnel' to enable integration tests")
