"""Data quality checks for equity_raw.earnings_calendar (QNT-357).

Real domain bounds, not just not-null: the next earnings date is only useful to
the report if it is genuinely upcoming and plausibly near. Earnings are quarterly
(~90 days apart), so a date already past means the weekly poll is lagging a
release, and a date beyond ~120 days means yfinance handed us an implausible
estimate. Both are WARN — a single stale/odd ticker shouldn't block the pipeline,
and a lagging date self-heals on the next weekly refresh.
"""

from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    asset_check,
)

from dagster_pipelines.assets.earnings_calendar import earnings_calendar
from dagster_pipelines.resources.clickhouse import ClickHouseResource

_TABLE = "equity_raw.earnings_calendar"
_DEFAULT_SEVERITY = AssetCheckSeverity.WARN

# Earnings are quarterly (~90 days apart); an estimate more than ~120 days out is
# implausibly far and suggests yfinance returned a stale/placeholder date.
_MAX_HORIZON_DAYS = 120


@asset_check(asset=earnings_calendar)
def earnings_calendar_date_in_window(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any next_earnings_date is in the past or beyond ~120 days out.

    The asset only inserts a still-future date, so a past date means the weekly
    poll is lagging a release that already happened; a date beyond the horizon
    means the calendar estimate is implausibly far for a quarterly reporter.
    """
    result = clickhouse.query_df(
        f"SELECT ticker, next_earnings_date FROM {_TABLE} FINAL "
        f"WHERE next_earnings_date < today() "
        f"   OR next_earnings_date > today() + INTERVAL {_MAX_HORIZON_DAYS} DAY "
        f"ORDER BY ticker"
    )
    out_of_window = (
        [f"{r['ticker']}/{r['next_earnings_date']}" for _, r in result.iterrows()]
        if not result.empty
        else []
    )
    return AssetCheckResult(
        passed=len(out_of_window) == 0,
        severity=_DEFAULT_SEVERITY,
        metadata={
            "out_of_window": out_of_window,
            "count": len(out_of_window),
            "max_horizon_days": _MAX_HORIZON_DAYS,
        },
        description=(
            f"{len(out_of_window)} rows with next_earnings_date outside "
            f"[today, today + {_MAX_HORIZON_DAYS}d]"
            + (f": {out_of_window}" if out_of_window else "")
        ),
    )


__all__ = [
    "earnings_calendar_date_in_window",
]
