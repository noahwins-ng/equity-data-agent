"""One-shot verification that RSS feeds return headlines for all 10 tickers.

Run locally with:
    uv run --package dagster-pipelines python scripts/verify_news_feeds.py

Per QNT-52 acceptance criteria: at least 8/10 tickers must return non-empty
headlines, and transient feed errors (404, parse errors) must not crash.

The actual Dagster ingestion asset is QNT-53; this script exists only to prove
feedparser + Yahoo's RSS surface work at this ticker scope before wiring them
into the pipeline.
"""

from __future__ import annotations

import sys

import feedparser
from dagster_pipelines.news_feeds import MARKET_FEEDS, all_ticker_feeds


def fetch_headlines(url: str) -> tuple[int, str]:
    """Parse an RSS feed. Returns (entry_count, status). Never raises."""
    try:
        parsed = feedparser.parse(url, request_headers={"User-Agent": "equity-data-agent/0.1"})
    except Exception as exc:
        return 0, f"error: {type(exc).__name__}: {exc}"

    if parsed.bozo and not parsed.entries:
        reason = getattr(parsed, "bozo_exception", "unknown parse error")
        return 0, f"parse-error: {reason}"

    http_status = parsed.get("status", "unknown")
    return len(parsed.entries), f"http {http_status}"


def main() -> int:
    print("=== Per-ticker Yahoo Finance RSS feeds ===")
    ticker_results: dict[str, int] = {}
    for ticker, url in all_ticker_feeds().items():
        count, status = fetch_headlines(url)
        ticker_results[ticker] = count
        marker = "OK " if count > 0 else "MISS"
        print(f"  [{marker}] {ticker:5s} — {count:2d} entries  ({status})")

    print("\n=== Broad market feeds ===")
    for name, url in MARKET_FEEDS.items():
        count, status = fetch_headlines(url)
        marker = "OK " if count > 0 else "MISS"
        print(f"  [{marker}] {name:16s} — {count:2d} entries  ({status})")

    non_empty = sum(1 for n in ticker_results.values() if n > 0)
    total = len(ticker_results)
    print(f"\nSummary: {non_empty}/{total} tickers returned non-empty headlines")

    threshold = 8
    if non_empty < threshold:
        print(f"FAIL: need at least {threshold}/{total} non-empty ticker feeds")
        return 1
    print(f"PASS: met {threshold}/{total} threshold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
