"""Data quality checks for equity_derived.fundamental_summary.

fundamental_summary divides many quantities (see _safe_divide in
fundamental_summary.py). _safe_divide coerces 0-denominators to NaN (→ NULL),
so +/-inf should never appear. These checks catch that invariant breaking
and flag ratios that are implausibly out-of-range.

All checks are WARN — a single bad ticker shouldn't block the pipeline.
"""

from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    asset_check,
)

from dagster_pipelines.assets.fundamental_summary import fundamental_summary
from dagster_pipelines.resources.clickhouse import ClickHouseResource

_TABLE = "equity_derived.fundamental_summary"

# Plausibility bands — loose enough to allow real extremes, tight enough to
# catch arithmetic corruption. P/E is symmetric: the N/M floor
# (_EPS_NM_THRESHOLD in fundamental_summary.py) permits |P/E| up to ~10k
# on both signs, so an asymmetric lower bound would flag legitimate
# negative-earnings years (e.g. AMZN 2022 = latest_close × 2022 loss).
_PE_MIN, _PE_MAX = -10_000.0, 10_000.0
_NET_MARGIN_MIN, _NET_MARGIN_MAX = -100.0, 100.0  # percent
# EBITDA margin shares net margin's [-100, 100] band — values outside that
# range mean the numerator (single-period EBITDA) was divided by a
# mismatched-period revenue, the same class of bug QNT-134 shipped on
# quarterly rows before the fix-forward narrowed emission to TTM only.
_EBITDA_MARGIN_MIN, _EBITDA_MARGIN_MAX = -100.0, 100.0  # percent


@asset_check(asset=fundamental_summary)
def fundamental_summary_pe_in_band(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any P/E ratio is outside [-10000, 10000]."""
    result = clickhouse.execute(
        f"SELECT count() FROM {_TABLE} FINAL "
        f"WHERE pe_ratio IS NOT NULL "
        f"AND (pe_ratio < {_PE_MIN} OR pe_ratio > {_PE_MAX})"
    )
    bad = int(result.result_rows[0][0])
    return AssetCheckResult(
        passed=bad == 0,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "rows_outside_pe_band": bad,
            "band": [_PE_MIN, _PE_MAX],
        },
        description=f"{bad} rows with P/E outside [{_PE_MIN}, {_PE_MAX}]",
    )


@asset_check(asset=fundamental_summary)
def fundamental_summary_net_margin_in_band(
    clickhouse: ClickHouseResource,
) -> AssetCheckResult:
    """Warn if net_margin_pct is outside [-100, 100].

    A >100% net margin is arithmetically impossible (net income > revenue);
    such values indicate unit-mismatched inputs or a divide-by-zero escape.
    """
    result = clickhouse.execute(
        f"SELECT count() FROM {_TABLE} FINAL "
        f"WHERE net_margin_pct IS NOT NULL "
        f"AND (net_margin_pct < {_NET_MARGIN_MIN} OR net_margin_pct > {_NET_MARGIN_MAX})"
    )
    bad = int(result.result_rows[0][0])
    return AssetCheckResult(
        passed=bad == 0,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "rows_outside_net_margin_band": bad,
            "band": [_NET_MARGIN_MIN, _NET_MARGIN_MAX],
        },
        description=(
            f"{bad} rows with net_margin_pct outside [{_NET_MARGIN_MIN}, {_NET_MARGIN_MAX}]"
        ),
    )


@asset_check(asset=fundamental_summary)
def fundamental_summary_ebitda_margin_in_band(
    clickhouse: ClickHouseResource,
) -> AssetCheckResult:
    """Warn if ebitda_margin_pct is outside [-100, 100] percent.

    Mirrors net_margin_pct's band check. The QNT-134 ship initially populated
    ebitda_margin_pct on quarterly rows by dividing TTM EBITDA by a single-Q
    revenue, producing values like 195%/233%/285% for NVDA. Without this band
    check the bug only surfaced via manual spot-checking — the kind of
    'first WARN' the asset-check rule (feedback_dont_explain_away_first_warn)
    is meant to catch.
    """
    result = clickhouse.execute(
        f"SELECT count() FROM {_TABLE} FINAL "
        f"WHERE ebitda_margin_pct IS NOT NULL "
        f"AND (ebitda_margin_pct < {_EBITDA_MARGIN_MIN} "
        f"OR ebitda_margin_pct > {_EBITDA_MARGIN_MAX})"
    )
    bad = int(result.result_rows[0][0])
    return AssetCheckResult(
        passed=bad == 0,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "rows_outside_ebitda_margin_band": bad,
            "band": [_EBITDA_MARGIN_MIN, _EBITDA_MARGIN_MAX],
        },
        description=(
            f"{bad} rows with ebitda_margin_pct outside "
            f"[{_EBITDA_MARGIN_MIN}, {_EBITDA_MARGIN_MAX}]"
        ),
    )


@asset_check(asset=fundamental_summary)
def fundamental_summary_no_infinities(clickhouse: ClickHouseResource) -> AssetCheckResult:
    """Warn if any ratio column contains +/-Infinity.

    _safe_divide replaces 0-denominators with NaN (→ NULL), so infinities should
    never reach the table. If one does, the divide-by-zero guard regressed.
    """
    ratio_cols = (
        "pe_ratio",
        "ev_ebitda",
        "price_to_book",
        "price_to_sales",
        "eps",
        "revenue_yoy_pct",
        "net_income_yoy_pct",
        "fcf_yoy_pct",
        "net_margin_pct",
        "gross_margin_pct",
        "ebitda_margin_pct",
        "roe",
        "roa",
        "fcf_yield",
        "debt_to_equity",
        "current_ratio",
    )
    # isInfinite() is false for NULL, so no null guard needed.
    sum_exprs = " + ".join(f"countIf(isInfinite({col}))" for col in ratio_cols)
    result = clickhouse.execute(f"SELECT {sum_exprs} FROM {_TABLE} FINAL")
    inf_count = int(result.result_rows[0][0])
    return AssetCheckResult(
        passed=inf_count == 0,
        severity=AssetCheckSeverity.WARN,
        metadata={"infinite_values": inf_count, "columns_checked": list(ratio_cols)},
        description=f"{inf_count} +/-Infinity values across ratio columns",
    )
