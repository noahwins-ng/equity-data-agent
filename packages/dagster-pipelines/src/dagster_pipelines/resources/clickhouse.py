from __future__ import annotations

import logging
import time
from typing import Any

import clickhouse_connect
import pandas as pd
from clickhouse_connect.driver.client import Client
from dagster import ConfigurableResource
from pydantic import Field
from shared.config import settings

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAY = 2.0  # seconds


class ClickHouseResource(ConfigurableResource):
    """Dagster resource wrapping a ClickHouse HTTP client.

    Defaults to shared.Settings (env vars). Override host/port via Dagster
    config to target a different instance (e.g. in tests).

    Dev:  CLICKHOUSE_HOST=localhost via SSH tunnel
    Prod: CLICKHOUSE_HOST=clickhouse via Docker network
    """

    host: str = Field(default="")
    port: int = Field(default=0)

    def _client(self) -> Client:
        return clickhouse_connect.get_client(
            host=self.host or settings.CLICKHOUSE_HOST,
            port=self.port or settings.CLICKHOUSE_PORT,
            compress=False,
        )

    def execute(self, query: str, parameters: dict[str, Any] | None = None) -> Any:
        """Execute a SQL query and return the QueryResult."""
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return self._client().query(query, parameters=parameters)
            except Exception as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "ClickHouse execute failed (attempt %d/%d): %s — retrying in %.1fs",
                        attempt,
                        _MAX_RETRIES,
                        exc,
                        _RETRY_DELAY,
                    )
                    time.sleep(_RETRY_DELAY)
        raise RuntimeError(f"ClickHouse execute failed after {_MAX_RETRIES} attempts") from last_exc

    def insert_df(self, table: str, df: pd.DataFrame) -> None:
        """Insert a pandas DataFrame into ClickHouse.

        Args:
            table: Fully-qualified table name, e.g. 'equity_raw.ohlcv_raw'
            df: DataFrame whose columns match the target table schema
        """
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                self._client().insert_df(table, df)
                return
            except Exception as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "ClickHouse insert_df failed (attempt %d/%d): %s — retrying in %.1fs",
                        attempt,
                        _MAX_RETRIES,
                        exc,
                        _RETRY_DELAY,
                    )
                    time.sleep(_RETRY_DELAY)
        raise RuntimeError(
            f"ClickHouse insert_df failed after {_MAX_RETRIES} attempts"
        ) from last_exc
