"""Fixture-loading helpers for QNT-64 integration tests.

Each ``seed_*`` function takes a real ClickHouse client and inserts a small,
deterministic batch of rows into the production table the asset would have
written to. Tests then exercise the downstream code path (API endpoint,
indicator computation, agent tool) against that fixture data.

Functions are intentionally tiny and do no math — the production code is
under test, so seeding helpers must not duplicate calculations that the
tests are meant to verify.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from clickhouse_connect.driver.client import Client

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures"


def load_ohlcv_fixture(ticker: str) -> pd.DataFrame:
    """Read the committed 2023-2024 OHLCV CSV for ``ticker``.

    Available tickers: AAPL, MSFT (matches
    ``tests/fixtures/indicators/<TICKER>_ohlcv_2023_2024.csv``). Returned
    DataFrame columns: date, open, high, low, close, adj_close, volume.
    """
    df = pd.read_csv(_FIXTURES_DIR / "indicators" / f"{ticker}_ohlcv_2023_2024.csv")
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def load_indicators_expected(ticker: str) -> pd.DataFrame:
    """Read the committed expected-indicator snapshot CSV for ``ticker``."""
    df = pd.read_csv(_FIXTURES_DIR / "indicators" / f"{ticker}_indicators_expected.csv")
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def seed_ohlcv_from_fixture(client: Client, ticker: str) -> int:
    """Insert the full 2023-2024 OHLCV fixture for ``ticker`` and return rowcount.

    Uses ``client.insert_df`` — same path the Dagster ``ohlcv_raw`` asset
    uses (via ClickHouseResource), so the column-shape contract is exercised
    too, not just the round-trip.
    """
    df = load_ohlcv_fixture(ticker)
    df["ticker"] = ticker
    df["volume"] = df["volume"].astype("int64")
    df["fetched_at"] = datetime.utcnow()
    cols = ["ticker", "date", "open", "high", "low", "close", "adj_close", "volume", "fetched_at"]
    df = pd.DataFrame(df[cols])
    client.insert_df("equity_raw.ohlcv_raw", df)
    return len(df)


def seed_synthetic_ohlcv(
    client: Client,
    ticker: str,
    *,
    days: int = 60,
    start_date: date | None = None,
    base_price: float = 100.0,
) -> pd.DataFrame:
    """Insert a synthetic monotonic OHLCV walk and return the seeded frame.

    Useful for tests that need a known, small dataset: the price increments
    by 1.0 per day so quote / dashboard / OHLCV endpoint outputs are easy
    to assert on without committing yet another fixture file.
    """
    start = start_date or (date.today() - timedelta(days=days))
    rows: list[dict[str, Any]] = []
    for i in range(days):
        d = start + timedelta(days=i)
        # Skip weekends so the data shape matches a real exchange feed (the
        # quote endpoint computes "30 prior bars" and the cleaner the bar
        # cadence, the easier the test invariants are to write).
        if d.weekday() >= 5:
            continue
        price = base_price + i
        rows.append(
            {
                "ticker": ticker,
                "date": d,
                "open": price,
                "high": price + 1.0,
                "low": price - 1.0,
                "close": price + 0.5,
                "adj_close": price + 0.5,
                "volume": 1_000_000 + i * 1000,
                "fetched_at": datetime.utcnow(),
            }
        )
    df = pd.DataFrame(rows)
    client.insert_df("equity_raw.ohlcv_raw", df)
    return df


def seed_fundamentals(client: Client, ticker: str) -> int:
    """Insert the committed synthetic-fundamentals fixture for ``ticker``.

    The fixture lives at ``tests/fixtures/fundamentals/synthetic_fundamentals.csv``
    and is the same one ``test_fundamental_ratios`` relies on for ratio
    snapshots. Re-using it here keeps the integration test referenced
    against a known shape.
    """
    df = pd.read_csv(_FIXTURES_DIR / "fundamentals" / "synthetic_fundamentals.csv")
    df["ticker"] = ticker
    df["period_end"] = pd.to_datetime(df["period_end"]).dt.date
    df["fetched_at"] = datetime.utcnow()
    df["shares_outstanding"] = df["shares_outstanding"].astype("int64")
    cols = [
        "ticker",
        "period_end",
        "period_type",
        "revenue",
        "gross_profit",
        "net_income",
        "total_assets",
        "total_liabilities",
        "current_assets",
        "current_liabilities",
        "free_cash_flow",
        "ebitda",
        "total_debt",
        "cash_and_equivalents",
        "shares_outstanding",
        "market_cap",
        "fetched_at",
    ]
    df = pd.DataFrame(df[cols])
    # Give every row a non-zero market cap so the quote endpoint surfaces a
    # value (the fixture's market_cap is all 0 by design — fundamental_summary
    # is what consumers usually read; quote is the only path that looks at
    # raw market_cap directly).
    df["market_cap"] = df["revenue"] * 5.0
    client.insert_df("equity_raw.fundamentals", df)
    return len(df)


def _summary_row(
    ticker: str,
    period_end: date,
    period_type: str,
) -> dict[str, Any]:
    """Build one ``fundamental_summary`` row with non-null ratios.

    Numbers are arbitrary but realistic-looking — the report templates
    only render and threshold them, so the test asserts on the rendered
    shape rather than the magnitudes themselves.
    """
    return {
        "ticker": ticker,
        "period_end": period_end,
        "period_type": period_type,
        "pe_ratio": 25.0,
        "ev_ebitda": 18.0,
        "price_to_book": 5.0,
        "price_to_sales": 4.0,
        "eps": 6.0,
        "revenue_yoy_pct": 12.0,
        "net_income_yoy_pct": 15.0,
        "fcf_yoy_pct": 10.0,
        "net_margin_pct": 22.0,
        "gross_margin_pct": 45.0,
        "ebitda_margin_pct": 30.0,
        "gross_margin_bps_yoy": 100,
        "net_margin_bps_yoy": 80,
        "roe": 35.0,
        "roa": 12.0,
        "fcf_yield": 4.5,
        "debt_to_equity": 1.5,
        "current_ratio": 1.8,
        "revenue_ttm": 100_000_000.0,
        "net_income_ttm": 22_000_000.0,
        "fcf_ttm": 30_000_000.0,
        "computed_at": datetime.utcnow(),
    }


def seed_fundamental_summary(client: Client, ticker: str) -> int:
    """Insert quarterly + TTM ``fundamental_summary`` rows for ``ticker``.

    Quarterly rows are required by the fundamental-report template
    (``WHERE period_type = 'quarterly'``); TTM rows feed the quote and
    fundamentals data endpoints. Two quarterly rows so the report's
    "trend acceleration vs prior" logic has something to compare.
    """
    rows = [
        _summary_row(ticker, date(2024, 9, 30), "quarterly"),
        _summary_row(ticker, date(2024, 12, 31), "quarterly"),
        _summary_row(ticker, date(2024, 12, 31), "ttm"),
    ]
    df = pd.DataFrame(rows)
    client.insert_df("equity_derived.fundamental_summary", df)
    return len(df)


def seed_news(client: Client, ticker: str, *, count: int = 5) -> int:
    """Insert ``count`` recent news rows for ``ticker``.

    Headlines are deterministic strings keyed on index so tests can assert
    on specific cells without depending on Finnhub's wording. The
    ``published_at`` timestamps are spaced 1 hour apart, all within the
    last 24h so the default 7-day news window picks them up.
    """
    now = datetime.utcnow()
    rows = []
    for i in range(count):
        rows.append(
            {
                "id": 1_000_000 + i,
                "ticker": ticker,
                "headline": f"{ticker} headline {i}",
                "body": f"Body for {ticker} headline {i}.",
                "publisher_name": "TestWire",
                "image_url": "",
                "source": "test",
                "url": f"https://example.com/{ticker}/{i}",
                "published_at": now - timedelta(hours=i),
                "sentiment_label": "neutral",
                "resolved_host": "example.com",
                "fetched_at": now,
            }
        )
    df = pd.DataFrame(rows)
    client.insert_df("equity_raw.news_raw", df)
    return len(df)


def seed_indicators_daily(client: Client, ticker: str, df: pd.DataFrame) -> int:
    """Write a computed-indicator DataFrame to ``technical_indicators_daily``.

    Caller computes via ``compute_indicators`` (the production function);
    this helper only handles the column projection + insert step the asset
    does. Coerces ``macd_bullish_cross`` to UInt8 the same way the asset
    does (``_coerce_for_clickhouse``).
    """
    cols = (
        "ticker",
        "date",
        "sma_20",
        "sma_50",
        "sma_200",
        "ema_12",
        "ema_26",
        "rsi_14",
        "macd",
        "macd_signal",
        "macd_hist",
        "macd_bullish_cross",
        "bb_upper",
        "bb_middle",
        "bb_lower",
        "bb_pct_b",
        "adx_14",
        "atr_14",
        "obv",
        "computed_at",
    )
    out = df.copy()
    out["ticker"] = ticker
    out["computed_at"] = datetime.utcnow()
    if "macd_bullish_cross" in out.columns:
        out["macd_bullish_cross"] = out["macd_bullish_cross"].fillna(False).astype("uint8")
    else:
        out["macd_bullish_cross"] = 0
    # Range-based indicators are only emitted when the OHLC frame carries
    # high/low/close/volume; tests using the legacy two-column pipeline can
    # still seed via this helper by leaving the missing columns as NaN.
    for col in ("sma_200", "adx_14", "atr_14", "obv", "bb_pct_b"):
        if col not in out.columns:
            out[col] = pd.NA
    out = pd.DataFrame(out[list(cols)])
    client.insert_df("equity_derived.technical_indicators_daily", out)
    return len(out)
