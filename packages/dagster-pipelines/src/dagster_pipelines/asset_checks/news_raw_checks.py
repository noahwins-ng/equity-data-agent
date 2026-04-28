"""Data quality checks for equity_raw.news_raw (QNT-93).

Applies the Phase 2 lesson (QNT-68): real domain bounds catch semantic bugs
that code review misses. News has a different bug surface than OHLCV/fundamentals
— RSS feeds can return empty strings, malformed URLs, or wildly misdated entries
— so the checks here target those specific failure modes rather than generic
"not null".

Severity defaults to WARN (`_DEFAULT_SEVERITY`) so a single bad ticker can't
block the pipeline. Individual checks can be promoted to ERROR later by
replacing the `severity=_DEFAULT_SEVERITY` kwarg with `AssetCheckSeverity.ERROR`.
"""

from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    asset_check,
)

from dagster_pipelines.assets.news_raw import news_raw
from dagster_pipelines.resources.clickhouse import ClickHouseResource

_TABLE = "equity_raw.news_raw"

# Default severity for all checks in this module. Flip an individual check
# to ERROR by passing `severity=AssetCheckSeverity.ERROR` in AssetCheckResult.
_DEFAULT_SEVERITY = AssetCheckSeverity.WARN

# Allow up to 1h of clock skew between the RSS source and ClickHouse `now()`
# before flagging a published_at as "future". Yahoo Finance timestamps are
# UTC but feed publishers sometimes post a few minutes ahead of wall clock.
_FUTURE_TOLERANCE_SECONDS = 3600

# news_raw_schedule runs daily at 02:00 ET (QNT-143). 48h (2× cadence) bounds
# per-ticker freshness so the check fires after exactly one missed tick plus a
# grace day — i.e., a silent failure on the Saturday tick warns by Monday's
# check. Tighter than 48h would flap because max(fetched_at) approaches 24h
# right before each next tick. Looser would mask consecutive silent failures.
# ReplacingMergeTree refreshes fetched_at on every re-insert, so even a "quiet"
# ticker with no new URLs still has a fresh fetched_at as long as the asset
# ran successfully.
_MAX_INGESTION_LAG_HOURS = 48


