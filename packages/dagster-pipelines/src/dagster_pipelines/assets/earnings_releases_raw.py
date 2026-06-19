"""Dagster asset: per-ticker 8-K earnings-release ingest via SEC EDGAR (QNT-260).

The second RAG corpus. For each portfolio ticker, discovers 8-K Item 2.02
filings in a rolling window from the EDGAR full-text search API, resolves each
filing's EX-99.1 press-release exhibit, cleans the HTML to narrative text, and
upserts one row per release into equity_raw.earnings_releases_raw.

ReplacingMergeTree on ``(ticker, filing_date, doc_id)`` dedups on re-run via
``doc_id = blake2b(url)`` — the same stable URL-hash scheme used for news and
Qdrant point ids — so the asset is idempotent. Downstream, earnings_embeddings
chunks ``body`` and embeds it into the equity_earnings Qdrant collection.

Source-boundary contract (QNT-259): the assembled per-release frame is
validated via EARNINGS_RELEASE_CONTRACT before the write — a renamed EFTS key
or a missing column hard-fails the partition (-> QNT-62 Discord sensor); an
empty cleaned body is a value-tier reject routed to ingest_rejects.

Note: this module deliberately omits ``from __future__ import annotations`` so
Dagster's runtime introspection on ``EarningsReleasesConfig`` works (matches the
pattern in ohlcv_raw.py / news_raw.py).
"""

import hashlib
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pandas as pd
from dagster import (
    AssetExecutionContext,
    Backoff,
    Config,
    Jitter,
    RetryPolicy,
    StaticPartitionsDefinition,
    asset,
)
from shared.contracts import EARNINGS_RELEASE_CONTRACT, validate_contract
from shared.tickers import TICKERS

from dagster_pipelines.edgar_feeds import (
    discover_earnings_filings,
    edgar_headers,
    fetch_clean_text,
    resolve_exhibit,
)
from dagster_pipelines.rejects import Reject, record_rejects
from dagster_pipelines.resources.clickhouse import ClickHouseResource

logger = logging.getLogger(__name__)

earnings_partitions = StaticPartitionsDefinition(TICKERS)

# Rolling discovery window. ~15 months captures the most recent ~4-5 quarterly
# earnings releases per ticker; ReplacingMergeTree keeps re-runs idempotent and
# the table accumulates older quarters over time as new ones land.
_DEFAULT_LOOKBACK_DAYS = 450

_REQUEST_TIMEOUT_SECONDS = 30.0

_COLUMNS = [
    "doc_id",
    "ticker",
    "cik",
    "accession",
    "form",
    "items",
    "filing_date",
    "period_ending",
    "exhibit",
    "title",
    "url",
    "body",
    "fetched_at",
]


class EarningsReleasesConfig(Config):
    """Per-run config for earnings_releases_raw.

    ``lookback_days`` controls the discovery window passed to EDGAR. Default 450
    (~5 quarters). A first-run / backfill can widen it to pull more history.
    """

    lookback_days: int = _DEFAULT_LOOKBACK_DAYS


def _doc_id(url: str) -> int:
    """Deterministic UInt64 hash of the exhibit URL for dedup.

    Same scheme as news_raw._url_hash — the stable doc_id shared across the
    ClickHouse row, the Qdrant points, and (per the Track-2 design) the
    relevance labels. ReplacingMergeTree dedups on ``(ticker, filing_date,
    doc_id)``, so the same release re-fetched keeps the asset idempotent.
    """
    return int(hashlib.blake2b(url.encode("utf-8"), digest_size=8).hexdigest(), 16)


