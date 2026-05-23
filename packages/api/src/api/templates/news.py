"""News report template — chronological headlines + source breakdown.

QNT-207 drops the per-headline sentiment surfacing that QNT-175 introduced:
the ``news_raw.sentiment_label`` column stays nullable in the warehouse but
mostly contains ``pending`` / ``neutral`` because we never shipped a real
classifier — surfacing those into the report added noise without signal.

Lookback widened 7 -> 14 days and headline cap 10 -> 20, so the synthesize
step sees a meatier slice of recent coverage to ground the thesis in.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from shared.tickers import TICKER_METADATA, TICKERS

from api.clickhouse import get_client

_MAX_HEADLINES = 20
_LOOKBACK_DAYS = 14
_BODY_SNIPPET_CHARS = 160


def _fetch_rows(ticker: str) -> list[dict[str, Any]]:
    client = get_client()
    since = datetime.now(UTC) - timedelta(days=_LOOKBACK_DAYS)
    query = f"""
        SELECT
            published_at,
            source,
            headline,
            substring(body, 1, {_BODY_SNIPPET_CHARS}) AS body_snippet
        FROM equity_raw.news_raw FINAL
        WHERE ticker = %(ticker)s AND published_at >= %(since)s
        ORDER BY published_at DESC
        LIMIT %(limit)s
    """
    result = client.query(
        query,
        parameters={"ticker": ticker, "since": since, "limit": _MAX_HEADLINES},
    )
    return [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]


def _sources_section(rows: list[dict[str, Any]]) -> list[str]:
    counts = Counter(str(row["source"]) for row in rows if row.get("source"))
    if not counts:
        return ["## SOURCES", "(no source attribution recorded)"]
    lines = ["## SOURCES"]
    for source, n in counts.most_common():
        lines.append(f"- {source}: {n}")
    return lines


def build_news_report(ticker: str) -> str:
    """Build a chronological news report for ``ticker``."""
    if ticker not in TICKERS:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")

    rows = _fetch_rows(ticker)
    meta = TICKER_METADATA.get(ticker, {})
    today = datetime.now(UTC).date().isoformat()

    header = [
        f"# NEWS REPORT — {ticker}",
        f"As of {today} ({meta.get('sector', 'Unknown sector')}, "
        f"{meta.get('industry', 'Unknown industry')})",
        f"Lookback: last {_LOOKBACK_DAYS} days, up to {_MAX_HEADLINES} headlines",
        "",
    ]

    if not rows:
        return "\n".join(
            [
                *header,
                "## RECENT HEADLINES",
                f"N/M (no news ingested for {ticker} in the last {_LOOKBACK_DAYS} days)",
                "",
                "## SOURCES",
                "(no headlines in window)",
            ]
        )

    lines = [*header, "## RECENT HEADLINES"]
    for row in rows:
        published: datetime = row["published_at"]
        snippet = (row.get("body_snippet") or "").strip()
        lines.append(f"- {published.date().isoformat()} [{row['source']}] {row['headline']}")
        if snippet:
            lines.append(f"    {snippet}")
    lines.append("")
    lines.extend(_sources_section(rows))
    return "\n".join(lines)
