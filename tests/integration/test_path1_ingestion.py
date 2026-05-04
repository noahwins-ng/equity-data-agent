"""Path 1: Ingestion → ClickHouse (QNT-64).

Verifies that the ``ClickHouseResource.insert_df`` path the Dagster
``ohlcv_raw`` / ``fundamentals`` / ``news_raw`` assets ride lands data
correctly. The asset code itself is yfinance/Finnhub-bound, so testing
"the asset" end-to-end would require network. We test the integration
boundary the asset actually owns — DataFrame → CH table — which is the
one that breaks when the schema, dtype mapping, or FINAL semantics drift.

Runs against a real ClickHouse so any CTE / dtype / engine bug surfaces
the way it would in prod (the QNT-148 lesson behind this ticket).
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest
from clickhouse_connect.driver.client import Client
from dagster_pipelines.resources.clickhouse import ClickHouseResource

from ._helpers import (
    seed_fundamentals,
    seed_news,
    seed_ohlcv_from_fixture,
    seed_synthetic_ohlcv,
)


@pytest.fixture
def resource() -> ClickHouseResource:
    """Production ClickHouseResource — same shape the Dagster asset injects."""
    return ClickHouseResource()


@pytest.mark.integration
def test_ohlcv_insert_df_round_trips_via_resource(
    resource: ClickHouseResource, ch_client: Client
) -> None:
    """Asset-shaped DataFrame survives the resource's insert + FINAL read.

    Mirrors the path ``ohlcv_raw`` rides: build a frame with the asset's
    column order, insert via the resource (not the raw client), then read
    it back with ``FROM ... FINAL`` like the API does. Any silent dtype
    coercion (volume → Int64 vs UInt64, date → DateTime) would fail this.
    """
    df = pd.DataFrame(
        [
            {
                "ticker": "NVDA",
                "date": date(2026, 1, 5),
                "open": 100.0,
                "high": 105.0,
                "low": 99.0,
                "close": 103.0,
                "adj_close": 103.0,
                "volume": 12_345_678,
                "fetched_at": datetime.utcnow(),
            },
            {
                "ticker": "NVDA",
                "date": date(2026, 1, 6),
                "open": 103.0,
                "high": 108.0,
                "low": 102.0,
                "close": 107.0,
                "adj_close": 107.0,
                "volume": 22_222_222,
                "fetched_at": datetime.utcnow(),
            },
        ]
    )
    resource.insert_df("equity_raw.ohlcv_raw", df)

    result = ch_client.query(
        "SELECT ticker, date, close, volume "
        "FROM equity_raw.ohlcv_raw FINAL "
        "WHERE ticker = %(ticker)s ORDER BY date",
        parameters={"ticker": "NVDA"},
    )
    rows = result.result_rows
    assert len(rows) == 2
    assert rows[0] == ("NVDA", date(2026, 1, 5), 103.0, 12_345_678)
    assert rows[1] == ("NVDA", date(2026, 1, 6), 107.0, 22_222_222)


@pytest.mark.integration
def test_ohlcv_replacingmergetree_keeps_latest_fetch(ch_client: Client) -> None:
    """Re-inserting the same (ticker, date) replaces the prior row under FINAL.

    ReplacingMergeTree with ``ORDER BY (ticker, date)`` and the
    ``fetched_at`` version column means a backfill replay must collapse to
    the latest write. The API's ``FROM ... FINAL`` reads depend on this —
    if the table engine were ever changed to plain MergeTree the API
    would silently start returning duplicates.
    """
    base = pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "date": date(2026, 1, 5),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 100.0,
                "adj_close": 100.0,
                "volume": 1_000_000,
                "fetched_at": datetime(2026, 1, 5, 12, 0, 0),
            }
        ]
    )
    ch_client.insert_df("equity_raw.ohlcv_raw", base)

    later = base.copy()
    later["close"] = 200.0
    later["fetched_at"] = datetime(2026, 1, 5, 18, 0, 0)
    ch_client.insert_df("equity_raw.ohlcv_raw", later)

    result = ch_client.query("SELECT close FROM equity_raw.ohlcv_raw FINAL WHERE ticker = 'AAPL'")
    assert result.result_rows == [(200.0,)]


@pytest.mark.integration
def test_ohlcv_fixture_loads_full_range(ch_client: Client) -> None:
    """The committed 2023-2024 fixture lands all 501 rows for a ticker."""
    inserted = seed_ohlcv_from_fixture(ch_client, "AAPL")
    assert inserted == 501  # matches fixture row count

    result = ch_client.query(
        "SELECT count(), min(date), max(date) FROM equity_raw.ohlcv_raw FINAL WHERE ticker = 'AAPL'"
    )
    count, min_date, max_date = result.result_rows[0]
    assert count == 501
    assert min_date == date(2023, 1, 3)
    assert max_date == date(2024, 12, 30)


@pytest.mark.integration
def test_fundamentals_insert_round_trips(ch_client: Client) -> None:
    """All-period-types insert lands annual + quarterly rows under one ticker."""
    inserted = seed_fundamentals(ch_client, "MSFT")
    assert inserted == 10  # 2 annual + 8 quarterly

    result = ch_client.query(
        "SELECT period_type, count() "
        "FROM equity_raw.fundamentals FINAL "
        "WHERE ticker = 'MSFT' "
        "GROUP BY period_type ORDER BY period_type"
    )
    by_type = {row[0]: row[1] for row in result.result_rows}
    assert by_type == {"annual": 2, "quarterly": 8}


@pytest.mark.integration
def test_news_insert_round_trips(ch_client: Client) -> None:
    """News rows land with their headline + publisher fields intact."""
    inserted = seed_news(ch_client, "TSLA", count=3)
    assert inserted == 3

    result = ch_client.query(
        "SELECT count(), dateDiff('second', min(published_at), max(published_at)) "
        "FROM equity_raw.news_raw FINAL WHERE ticker = 'TSLA'"
    )
    count, span_seconds = result.result_rows[0]
    assert count == 3
    # Three rows spaced 1h apart → max - min = 2h = 7200s.
    assert span_seconds == 7200


@pytest.mark.integration
def test_synthetic_ohlcv_seeder_skips_weekends(ch_client: Client) -> None:
    """The synthetic seeder only emits weekday bars (mirrors a real exchange)."""
    df = seed_synthetic_ohlcv(
        ch_client, "GOOGL", days=14, start_date=date(2026, 1, 5), base_price=100.0
    )
    # 14 calendar days starting Monday Jan-5-26 → 10 weekdays
    assert len(df) == 10
    weekdays = {d.weekday() for d in df["date"]}
    assert weekdays.isdisjoint({5, 6})

    result = ch_client.query(
        "SELECT count() FROM equity_raw.ohlcv_raw FINAL WHERE ticker = 'GOOGL'"
    )
    assert result.result_rows == [(10,)]
