"""News report template — chronological headlines + outlet breakdown.

QNT-207 drops the per-headline sentiment surfacing that QNT-175 introduced:
the ``news_raw.sentiment_label`` column stays nullable in the warehouse but
mostly contains ``pending`` / ``neutral`` because we never shipped a real
classifier — surfacing those into the report added noise without signal.

Lookback widened 7 -> 14 days and headline cap 10 -> 20, so the synthesize
step sees a meatier slice of recent coverage to ground the thesis in.

QNT-356 (C-9): the digest's "source" identity is the publishing outlet
(``publisher_name``: Yahoo, Benzinga, CNBC, ...), not the ingestion feed label
(``source``: only ``finnhub`` / ``yahoo_finance`` per ticker, which carries no
breadth signal). Near-duplicate headlines — one wire story republished across
outlets — collapse into one bullet whose "also covered by N sources" suffix is
the outlet count, the materiality cue an analyst reads as story weight.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from shared.retrieval import NEWS_BODY_SNIPPET_CHARS
from shared.tickers import TICKER_METADATA, TICKERS

from api.clickhouse import get_client
from api.formatters import format_as_of_footer

_MAX_HEADLINES = 20  # distinct stories (clusters) rendered
_LOOKBACK_DAYS = 14

# QNT-356 (C-9): dedup runs over a pool LARGER than the render cap so collapsing a
# syndicated story frees its slots for the next distinct story rather than merely
# shrinking the digest ("five outlets covering one story crowd out the window's
# second story"). Only _MAX_HEADLINES representative bullets render, so the prompt
# cost stays bounded (~20 bullets) regardless of pool size.
_FETCH_POOL = 60

# Jaccard overlap of normalised headline token sets: >= this threshold is "same
# story". Set high (0.8) on purpose: the target is syndicated near-identical
# reprints, which score ~1.0. Distinct-but-similar headlines that differ only in a
# key discriminator -- "...Q3 2026 Earnings" vs "...Q4 2026 Earnings" (0.71),
# "shares rise on strong..." vs "shares fall on weak..." (0.60) -- sit BELOW 0.8
# and stay separate. Merging those would hide a materially different story under an
# "also covered by" suffix; a missed collapse (reworded takes staying separate) is
# the benign failure, so we bias to precision over recall.
_DUP_SIMILARITY_THRESHOLD = 0.8
_WORD_RE = re.compile(r"\w+")


def _fetch_rows(ticker: str) -> list[dict[str, Any]]:
    client = get_client()
    since = datetime.now(UTC) - timedelta(days=_LOOKBACK_DAYS)
    query = f"""
        SELECT
            published_at,
            source,
            publisher_name,
            headline,
            substring(body, 1, {NEWS_BODY_SNIPPET_CHARS}) AS body_snippet
        FROM equity_raw.news_raw FINAL
        WHERE ticker = %(ticker)s AND published_at >= %(since)s
        ORDER BY published_at DESC
        LIMIT %(limit)s
    """
    result = client.query(
        query,
        parameters={"ticker": ticker, "since": since, "limit": _FETCH_POOL},
    )
    return [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]


def _outlet(row: dict[str, Any]) -> str:
    """The publishing outlet for a row: ``publisher_name`` (the canonical outlet),
    falling back to the Finnhub feed label only if publisher_name is unset."""
    return str(row.get("publisher_name") or row.get("source") or "")


def _headline_tokens(headline: str) -> frozenset[str]:
    return frozenset(_WORD_RE.findall(headline.lower()))


def _is_near_dup(a: frozenset[str], b: frozenset[str]) -> bool:
    """True when two headline token sets overlap at >= the dup threshold (Jaccard)."""
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= _DUP_SIMILARITY_THRESHOLD


def _cluster_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group near-duplicate headlines into story clusters, newest row first.

    Rows arrive DESC by ``published_at``, so the first row to seed a cluster is its
    newest and becomes the representative bullet. Greedy O(n^2) matching is fine at
    pool size. Each cluster keeps its member rows so the render step can surface
    coverage breadth (distinct outlets) as a materiality cue and roll outlet volume
    up over exactly the stories shown.
    """
    clusters: list[dict[str, Any]] = []
    for row in rows:
        tokens = _headline_tokens(str(row.get("headline", "")))
        for cluster in clusters:
            if _is_near_dup(tokens, cluster["tokens"]):
                cluster["members"].append(row)
                break
        else:
            clusters.append({"rep": row, "tokens": tokens, "members": [row]})
    return clusters


def _sources_section(rows: list[dict[str, Any]]) -> list[str]:
    counts = Counter(outlet for row in rows if (outlet := _outlet(row)))
    if not counts:
        return ["## SOURCES", "(no source attribution recorded)"]
    lines = ["## SOURCES"]
    for outlet, n in counts.most_common():
        lines.append(f"- {outlet}: {n}")
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
                "",
                format_as_of_footer(None),
            ]
        )

    # Cluster the pool, then render up to _MAX_HEADLINES DISTINCT stories.
    clusters = _cluster_rows(rows)[:_MAX_HEADLINES]
    lines = [*header, "## RECENT HEADLINES"]
    for cluster in clusters:
        rep = cluster["rep"]
        published: datetime = rep["published_at"]
        line = f"- {published.date().isoformat()} [{_outlet(rep)}] {rep['headline']}"
        # Coverage breadth: distinct OTHER outlets that also ran the story.
        outlets = {o for member in cluster["members"] if (o := _outlet(member))}
        others = outlets - {_outlet(rep)}
        if others:
            n = len(others)
            line += f"  (also covered by {n} source{'s' if n != 1 else ''})"
        lines.append(line)
        snippet = (rep.get("body_snippet") or "").strip()
        if snippet:
            lines.append(f"    {snippet}")
    lines.append("")
    # Roll outlet volume up over exactly the stories shown (not the unrendered pool).
    rendered_rows = [member for cluster in clusters for member in cluster["members"]]
    lines.extend(_sources_section(rendered_rows))
    lines.append("")
    # QNT-299: the header's "As of {today}" is when the report was generated,
    # not how stale the underlying headlines are. The footer uses the newest
    # headline's own date -- rows are DESC by published_at, so rows[0] is it --
    # so a freshness read reflects the actual data, not the query time.
    lines.append(format_as_of_footer(rows[0]["published_at"].date()))
    return "\n".join(lines)