@asset(
    partitions_def=earnings_partitions,
    retry_policy=RetryPolicy(
        max_retries=3,
        delay=30,
        backoff=Backoff.EXPONENTIAL,
        jitter=Jitter.PLUS_MINUS,
    ),
    group_name="ingestion",
)
def earnings_releases_raw(
    context: AssetExecutionContext,
    config: EarningsReleasesConfig,
    clickhouse: ClickHouseResource,
) -> None:
    """Fetch per-ticker 8-K earnings releases from EDGAR into earnings_releases_raw.

    Partitioned by ticker. One pooled httpx client per partition carries the
    SEC-required User-Agent across discovery, manifest, and document fetches.
    Per-filing failures (exhibit unresolved, fetch error, empty body) are routed
    to the reject sink; transient HTTP errors are handled by the RetryPolicy.
    """
    ticker = context.partition_key
    until = date.today()
    since = until - timedelta(days=config.lookback_days)

    context.log.info(
        "Discovering EDGAR 8-K Item 2.02 filings for %s [%s..%s]",
        ticker,
        since.isoformat(),
        until.isoformat(),
    )

    # Accessions already ingested for this ticker. 8-K filings are immutable once
    # filed, so a discovered filing whose accession is already a row needs no
    # re-fetch — skipping it before resolve/fetch avoids the two redundant EDGAR
    # requests (manifest + document) per already-stored release, which is the
    # bulk of the weekly steady-state cost (the corpus only gains ~1 filing per
    # ticker per quarter). Previously-*rejected* filings have no row, so their
    # accession is absent here and they are correctly retried. Discovery (the one
    # cheap call) still runs so a newly-filed release is always found.
    existing = clickhouse.query_df(
        "SELECT DISTINCT accession FROM equity_raw.earnings_releases_raw FINAL "
        "WHERE ticker = %(ticker)s",
        parameters={"ticker": ticker},
    )
    ingested_accessions: set[str] = set(existing["accession"]) if not existing.empty else set()

    http = httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS, headers=edgar_headers())
    rows: list[dict[str, Any]] = []
    rejects: list[Reject] = []
    try:
        discovered = discover_earnings_filings(ticker, since=since, until=until, client=http)
        filings = [f for f in discovered if f.accession not in ingested_accessions]
        context.log.info(
            "Found %d earnings filings for %s (%d new, %d already ingested)",
            len(discovered),
            ticker,
            len(filings),
            len(discovered) - len(filings),
        )

        if not filings:
            # Steady-state week: every discovered release is already stored, so
            # there is nothing to fetch. Emit the 0-count reject metric (keeps the
            # QNT-240 dashboard series gap-free) and return without a DB write.
            context.log.info("No new earnings releases for %s — skipping insert", ticker)
            record_rejects(context, clickhouse, source_asset="earnings_releases_raw", rejects=[])
            return

        for filing in filings:
            try:
                resolved = resolve_exhibit(filing, client=http)
                if resolved is None:
                    rejects.append(
                        Reject(
                            ticker=ticker,
                            reason="exhibit_unresolved",
                            payload={"accession": filing.accession},
                        )
                    )
                    continue
                exhibit_type, url = resolved
                body = fetch_clean_text(url, client=http).strip()
                if not body:
                    rejects.append(
                        Reject(
                            ticker=ticker,
                            reason="empty_body",
                            payload={"accession": filing.accession, "url": url},
                        )
                    )
                    continue
                # Title = the release headline (first cleaned line), falling back
                # to EDGAR's display name when the body is unexpectedly headless.
                title = body.splitlines()[0][:300] if body else filing.title
                rows.append(
                    {
                        "doc_id": _doc_id(url),
                        "ticker": ticker,
                        "cik": filing.cik,
                        "accession": filing.accession,
                        "form": "8-K",
                        "items": filing.items,
                        "filing_date": filing.filing_date,
                        "period_ending": filing.period_ending,
                        "exhibit": exhibit_type,
                        "title": title,
                        "url": url,
                        "body": body,
                        "fetched_at": datetime.now(UTC).replace(tzinfo=None),
                    }
                )
            except httpx.HTTPError as exc:
                # Any per-document HTTP failure (status error, timeout, connect
                # error): record and continue so one bad exhibit doesn't lose the
                # rest of the ticker's window. The next scheduled run re-attempts
                # the rejected release (idempotent on doc_id). Discovery-level
                # transient errors stay uncaught -> the asset RetryPolicy.
                rejects.append(
                    Reject(
                        ticker=ticker,
                        reason="fetch_error",
                        payload={"accession": filing.accession},
                        detail=str(exc),
                    )
                )
    finally:
        http.close()

    if not rows:
        context.log.warning("No usable earnings releases for %s — skipping insert", ticker)
        record_rejects(context, clickhouse, source_asset="earnings_releases_raw", rejects=rejects)
        return

    df = pd.DataFrame(rows)
    # Contract pins filing_date as datetime64; ClickHouse Date truncates on insert.
    df["filing_date"] = pd.to_datetime(df["filing_date"])
    df["period_ending"] = pd.to_datetime(df["period_ending"])

    result = validate_contract(df, EARNINGS_RELEASE_CONTRACT)
    df = result.valid_df
    if result.value_rejects:
        rejects.extend(
            Reject(
                ticker=ticker,
                reason="contract_value_violation",
                payload={"column": r.column, "value": r.failure_case, "check": r.check},
            )
            for r in result.value_rejects
        )

    record_rejects(context, clickhouse, source_asset="earnings_releases_raw", rejects=rejects)

    if df.empty:
        context.log.warning("All discovered releases for %s failed the contract — skipping", ticker)
        return

    df["doc_id"] = df["doc_id"].astype("uint64")
    df = pd.DataFrame(df[_COLUMNS])
    clickhouse.insert_df("equity_raw.earnings_releases_raw", df)
    context.log.info("Inserted %d earnings releases for %s", len(df), ticker)
