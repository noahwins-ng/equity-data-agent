"""Data quality checks for equity_raw.earnings_releases_raw (QNT-260).

Mirrors news_raw_checks: real domain bounds catch the failure modes specific to
EDGAR ingestion — a ticker whose discovery silently returned nothing, an exhibit
that cleaned to an empty body, or a mis-parsed filing date. Severity defaults to
WARN so one bad ticker can't block the pipeline.
"""

from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    asset_check,
)

from dagster_pipelines.assets.earnings_releases_raw import earnings_releases_raw
from dagster_pipelines.resources.clickhouse import ClickHouseResource

_TABLE = "equity_raw.earnings_releases_raw"
_DEFAULT_SEVERITY = AssetCheckSeverity.WARN

# Allow a day of clock skew before flagging a filing_date as "future" — EDGAR
# file_date is a calendar date, so anything past tomorrow is a parse bug.
_FUTURE_TOLERANCE_DAYS = 1


@asset_check(asset=earnings_releases_raw)
def earnings_releases_has_rows(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any portfolio ticker has zero earnings releases.

    Per-ticker (stricter than a global count): a ticker whose EDGAR discovery
    silently 404s or whose CIK is wrong would leave one ticker empty while the
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
            f"{len(empty)}/{len(TICKERS)} tickers have zero releases"
            + (f": {empty}" if empty else "")
        ),
    )


@asset_check(asset=earnings_releases_raw)
def earnings_releases_non_empty_body(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any row has a blank or whitespace-only body.

    The contract value-check already quarantines empty bodies at ingest; this
    guards against that filter regressing (e.g. the HTML cleaner returning
    whitespace that survives ``.strip()`` upstream).
    """
    result = clickhouse.execute(f"SELECT count() FROM {_TABLE} FINAL WHERE empty(trim(body))")
    bad = int(result.result_rows[0][0])
    return AssetCheckResult(
        passed=bad == 0,
        severity=_DEFAULT_SEVERITY,
        metadata={"empty_body_rows": bad},
        description=f"{bad} rows with empty/whitespace-only body",
    )


@asset_check(asset=earnings_releases_raw)
def earnings_releases_valid_filing_date(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any row has a filing_date in the future (beyond clock-skew tolerance).

    EDGAR's file_date is a calendar date that can't be in the future; one that is
    indicates a date-parse regression that would corrupt "latest release"
    ordering and the rolling discovery window.
    """
    result = clickhouse.execute(
        f"SELECT count() FROM {_TABLE} FINAL "
        f"WHERE filing_date > today() + INTERVAL {_FUTURE_TOLERANCE_DAYS} DAY"
    )
    bad = int(result.result_rows[0][0])
    return AssetCheckResult(
        passed=bad == 0,
        severity=_DEFAULT_SEVERITY,
        metadata={
            "future_filing_date_rows": bad,
            "tolerance_days": _FUTURE_TOLERANCE_DAYS,
        },
        description=f"{bad} rows with filing_date > today + {_FUTURE_TOLERANCE_DAYS}d tolerance",
    )


__all__ = [
    "earnings_releases_has_rows",
    "earnings_releases_non_empty_body",
    "earnings_releases_valid_filing_date",
]
