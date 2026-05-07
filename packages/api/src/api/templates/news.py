"""News report template — headline + body snippet + sentiment per article.

Phase 4 ships ``sentiment_label`` (pending|positive|neutral|negative) and
``body`` text on every ``equity_raw.news_raw`` row. QNT-175 plumbs both into
the agent-facing report so the synthesize step has prose context (not just a
headline) and can quote the sentiment distribution as a ground-truth signal
instead of leaning on its own headline-skim heuristics.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from shared.tickers import TICKER_METADATA, TICKERS

from api.clickhouse import get_client

_MAX_HEADLINES = 10
_LOOKBACK_DAYS = 7
_BODY_SNIPPET_CHARS = 120

# ``sentiment_label`` defaults to ``pending`` for legacy Yahoo-RSS rows that
# never went through the Phase 4 scorer. Anything outside the scored trio is
# rendered as N/A so the reader (and the agent) can tell "scorer hasn't run"
# apart from a real neutral verdict.
_SCORED_LABELS = frozenset({"positive", "neutral", "negative"})


def _fetch_rows(ticker: str) -> list[dict[str, Any]]:
    client = get_client()
    since = datetime.now(UTC) - timedelta(days=_LOOKBACK_DAYS)
    query = f"""
        SELECT
            published_at,
            source,
            headline,
            sentiment_label,
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


def _sentiment_display(label: str | None) -> str:
    """Return the canonical sentiment string for the report.

    Anything outside ``positive`` / ``neutral`` / ``negative`` (most often
    ``pending`` from legacy rows, occasionally ``""`` if a future migration
    relaxes the column) renders as ``N/A`` so the agent can distinguish
    "scorer hasn't run" from a real neutral verdict.
    """
    if label in _SCORED_LABELS:
        return label.capitalize()  # type: ignore[union-attr]
    return "N/A"


def _signal_distribution(rows: list[dict[str, Any]]) -> str:
    """Net-bullish/bearish summary across the scored articles.

    Skips ``N/A`` rows so a feed full of legacy Yahoo articles doesn't read
    as "1 bullish / 0 / 0 -- net bullish" off a single Finnhub article. If
    nothing is scored we say so explicitly instead of inventing a distribution.
    """
    counts = {"positive": 0, "neutral": 0, "negative": 0}
    for row in rows:
        label = row.get("sentiment_label")
        if label in _SCORED_LABELS:
            counts[label] += 1  # type: ignore[index]
    scored_total = sum(counts.values())
    if scored_total == 0:
        return (
            f"{len(rows)} headlines in the last {_LOOKBACK_DAYS} days "
            "(none scored — sentiment N/A across the window)"
        )

    if counts["positive"] > counts["negative"]:
        verdict = "net bullish"
    elif counts["negative"] > counts["positive"]:
        verdict = "net bearish"
    else:
        verdict = "balanced"
    return (
        f"{counts['positive']} bullish / {counts['neutral']} neutral / "
        f"{counts['negative']} bearish across {scored_total} scored "
        f"of {len(rows)} headlines -- {verdict}"
    )


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
                f"N/M (no news ingested for {ticker} in the last {_LOOKBACK_DAYS} days)",
                "",
                "## SIGNAL",
                "N/M (no news to evaluate)",
            ]
        )

    lines = [*header, "## HEADLINES"]
    for row in rows:
        published: datetime = row["published_at"]
        sentiment = _sentiment_display(row.get("sentiment_label"))
        snippet = (row.get("body_snippet") or "").strip()
        lines.append(f"- {published.date().isoformat()} [{row['source']}] {row['headline']}")
        if snippet:
            lines.append(f"    {snippet}")
        lines.append(f"    Sentiment: {sentiment}")
    lines += [
        "",
        "## SIGNAL",
        _signal_distribution(rows),
    ]
    return "\n".join(lines)
