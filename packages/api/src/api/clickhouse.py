"""Thin ClickHouse client wrapper shared by all report endpoints.

The wrapper exists so endpoints don't each reinstantiate a connection and so
every query gets the ``FROM <table> FINAL`` treatment for ReplacingMergeTree
consistency (ADR-001).
"""

from __future__ import annotations

from functools import lru_cache

import clickhouse_connect
from clickhouse_connect.driver import Client
from shared.config import settings


@lru_cache(maxsize=1)
def get_client() -> Client:
    """Return a process-wide ClickHouse client, created lazily on first use."""
    return clickhouse_connect.get_client(
        host=settings.CLICKHOUSE_HOST,
        port=settings.CLICKHOUSE_PORT,
        connect_timeout=3,
        query_limit=0,
    )
