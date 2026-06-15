"""Corporate-action OHLCV refresh — Option A mechanism (QNT-235).

Two surfaces are pinned:

1. The monthly schedule fans out one run per ticker at period="2y" — the full
   window that re-fetches (and thus re-adjusts) the *entire* stored history.
   This is the trigger of the correction; if it ever narrows back toward the
   daily period="5d" window the splice bug returns silently.

2. The correction invariant: given a stored series corrupted by a split splice
   (pre-action raw prices spliced onto a post-action adjusted tail), a full
   period="2y" overwrite carrying a newer fetched_at, deduplicated the way
   ReplacingMergeTree(fetched_at) + FINAL does on (ticker, date), yields a
   self-consistent series with no artificial jump at the split boundary.

AC3 proves the same invariant against real ClickHouse on NVDA; this test pins
it deterministically in CI without a tunnel.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any, cast

import pandas as pd
from dagster import RunRequest, build_schedule_context
from dagster_pipelines.schedules import ohlcv_monthly_refresh_schedule
from shared.tickers import ALL_OHLCV_TICKERS


def _period_of(run_config: Mapping[str, object]) -> str:
    """Pull the OHLCVConfig.period out of a RunRequest's run_config."""
    ops = cast("dict[str, Any]", run_config["ops"])
    return cast("str", ops["ohlcv_raw"]["config"]["period"])


def test_monthly_refresh_emits_full_window_for_all_tickers() -> None:
    """AC1/AC2: one run per ticker, every one at period>=2y (full history)."""
    context = build_schedule_context(scheduled_execution_time=datetime(2026, 2, 1, 6, 0))
    run_requests = list(cast("Iterable[RunRequest]", ohlcv_monthly_refresh_schedule(context)))

    tickers = sorted(str(rr.partition_key) for rr in run_requests)
    assert tickers == sorted(ALL_OHLCV_TICKERS)

    periods = {_period_of(rr.run_config) for rr in run_requests}
    assert periods == {"2y"}, f"expected every run at period=2y, got {periods}"


def _dedup_final(df: pd.DataFrame) -> pd.DataFrame:
    """Mimic ReplacingMergeTree(fetched_at) + FINAL on ORDER BY (ticker, date):
    keep the row with the greatest fetched_at per (ticker, date)."""
    return (
        df.sort_values("fetched_at")
        .groupby(["ticker", "date"], as_index=False)
        .last()
        .sort_values(["ticker", "date"])
        .reset_index(drop=True)
    )


def _max_abs_daily_return(adj_close: pd.Series) -> float:
    """Largest single-day fractional move — a 2:1 split splice shows up as a
    ~0.5 jump that no real trading day produces.

    Drops the leading NaN from pct_change(); a degenerate single-row input
    would otherwise return NaN and make a ``< threshold`` assert pass vacuously.
    """
    moves = adj_close.pct_change().abs().dropna()
    assert not moves.empty, "need >=2 rows to measure a daily return"
    return float(moves.max())


def test_full_refresh_corrects_split_splice() -> None:
    """AC2: a 2:1 split splice in the stored series is removed once a full
    period="2y" refetch overwrites every (ticker, date) under RMT semantics."""
    dates = [
        date(2026, 1, 2),
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 7),
        date(2026, 1, 8),
        date(2026, 1, 9),
    ]

    # Truth (what yfinance returns AFTER the 2:1 split, fully adjusted): smooth.
    adjusted = [50.0, 51.0, 52.0, 53.0, 54.0, 55.0]

    # Corrupted stored series: the first 4 days were fetched BEFORE the split at
    # ~2x unadjusted prices; the daily period="5d" incremental only rewrote the
    # last 2 days post-split → a ~50% cliff between day 4 and day 5.
    spliced = [100.0, 102.0, 104.0, 106.0, 54.0, 55.0]
    stored = pd.DataFrame(
        {
            "ticker": "NVDA",
            "date": dates,
            "adj_close": spliced,
            "fetched_at": pd.Timestamp("2026-01-09 17:00"),
        }
    )
    assert _max_abs_daily_return(pd.Series(stored["adj_close"])) > 0.4  # splice present

    # Monthly full refresh: entire window re-fetched, fully adjusted, newer ts.
    refreshed = pd.DataFrame(
        {
            "ticker": "NVDA",
            "date": dates,
            "adj_close": adjusted,
            "fetched_at": pd.Timestamp("2026-02-01 06:00"),
        }
    )

    final = _dedup_final(pd.concat([stored, refreshed], ignore_index=True))

    # One row per date (no duplicates), series equals the adjusted truth, and
    # the split cliff is gone.
    assert len(final) == len(dates)
    assert pd.Series(final["adj_close"]).tolist() == adjusted
    assert _max_abs_daily_return(pd.Series(final["adj_close"])) < 0.05
