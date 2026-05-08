"""Data quality checks for equity_raw.fundamentals.

Validates that yfinance statement extraction is still producing structurally
valid rows. If yfinance changes column names, most rows would hydrate as zeros
(see _safe_get in fundamentals.py) — these checks catch that regression.
"""

from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    asset_check,
)

from dagster_pipelines.assets.fundamentals import fundamentals
from dagster_pipelines.resources.clickhouse import ClickHouseResource

_VALID_PERIOD_TYPES = ("quarterly", "annual")


@asset_check(asset=fundamentals, blocking=True)
def fundamentals_has_rows(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Fail if equity_raw.fundamentals is empty.

    Blocking: fundamental_summary requires non-empty fundamentals to compute ratios.
    """
    result = clickhouse.execute("SELECT count() FROM equity_raw.fundamentals FINAL")
    row_count = int(result.result_rows[0][0])
    return AssetCheckResult(
        passed=row_count > 0,
        metadata={"row_count": row_count},
        description=f"Found {row_count} rows in equity_raw.fundamentals",
    )


@asset_check(asset=fundamentals, blocking=True)
def fundamentals_period_type_valid(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Fail if any row has period_type outside the allowed set.

    Blocking: fundamental_summary YoY logic branches on period_type
    (4 periods for quarterly, 1 for annual). An unexpected value silently
    produces wrong YoY percentages.
    """
    valid_list = ",".join(f"'{pt}'" for pt in _VALID_PERIOD_TYPES)
    result = clickhouse.execute(
        f"SELECT count() FROM equity_raw.fundamentals FINAL WHERE period_type NOT IN ({valid_list})"
    )
    invalid_count = int(result.result_rows[0][0])
    return AssetCheckResult(
        passed=invalid_count == 0,
        metadata={
            "invalid_period_type_rows": invalid_count,
            "allowed": list(_VALID_PERIOD_TYPES),
        },
        description=(f"{invalid_count} rows with period_type outside {_VALID_PERIOD_TYPES}"),
    )


@asset_check(asset=fundamentals)
def fundamentals_revenue_and_net_income_populated(
    clickhouse: ClickHouseResource,
) -> AssetCheckResult:
    """Warn if any ticker has no non-zero revenue AND no non-zero net_income.

    yfinance extraction uses _safe_get which returns 0.0 on missing keys. If
    yfinance renames both 'Total Revenue' and 'Net Income', every row for that
    ticker would be zero — the downstream ratios would still compute but be
    meaningless. This check flags that case.

    Non-blocking: a single ticker with bad data shouldn't halt the whole pipeline.
    """
    # A ticker is "populated" if any row has revenue != 0 OR net_income != 0.
    result = clickhouse.query_df(
        "SELECT ticker, "
        "countIf(revenue != 0 OR net_income != 0) AS populated_rows, "
        "count() AS total_rows "
        "FROM equity_raw.fundamentals FINAL "
        "GROUP BY ticker "
        "HAVING populated_rows = 0"
    )
    empty_tickers = result["ticker"].tolist() if not result.empty else []
    return AssetCheckResult(
        passed=len(empty_tickers) == 0,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "tickers_with_all_zero_fundamentals": empty_tickers,
            "count": len(empty_tickers),
        },
        description=(
            f"{len(empty_tickers)} tickers have all-zero revenue and net_income"
            + (f": {empty_tickers}" if empty_tickers else "")
        ),
    )


@asset_check(asset=fundamentals)
def fundamentals_no_all_zero_core_rows(
    clickhouse: ClickHouseResource,
) -> AssetCheckResult:
    """Warn on individual rows where revenue, net_income, AND total_assets are all 0.

    Catches stub rows from the QNT-179 race where yfinance lists a period as
    a column header before populating values; the older
    ``fundamentals_revenue_and_net_income_populated`` check only flags
    tickers whose every row is zero, so a single stub mixed in with real
    rows slipped through (AAPL Q2 FY2026 + 8 historical stubs across
    GOOGL/JPM/META/TSLA/UNH).

    Non-blocking: a stub usually self-heals on the next scheduled fetch via
    ReplacingMergeTree, but the WARN gives us visibility while it sits
    there.
    """
    # Total stub count comes from a count() query so the metadata stays accurate
    # if the corruption ever spreads beyond a handful of rows; the sample query
    # caps payload size with LIMIT 11 (10 surfaced + 1 to detect "more than 10").
    count_result = clickhouse.execute(
        "SELECT count() FROM equity_raw.fundamentals FINAL "
        "WHERE revenue = 0 AND net_income = 0 AND total_assets = 0"
    )
    stub_count = int(count_result.result_rows[0][0])

    sample_df = clickhouse.query_df(
        "SELECT ticker, period_end, period_type "
        "FROM equity_raw.fundamentals FINAL "
        "WHERE revenue = 0 AND net_income = 0 AND total_assets = 0 "
        "ORDER BY ticker, period_end "
        "LIMIT 11"
    )
    sample = [
        f"{r['ticker']}/{r['period_end']}/{r['period_type']}"
        for _, r in sample_df.head(10).iterrows()
    ]
    truncated = stub_count > len(sample)
    return AssetCheckResult(
        passed=stub_count == 0,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "stub_row_count": stub_count,
            "sample": sample,
        },
        description=(
            f"{stub_count} rows have all-zero revenue, net_income, and total_assets"
            + (
                f" (first 10 of {stub_count}: {sample})"
                if truncated and sample
                else f" ({sample})"
                if sample
                else ""
            )
        ),
    )
