"""Thin ClickHouse client wrapper shared by all report endpoints.

The wrapper exists so endpoints don't each reinstantiate a connection and so
every query gets the ``FROM <table> FINAL`` treatment for ReplacingMergeTree
consistency (ADR-001).

Concurrency: ``clickhouse-connect``'s HttpClient is NOT safe for concurrent
queries on a single session — when two FastAPI request threads hit the same
cached client at the same time, ClickHouse rejects the second with::

    Attempt to execute concurrent queries within the same session.

The Phase-6 ticker page surfaces this routinely because the chart, technicals,
and fundamentals client components all fire their fetches in parallel after
hydration (5+ concurrent requests). We pass ``autogenerate_session_id=False``
so each query runs on a fresh server-side session, removing the contention
without losing the urllib3 connection pool / keep-alive that comes with a
shared client object.
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
        autogenerate_session_id=False,
    )
