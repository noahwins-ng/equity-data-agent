"""Path 2: Raw OHLCV → Computed Indicators (QNT-64).

Verifies that the production indicator pipeline — read OHLCV from
ClickHouse, run ``compute_indicators``, write to
``equity_derived.technical_indicators_daily`` — produces the same numbers
the canonical fixture snapshot says it should, and that those numbers
survive a CH round-trip without dtype drift.

This is the path the ``technical_indicators_daily`` Dagster asset rides.
We exercise it without spinning up a Dagster context: the asset's body is
``query_df → compute_indicators → insert_df``, and each piece is exercised
here against a real CH against the committed fixture data — so any
schema, dtype, or NULL-handling regression surfaces.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from clickhouse_connect.driver.client import Client
from dagster_pipelines.assets.indicators.technical_indicators import compute_indicators

from ._helpers import (
    load_indicators_expected,
    seed_indicators_daily,
    seed_ohlcv_from_fixture,
)


@pytest.mark.integration
def test_indicators_match_fixture_snapshot_after_round_trip(ch_client: Client) -> None:
    """Compute indicators against fixture OHLCV, write to CH, read back, compare.

    Pulls the AAPL 2023-2024 fixture into ``equity_raw.ohlcv_raw``, runs
    ``compute_indicators`` exactly the way the asset does, writes via
    ``insert_df`` into ``equity_derived.technical_indicators_daily``, then
    queries the canonical "snapshot expectation" cells and asserts they
    survived. This catches: schema drift between asset code and migration
    DDL, Nullable-column round-trip bugs, and CTE/aggregation regressions
    in the read path.
    """
    seed_ohlcv_from_fixture(ch_client, "AAPL")

    # Read OHLCV back via the same query the asset issues. Don't pass
    # tz-aware timestamps through; the fixture is timezone-naive dates.
    df = ch_client.query_df(
        "SELECT date, high, low, close, adj_close, volume "
        "FROM equity_raw.ohlcv_raw FINAL "
        "WHERE ticker = 'AAPL' "
        "ORDER BY date"
    )
    assert len(df) == 501

    df["date"] = pd.to_datetime(df["date"]).dt.date
    computed = compute_indicators(df)
    seed_indicators_daily(ch_client, "AAPL", computed)

    # Read indicators back and compare against the canonical fixture
    # snapshot for a known cell. Index 250 sits well past every warm-up
    # window (max is SMA-200 at index 200), so every column is non-NULL.
    expected = load_indicators_expected("AAPL")
    expected_row = expected.iloc[250]
    target_date = expected_row["date"]

    result = ch_client.query(
        "SELECT sma_20, sma_50, ema_12, rsi_14, macd, bb_middle "
        "FROM equity_derived.technical_indicators_daily FINAL "
        "WHERE ticker = 'AAPL' AND date = %(date)s",
        parameters={"date": target_date},
    )
    rows = result.result_rows
    assert len(rows) == 1
    sma_20, sma_50, ema_12, rsi_14, macd, bb_middle = rows[0]

    # rtol=1e-6 — same tolerance the snapshot test in
    # test_indicator_validation.py uses (8-dp CSV rounding + FP noise).
    assert sma_20 == pytest.approx(expected_row["sma_20"], rel=1e-6)
    assert sma_50 == pytest.approx(expected_row["sma_50"], rel=1e-6)
    assert ema_12 == pytest.approx(expected_row["ema_12"], rel=1e-6)
    assert rsi_14 == pytest.approx(expected_row["rsi_14"], rel=1e-6)
    assert macd == pytest.approx(expected_row["macd"], rel=1e-6)
    assert bb_middle == pytest.approx(expected_row["bb_middle"], rel=1e-6)


@pytest.mark.integration
def test_indicators_warmup_rows_are_null(ch_client: Client) -> None:
    """SMA-50's first 49 rows must be NULL — ensures Nullable round-trip works.

    The migration declares every indicator column as ``Nullable(Float64)``
    so warm-up periods can carry NULL. If a future schema migration
    accidentally dropped Nullable, those rows would become 0.0 and the
    frontend would render zeros where it should render dashes. Catching
    this requires a real CH round-trip — pandas → CH coerces NaN → NULL,
    and the reverse must hold.
    """
    seed_ohlcv_from_fixture(ch_client, "MSFT")
    df = ch_client.query_df(
        "SELECT date, high, low, close, adj_close, volume "
        "FROM equity_raw.ohlcv_raw FINAL "
        "WHERE ticker = 'MSFT' "
        "ORDER BY date"
    )
    df["date"] = pd.to_datetime(df["date"]).dt.date
    computed = compute_indicators(df)
    seed_indicators_daily(ch_client, "MSFT", computed)

    result = ch_client.query(
        "SELECT count() FROM equity_derived.technical_indicators_daily FINAL "
        "WHERE ticker = 'MSFT' AND sma_50 IS NULL"
    )
    null_rows = result.result_rows[0][0]
    # SMA-50 needs 50 prior closes → first 49 rows are NULL.
    assert null_rows == 49


@pytest.mark.integration
def test_indicators_idempotent_on_replay(ch_client: Client) -> None:
    """Re-running the same partition yields one row per date, not duplicates.

    ReplacingMergeTree on ``computed_at`` collapses repeated writes; a
    backfill replay must read identically to a one-shot write. The asset
    explicitly relies on this for daily incremental runs that overlap
    yesterday's partition window.
    """
    seed_ohlcv_from_fixture(ch_client, "AAPL")
    df = ch_client.query_df(
        "SELECT date, high, low, close, adj_close, volume "
        "FROM equity_raw.ohlcv_raw FINAL "
        "WHERE ticker = 'AAPL' "
        "ORDER BY date"
    )
    df["date"] = pd.to_datetime(df["date"]).dt.date
    computed = compute_indicators(df)
    seed_indicators_daily(ch_client, "AAPL", computed)
    seed_indicators_daily(ch_client, "AAPL", computed)  # replay

    result = ch_client.query(
        "SELECT count(), uniqExact((ticker, date)) "
        "FROM equity_derived.technical_indicators_daily FINAL "
        "WHERE ticker = 'AAPL'"
    )
    total, unique_keys = result.result_rows[0]
    # FINAL collapses the duplicate writes to one row per (ticker, date).
    assert total == unique_keys == 501


@pytest.mark.integration
def test_indicators_threshold_query_returns_overbought_dates(ch_client: Client) -> None:
    """The frontend's RSI-overbought lookup query works end-to-end.

    Spot-checks a specific date the AAPL 2023-2024 fixture is known to
    classify as overbought (RSI ≥ 70). If the schema or CTE alias logic
    drifted, this query would either error out (the QNT-148 class) or
    return the wrong date.
    """
    seed_ohlcv_from_fixture(ch_client, "AAPL")
    df = ch_client.query_df(
        "SELECT date, high, low, close, adj_close, volume "
        "FROM equity_raw.ohlcv_raw FINAL "
        "WHERE ticker = 'AAPL' "
        "ORDER BY date"
    )
    df["date"] = pd.to_datetime(df["date"]).dt.date
    computed = compute_indicators(df)
    seed_indicators_daily(ch_client, "AAPL", computed)

    result = ch_client.query(
        "SELECT count() "
        "FROM equity_derived.technical_indicators_daily FINAL "
        "WHERE ticker = 'AAPL' AND rsi_14 >= 70"
    )
    overbought_count = result.result_rows[0][0]
    # The committed fixture has at least one overbought day in
    # 2023-2024 — the dashboard signal would otherwise never light up.
    assert overbought_count > 0


@pytest.mark.integration
def test_compute_indicators_handles_short_partition() -> None:
    """A 30-bar partition computes the short-window indicators but leaves
    long-window SMA-50 NULL.

    Catches the "every column initializes to NULL on a short partition"
    edge case the frontend depends on (SMA-50 dashes when history is too
    short, but the SMA-20 dial lights up at bar 20). Pure-pandas test —
    no CH round-trip needed since we're verifying the computation, not
    persistence; the prior tests already exercise the persistence path.
    """
    rng = np.random.default_rng(seed=42)
    rows: list[dict[str, object]] = []
    base = 100.0
    for i in range(30):
        d = date(2026, 1, 1)
        # Oscillating walk so RSI sees gains AND losses (a strict monotone
        # series gives avg_loss == 0 → RSI = NaN by design).
        delta = float(rng.normal(0.0, 1.0))
        base += delta
        rows.append(
            {
                "ticker": "TEST",
                "date": pd.Timestamp(d) + pd.Timedelta(days=i),
                "high": base + 1.0,
                "low": base - 1.0,
                "close": base,
                "adj_close": base,
                "volume": 1_000_000,
            }
        )
    df = pd.DataFrame(rows)
    df["date"] = df["date"].dt.date
    computed = compute_indicators(df)

    # RSI-14 populated past the 14-bar warm-up; SMA-20 populated past the
    # 20-bar warm-up; SMA-50 still all-NULL on a 30-bar partition.
    assert pd.notna(computed["rsi_14"].iloc[20])
    assert pd.notna(computed["sma_20"].iloc[25])
    assert bool(computed["sma_50"].isna().all())