@asset_check(asset=news_raw)
def news_raw_has_rows(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any partitioned ticker has zero rows in equity_raw.news_raw.

    `has_rows > 0 per ticker` is stricter than the global `count() > 0` check
    used for OHLCV/fundamentals — news fan-outs partition-by-partition, and a
    single feed that silently 404s would leave one ticker empty while the
    table as a whole stays populated.
    """
    from shared.tickers import TICKERS

    result = clickhouse.query_df(
        f"SELECT ticker, count() AS row_count FROM {_TABLE} FINAL GROUP BY ticker"
    )
    per_ticker = dict(zip(result["ticker"], result["row_count"], strict=False))
    empty = sorted(t for t in TICKERS if per_ticker.get(t, 0) == 0)
    return AssetCheckResult(
        passed=len(empty) == 0,
        severity=_DEFAULT_SEVERITY,
        metadata={
            "empty_tickers": empty,
            "empty_count": len(empty),
            "total_tickers": len(TICKERS),
        },
        description=(
            f"{len(empty)}/{len(TICKERS)} tickers have zero rows" + (f": {empty}" if empty else "")
        ),
    )


@asset_check(asset=news_raw)
def news_raw_no_empty_headlines(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any row has a blank or whitespace-only headline.

    news_raw._article_to_row already strips and skips blank headlines at
    ingest time (post-QNT-141 / Finnhub migration; was _entry_to_row pre-RSS
    cutover). This check guards against that filter regressing — e.g. if
    Finnhub renames `headline` and `.strip()` runs on a None.
    """
    result = clickhouse.execute(f"SELECT count() FROM {_TABLE} FINAL WHERE empty(trim(headline))")
    bad = int(result.result_rows[0][0])
    return AssetCheckResult(
        passed=bad == 0,
        severity=_DEFAULT_SEVERITY,
        metadata={"empty_headline_rows": bad},
        description=f"{bad} rows with empty/whitespace-only headline",
    )


@asset_check(asset=news_raw)
def news_raw_valid_urls(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any row has a URL that doesn't start with http:// or https://.

    The url column feeds the frontend's "open article" link and the Qdrant
    payload consumed by the agent — malformed URLs break both surfaces. A
    literal-prefix check is the minimum bound here; a full URL parse would
    add false positives for unusual but valid URLs (IPv6 hosts, unusual ports).
    """
    result = clickhouse.execute(
        f"SELECT count() FROM {_TABLE} FINAL "
        f"WHERE NOT (startsWith(url, 'http://') OR startsWith(url, 'https://'))"
    )
    bad = int(result.result_rows[0][0])
    return AssetCheckResult(
        passed=bad == 0,
        severity=_DEFAULT_SEVERITY,
        metadata={"invalid_url_rows": bad},
        description=f"{bad} rows with URL missing http(s):// scheme",
    )


@asset_check(asset=news_raw)
def news_raw_no_future_published_at(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any row has published_at in the future (beyond clock-skew tolerance).

    Published timestamps in the future corrupt downstream "latest N headlines"
    queries by sorting correctly-timestamped recent items below stale-but-future
    items. `_FUTURE_TOLERANCE_SECONDS` absorbs minor publisher clock skew.
    """
    result = clickhouse.execute(
        f"SELECT count() FROM {_TABLE} FINAL "
        f"WHERE published_at > now() + INTERVAL {_FUTURE_TOLERANCE_SECONDS} SECOND"
    )
    bad = int(result.result_rows[0][0])
    return AssetCheckResult(
        passed=bad == 0,
        severity=_DEFAULT_SEVERITY,
        metadata={
            "future_published_rows": bad,
            "tolerance_seconds": _FUTURE_TOLERANCE_SECONDS,
        },
        description=(
            f"{bad} rows with published_at > now() + {_FUTURE_TOLERANCE_SECONDS}s tolerance"
        ),
    )


@asset_check(asset=news_raw)
def news_raw_recent_ingestion(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any ticker's most recent fetched_at is older than _MAX_INGESTION_LAG_HOURS.

    news_raw_schedule runs daily at 02:00 ET; ReplacingMergeTree refreshes
    fetched_at on every re-insert of the same URL hash. So max(fetched_at) per
    ticker should advance every tick even on quiet tickers. A ticker stuck
    >48h indicates a silent ingestion failure (DNS, 404, parser regression)
    that doesn't raise but also doesn't produce rows.
    """
    # One query returns every ticker's staleness; we classify stale/fresh in
    # Python rather than running a second full scan. Tickers entirely missing
    # from news_raw don't appear in the result and are handled by
    # news_raw_has_rows — this check focuses on "present but stale".
    result = clickhouse.query_df(
        f"SELECT ticker, "
        f"  dateDiff('hour', max(fetched_at), now()) AS hours_since_fetch "
        f"FROM {_TABLE} FINAL "
        f"GROUP BY ticker"
    )
    stale: dict[str, int] = {}
    fresh_tickers: list[str] = []
    if not result.empty:
        for t, h in zip(result["ticker"], result["hours_since_fetch"], strict=False):
            hours = int(h)
            if hours > _MAX_INGESTION_LAG_HOURS:
                stale[str(t)] = hours
            else:
                fresh_tickers.append(str(t))
    return AssetCheckResult(
        passed=len(stale) == 0,
        severity=_DEFAULT_SEVERITY,
        metadata={
            "stale_tickers": stale,
            "threshold_hours": _MAX_INGESTION_LAG_HOURS,
            "fresh_ticker_count": len(fresh_tickers),
        },
        description=(
            f"{len(stale)} tickers have fetched_at > {_MAX_INGESTION_LAG_HOURS}h old"
            + (f": {stale}" if stale else "")
        ),
    )


__all__ = [
    "news_raw_has_rows",
    "news_raw_no_empty_headlines",
    "news_raw_no_future_published_at",
    "news_raw_recent_ingestion",
    "news_raw_valid_urls",
]
