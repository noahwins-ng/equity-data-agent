"""One-time 1y backfill of equity_raw.news_raw from Finnhub /company-news (QNT-141).

ADR-015 picked Finnhub `/company-news` over Yahoo RSS. Free tier explicitly
allows 1y of history (verified `freeTier: "1 year of historical news and new
updates"` in the docs JSON). This script does the cutover backfill, chunked
by month per ticker so memory + per-call payload stay bounded.

Steady-state ingestion (4h schedule) handles everything from cutover-day
forward; this script only runs once at cutover (or on disaster recovery to
re-hydrate from upstream).

Usage (from repo root, with FINNHUB_API_KEY in .env or env):
    uv run --package dagster-pipelines python scripts/backfill_finnhub_news.py
    uv run --package dagster-pipelines python scripts/backfill_finnhub_news.py --tickers NVDA,AAPL
    uv run --package dagster-pipelines python scripts/backfill_finnhub_news.py --months 6

Notes:
    * Re-runs are idempotent — ReplacingMergeTree dedups on
      (ticker, published_at, id). Re-running the script the next day is safe;
      it will just re-write today's rows with a fresher fetched_at.
    * Each (ticker, month) pair is one HTTP call to Finnhub. With defaults
      (10 tickers × 12 months = 120 calls), and an inter-call sleep of
      ~1.5s, the full backfill takes ~3 minutes. Well clear of Finnhub's
      60 RPM ceiling.
    * The sentiment_label column lands `pending`; QNT-131's classifier picks
      these rows up on its first run.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from calendar import monthrange
from datetime import date
from typing import Any

import pandas as pd
from clickhouse_connect import get_client
from clickhouse_connect.driver.client import Client
from dagster_pipelines.assets.news_raw import _article_to_row
from dagster_pipelines.news_feeds import FinnhubAPIKeyMissing, fetch_company_news
from shared.config import settings
from shared.tickers import TICKERS

logger = logging.getLogger("backfill_finnhub_news")

# Finnhub free tier: 60 RPM. With ~1.5s between calls we stay well under.
_INTER_CALL_SLEEP_SECONDS = 1.5


def _month_windows(months_back: int, today: date) -> list[tuple[date, date]]:
    """Return [(from, to), ...] inclusive month windows ending at ``today``.

    Windows go in chronological order so resumed runs land older articles
    first — useful when watching the run live and wanting to verify density
    before committing to the full sweep.
    """
    windows: list[tuple[date, date]] = []
    cursor_year, cursor_month = today.year, today.month
    # Walk back N months from today.
    for _ in range(months_back):
        last_day = monthrange(cursor_year, cursor_month)[1]
        end = date(cursor_year, cursor_month, last_day)
        start = date(cursor_year, cursor_month, 1)
        windows.append((start, end))
        # Step back one month.
        cursor_month -= 1
        if cursor_month == 0:
            cursor_month = 12
            cursor_year -= 1
    # Trim the most recent window to today (don't ask Finnhub for "future").
    if windows and windows[0][1] > today:
        windows[0] = (windows[0][0], today)
    return list(reversed(windows))


def _clickhouse_client() -> Client:
    return get_client(
        host=settings.CLICKHOUSE_HOST,
        port=settings.CLICKHOUSE_PORT,
        compress=False,
    )


def _backfill_window(
    ticker: str,
    from_date: date,
    to_date: date,
    client: Client,
) -> int:
    """Fetch one (ticker, window) pair and insert resulting rows. Returns row count."""
    articles = fetch_company_news(ticker, from_date=from_date, to_date=to_date)
    rows: list[dict[str, Any]] = []
    for article in articles:
        row = _article_to_row(article, ticker=ticker)
        if row is not None:
            rows.append(row)
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    df["id"] = df["id"].astype("uint64")
    cols = [
        "id",
        "ticker",
        "headline",
        "body",
        "source",
        "url",
        "published_at",
        "fetched_at",
        "publisher_name",
        "image_url",
        "sentiment_label",
    ]
    df = pd.DataFrame(df[cols])
    client.insert_df("equity_raw.news_raw", df)
    return len(df)


def _run(tickers: list[str], months: int) -> int:
    """Run the backfill. Returns total rows inserted across all (ticker, window) pairs."""
    today = date.today()
    windows = _month_windows(months, today)

    logger.info(
        "Starting Finnhub backfill: %d tickers x %d months = %d HTTP calls "
        "(~%ds wall-clock with %.1fs sleep between)",
        len(tickers),
        len(windows),
        len(tickers) * len(windows),
        int(len(tickers) * len(windows) * _INTER_CALL_SLEEP_SECONDS),
        _INTER_CALL_SLEEP_SECONDS,
    )

    client = _clickhouse_client()
    total = 0

    for ticker in tickers:
        ticker_total = 0
        for from_date, to_date in windows:
            try:
                inserted = _backfill_window(ticker, from_date, to_date, client)
                ticker_total += inserted
                logger.info(
                    "  %s [%s..%s]: %d rows",
                    ticker,
                    from_date.isoformat(),
                    to_date.isoformat(),
                    inserted,
                )
            except Exception as exc:  # noqa: BLE001 — surface as warn, continue
                logger.warning(
                    "Failed %s [%s..%s]: %s — continuing",
                    ticker,
                    from_date.isoformat(),
                    to_date.isoformat(),
                    exc,
                )
            finally:
                # Always rate-limit, including on errors. A 429 without backoff
                # would compound: every subsequent call in the loop would also
                # 429 because we'd be hammering immediately.
                time.sleep(_INTER_CALL_SLEEP_SECONDS)
        logger.info("%s done — %d rows total", ticker, ticker_total)
        total += ticker_total

    logger.info("Backfill complete — %d rows inserted across %d tickers", total, len(tickers))
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--tickers",
        default=",".join(TICKERS),
        help="Comma-separated tickers to backfill (default: shared.tickers.TICKERS)",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=12,
        help="Number of trailing months to backfill (default: 12, i.e. 1y)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    requested = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    unknown = [t for t in requested if t not in TICKERS]
    if unknown:
        logger.error("Unknown tickers (not in shared.tickers.TICKERS): %s", unknown)
        return 2

    try:
        _run(requested, args.months)
    except FinnhubAPIKeyMissing as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
