"""News report template — parameterised from the technical template.

Phase 3 note: ``equity_raw.news_raw`` is populated by Phase 4. Until then the
endpoint returns a well-formed "no data yet" report rather than a 404 so the
agent tool contract stays stable and the reader immediately sees why.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from shared.tickers import TICKER_METADATA, TICKERS

from api.clickhouse import get_client

_MAX_HEADLINES = 10
_LOOKBACK_DAYS = 7


def _fetch_rows(ticker: str) -> list[dict[str, Any]]:
    client = get_client()
    since = datetime.now(UTC) - timedelta(days=_LOOKBACK_DAYS)
    query = """
        SELECT published_at, source, headline
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


def build_news_report(ticker: str) -> str:
    """Build a human-readable news summary report for ``ticker``."""
    if ticker not in TICKERS:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")

    rows = _fetch_rows(ticker)
    meta = TICKER_METADATA.get(ticker, {})
    today = datetime.now(UTC).date().isoformat()

    header = [
        f"# NEWS REPORT — {ticker}",
        f"As of {today} ({meta.get('sector', 'Unknown sector')}, "
        f"{meta.get('industry', 'Unknown industry')})",
        f"Lookback: last {_LOOKBACK_DAYS} days",
        "",
    ]

    if not rows:
        return "\n".join(
            [
                *header,
                "## HEADLINES",
                f"N/M (no news ingested for {ticker} in the last "
                f"{_LOOKBACK_DAYS} days — Phase 4 news pipeline pending)",
                "",
                "## SIGNAL",
                "N/M (no news to evaluate)",
            ]
        )

    lines = [*header, "## HEADLINES"]
    for row in rows:
        published: datetime = row["published_at"]
        lines.append(f"- {published.date().isoformat()} [{row['source']}] {row['headline']}")
    lines += [
        "",
        "## SIGNAL",
        f"{len(rows)} headlines in the last {_LOOKBACK_DAYS} days "
        "(sentiment scoring pending Phase 4)",
    ]
    return "\n".join(lines)
