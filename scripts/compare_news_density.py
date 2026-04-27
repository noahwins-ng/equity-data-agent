"""Side-by-side density check: Finnhub /company-news vs Yahoo Finance RSS (QNT-141).

ADR-015's verification recommendation: before flipping the schedule, count
Finnhub headlines vs Yahoo RSS for one ticker, one week, to confirm Finnhub
gives at least comparable density per ticker. This script produces that
artifact.

Usage:
    uv run --package dagster-pipelines python scripts/compare_news_density.py
    uv run --package dagster-pipelines python scripts/compare_news_density.py --ticker NVDA --days 7

Output is printed (no file write) so the user can paste into the cutover PR
description as the AC artifact.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date, timedelta

import feedparser
from dagster_pipelines.news_feeds import (
    FINNHUB_BASE_URL,
    FinnhubAPIKeyMissing,
    fetch_company_news,
)


def _yahoo_rss_count(ticker: str) -> tuple[int, str]:
    """Return (entry_count, status) for the Yahoo RSS feed of ``ticker``.

    RSS doesn't accept date filters; the feed window is whatever Yahoo decides.
    The number is for context only — direct day-comparison is not possible.
    """
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
    try:
        parsed = feedparser.parse(url, request_headers={"User-Agent": "equity-data-agent/0.1"})
    except Exception as exc:  # noqa: BLE001
        return 0, f"error: {type(exc).__name__}: {exc}"
    if parsed.bozo and not parsed.entries:
        reason = getattr(parsed, "bozo_exception", "unknown parse error")
        return 0, f"parse-error: {reason}"
    return len(parsed.entries), f"http {parsed.get('status', 'unknown')}"


def _finnhub_summary(ticker: str, days: int) -> tuple[int, Counter[str], int]:
    """Return (article_count, publisher_counter, image_count) for the last ``days``."""
    today = date.today()
    from_date = today - timedelta(days=days)
    articles = fetch_company_news(ticker, from_date=from_date, to_date=today)
    publishers: Counter[str] = Counter()
    image_count = 0
    for article in articles:
        publishers[(article.get("source") or "").strip() or "<empty>"] += 1
        if (article.get("image") or "").strip():
            image_count += 1
    return len(articles), publishers, image_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--ticker", default="NVDA", help="Ticker to probe (default: NVDA)")
    parser.add_argument(
        "--days", type=int, default=7, help="Trailing window for Finnhub (default: 7)"
    )
    args = parser.parse_args(argv)

    ticker = args.ticker.upper()
    print(f"=== News density: {ticker} (last {args.days} days) ===\n")

    print(f"Finnhub /company-news ({FINNHUB_BASE_URL}/company-news):")
    try:
        count, publishers, image_count = _finnhub_summary(ticker, args.days)
    except FinnhubAPIKeyMissing as exc:
        print(f"  FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"  {count} articles, {image_count} with images ({100 * image_count // max(count, 1)}%)")
    print(f"  Distinct publishers: {len(publishers)}")
    print("  Top publishers:")
    for publisher, n in publishers.most_common(10):
        print(f"    {n:3d}  {publisher}")

    print()
    print("Yahoo Finance RSS (no date filter — feed-defined window):")
    rss_count, rss_status = _yahoo_rss_count(ticker)
    print(f"  {rss_count} entries  ({rss_status})")
    print("  Note: RSS source field is 'Yahoo Finance' — no per-publisher attribution.")

    print()
    print("=== Verdict ===")
    if count == 0:
        print(f"  Finnhub returned 0 articles for {ticker}. Check FINNHUB_API_KEY + ticker scope.")
        return 2
    if count < rss_count // 2:
        print(
            f"  Finnhub density ({count}) is <50% of Yahoo RSS density ({rss_count}). "
            "Investigate before cutover."
        )
        return 3
    print(
        f"  Finnhub: {count} articles in {args.days}d, {len(publishers)} publishers. "
        "Density acceptable for cutover."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
