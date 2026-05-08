import logging
import time
from datetime import datetime

import pandas as pd
import yfinance as yf
from dagster import (
    AssetExecutionContext,
    Backoff,
    Jitter,
    RetryPolicy,
    StaticPartitionsDefinition,
    asset,
)
from shared.tickers import TICKERS

from dagster_pipelines.resources.clickhouse import ClickHouseResource
from dagster_pipelines.retry_helpers import retry_after_seconds_from_exception

logger = logging.getLogger(__name__)

fundamentals_partitions = StaticPartitionsDefinition(TICKERS)


def _extract_periods(
    income_stmt: pd.DataFrame,
    balance_sheet: pd.DataFrame,
    cashflow: pd.DataFrame,
    info: dict,
    ticker: str,
    period_type: str,
) -> list[dict]:
    """Extract rows from yfinance financial statement DataFrames.

    Each column in the DataFrame is a reporting period (datetime index).
    Rows are financial line items.

    Skips a period when its Total Revenue cell is missing/NaN: yfinance
    sometimes lists an upcoming/just-reported period as a column header
    while the cells are still empty (QNT-179, AAPL Q2 FY2026 race). Without
    this guard, ``_safe_get``'s zero default lands an all-zero stub row in
    ClickHouse that downstream ratios divide by zero against.
    """
    if income_stmt.empty:
        return []

    rows: list[dict] = []
    for period_end in income_stmt.columns:
        revenue = _safe_get(income_stmt, "Total Revenue", period_end)
        if revenue is None:
            # yfinance has the period header but no values yet; skip the
            # whole period so we don't half-ingest a quarter.
            continue

        row: dict = {
            "ticker": ticker,
            "period_end": period_end.date() if hasattr(period_end, "date") else period_end,
            "period_type": period_type,
            "fetched_at": datetime.utcnow(),
            "revenue": revenue,
            "gross_profit": _safe_get_or_zero(income_stmt, "Gross Profit", period_end),
            "net_income": _safe_get_or_zero(income_stmt, "Net Income", period_end),
        }

        # Balance sheet
        for field, col_name in [
            ("total_assets", "Total Assets"),
            ("total_liabilities", "Total Liabilities Net Minority Interest"),
            ("current_assets", "Current Assets"),
            ("current_liabilities", "Current Liabilities"),
            ("total_debt", "Total Debt"),
            ("cash_and_equivalents", "Cash And Cash Equivalents"),
        ]:
            row[field] = _safe_get_or_zero(balance_sheet, col_name, period_end)

        # Cash flow
        row["free_cash_flow"] = _safe_get_or_zero(cashflow, "Free Cash Flow", period_end)

        # Info fields (point-in-time, same for all periods)
        row["ebitda"] = float(info.get("ebitda", 0) or 0)
        row["shares_outstanding"] = int(info.get("sharesOutstanding", 0) or 0)
        row["market_cap"] = float(info.get("marketCap", 0) or 0)

        rows.append(row)

    return rows


def _safe_get(df: pd.DataFrame, field: str, column: object) -> float | None:
    """Return a value from a yfinance statement DataFrame, or ``None`` if missing.

    ``None`` distinguishes "yfinance has no value here" from "the value is
    legitimately zero" — critical for the spine field (revenue) which the
    caller uses to decide whether to keep the period at all.
    """
    try:
        if field in df.index and column in df.columns:
            val = df.loc[field, column]
            if pd.notna(val):
                return float(val)
    except (KeyError, TypeError):
        pass
    return None


def _safe_get_or_zero(df: pd.DataFrame, field: str, column: object) -> float:
    """Like ``_safe_get`` but coerces missing to 0.0 — for non-spine fields.

    The ClickHouse columns are non-nullable Float64, so missing values land
    as 0.0. This is acceptable for fields like ``gross_profit`` where the
    period itself is already validated (revenue is present) — a missing
    line item then is genuinely "zero" or "yfinance lacks this granularity",
    not a stale-period stub.
    """
    val = _safe_get(df, field, column)
    return val if val is not None else 0.0


@asset(
    partitions_def=fundamentals_partitions,
    retry_policy=RetryPolicy(
        max_retries=3,
        delay=30,
        backoff=Backoff.EXPONENTIAL,
        jitter=Jitter.PLUS_MINUS,
    ),
    group_name="ingestion",
)
def fundamentals(
    context: AssetExecutionContext,
    clickhouse: ClickHouseResource,
) -> None:
    """Fetch quarterly/annual fundamentals from yfinance and upsert into equity_raw.fundamentals.

    Partitioned by ticker. ReplacingMergeTree deduplicates on re-run.
    Fetches all available quarters/years from yfinance (typically last 4 each).
    """
    ticker = context.partition_key

    context.log.info("Fetching fundamentals for %s", ticker)

    try:
        stock = yf.Ticker(ticker)
        info = stock.info or {}
    except Exception as exc:
        msg = str(exc).lower()
        if "429" in msg or "too many requests" in msg or "rate limit" in msg:
            # See ohlcv_raw for the rationale; same Retry-After handling
            # so the two yfinance call sites behave identically when
            # Yahoo gets specific about the back-off it wants.
            wait = retry_after_seconds_from_exception(exc)
            if wait is not None and wait > 0:
                context.log.info(
                    "yfinance 429 for %s — Retry-After=%.1fs; sleeping before re-raising",
                    ticker,
                    wait,
                )
                time.sleep(wait)
            raise
        context.log.warning("yfinance Ticker(%s) failed: %s — skipping", ticker, exc)
        return

    all_rows: list[dict] = []

    # Quarterly financials
    try:
        q_income = stock.quarterly_financials
        q_balance = stock.quarterly_balance_sheet
        q_cashflow = stock.quarterly_cashflow
        all_rows.extend(
            _extract_periods(q_income, q_balance, q_cashflow, info, ticker, "quarterly")
        )
    except Exception as exc:
        context.log.warning("Quarterly data failed for %s: %s", ticker, exc)

    # Annual financials
    try:
        a_income = stock.financials
        a_balance = stock.balance_sheet
        a_cashflow = stock.cashflow
        all_rows.extend(_extract_periods(a_income, a_balance, a_cashflow, info, ticker, "annual"))
    except Exception as exc:
        context.log.warning("Annual data failed for %s: %s", ticker, exc)

    if not all_rows:
        context.log.warning("No fundamental data found for %s — skipping", ticker)
        return

    df = pd.DataFrame(all_rows)

    # Ensure correct types for ClickHouse
    df["shares_outstanding"] = df["shares_outstanding"].astype("int64")

    cols = [
        "ticker",
        "period_end",
        "period_type",
        "revenue",
        "gross_profit",
        "net_income",
        "total_assets",
        "total_liabilities",
        "current_assets",
        "current_liabilities",
        "free_cash_flow",
        "ebitda",
        "total_debt",
        "cash_and_equivalents",
        "shares_outstanding",
        "market_cap",
        "fetched_at",
    ]
    df = pd.DataFrame(df[cols])

    clickhouse.insert_df("equity_raw.fundamentals", df)
    context.log.info(
        "Inserted %d rows for %s (%d quarterly, %d annual)",
        len(df),
        ticker,
        len([r for r in all_rows if r["period_type"] == "quarterly"]),
        len([r for r in all_rows if r["period_type"] == "annual"]),
    )

    # Rate limiting
    time.sleep(1.5)
