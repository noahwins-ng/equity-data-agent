"""Source-boundary data contracts (QNT-259).

Executable specs of the SHAPE we assume each external source hands us, enforced
at the ingestion boundary *before* any ClickHouse write. The 23 asset checks
validate VALUES (RSI in [0, 100], close not null); these contracts validate
STRUCTURE -- a renamed/missing column, a dtype change, or an empty frame is
caught here instead of landing as a silent NULL column days later.

Pandera is the tool (the canonical pandas dataframe-contract library); the
contracts live in ``shared`` so the producer (Dagster ingestion assets) and the
tests import the same single source of truth.

Two-tier failure policy (the actual engineering, per
docs/v2-overall-enhancement.md Track 1):

  * SCHEMA violation -- renamed/missing column, dtype change, empty frame. The
    contract is stale or the source broke; no amount of row-quarantining fixes a
    renamed column. ``validate_contract`` RAISES ``SchemaContractViolation``,
    which (left uncaught in an asset) hard-fails the Dagster partition and fires
    the QNT-62 Discord run-failure sensor.
  * VALUE violation -- an out-of-range or bad-enum cell in an otherwise
    well-shaped frame. The offending rows are returned as ``value_rejects`` for
    the asset to route to the existing ``equity_raw.ingest_rejects`` sink
    (QNT-243); the clean rows proceed. No behavior change.

Classification rule: a pandera failure is SCHEMA-tier when it is a
column-presence failure (``schema_context != "Column"``) or a dtype failure
(check ``dtype(...)``); every other element-wise check failure (``ge``,
``isin``, nullability) is VALUE-tier. Extra / reordered columns are tolerated
(``strict`` is off) -- they do not break us, and the craft is failing on what
breaks us while tolerating what doesn't.

Calibration: dtypes are pinned only where drift is meaningful and the real
payload is stable (price columns are float; ``period_type`` is a string).
Price/financial columns are ``nullable=True`` so a benign NaN row on a
non-trading day is not newly quarantined (preserves existing reject counts on
clean inputs). Value checks (non-negative volume, period-type enum) are
impossible-to-trip on clean real data -- they exist to route genuinely broken
cells, not to filter live data.

Evolving a contract (schema-evolution discipline, AC5):
  When a source LEGITIMATELY changes shape (yfinance adds a field, Finnhub
  renames a key), bump the contract HERE in the same commit, so the change shows
  in a diff -- same migration discipline as the ClickHouse DDL. Steps:
    1. Confirm the change is real and intended (not a transient upstream blip)
       by inspecting the partition that hard-failed.
    2. Edit the relevant ``*_CONTRACT`` below (add/rename the column, adjust the
       dtype). Add a one-line comment noting the source change and date.
    3. Update the matching fixtures in tests/dagster/test_source_contracts.py.
    4. Re-run the asset for the failed partition; the hard-fail clears.
  A bump is a deliberate, reviewable event -- never widen a contract just to
  silence an alert without understanding what drifted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import pandera.pandas as pa
from pandera.errors import SchemaErrors


class SchemaContractViolation(Exception):
    """A structural (schema-tier) contract violation at the ingestion boundary.

    Renamed/missing column, dtype change, or empty frame. Left uncaught in a
    Dagster asset, this hard-fails the partition and fires the QNT-62 Discord
    run-failure sensor -- the correct response, because row-quarantining cannot
    fix a broken source shape.
    """


@dataclass(frozen=True)
class ContractValueReject:
    """One row dropped by a VALUE-tier contract check.

    Generic (no Dagster import) so ``shared`` stays dependency-light; the
    ingestion asset maps this to its ``rejects.Reject`` for the
    ``ingest_rejects`` sink.
    """

    index: Any
    column: str
    check: str
    failure_case: Any


@dataclass
class ContractResult:
    """Outcome of a passing-or-value-only validation.

    ``valid_df`` is the input frame with value-rejected rows removed (identical
    to the input when nothing tripped). ``value_rejects`` lists the dropped
    cells for the reject sink.
    """

    valid_df: pd.DataFrame
    value_rejects: list[ContractValueReject] = field(default_factory=list)


def _is_schema_tier(schema_context: Any, check: Any) -> bool:
    """Classify a pandera failure case as schema-tier (vs. value-tier).

    Schema-tier == the frame's *shape* is wrong: a missing/renamed column
    (reported at ``DataFrameSchema`` context) or a column dtype mismatch
    (check string ``dtype(...)`` / ``coerce_dtype(...)``). Everything else is an
    element-wise value check on a present, correctly-typed column.
    """
    if schema_context != "Column":
        return True
    check_str = str(check)
    return check_str.startswith("dtype(") or check_str.startswith("coerce_dtype(")


def validate_contract(df: pd.DataFrame, schema: pa.DataFrameSchema) -> ContractResult:
    """Validate a source frame against its contract at the ingestion boundary.

    Raises ``SchemaContractViolation`` on any schema-tier problem (empty frame,
    missing/renamed column, dtype drift). Returns a ``ContractResult`` when the
    frame is well-shaped -- with any value-tier violations removed from
    ``valid_df`` and listed in ``value_rejects`` for the reject sink.
    """
    if df.empty:
        raise SchemaContractViolation(f"{schema.name}: empty frame at ingestion boundary")

    try:
        validated = schema.validate(df, lazy=True)
    except SchemaErrors as exc:
        failures = exc.failure_cases
        schema_mask = failures.apply(
            lambda r: _is_schema_tier(r["schema_context"], r["check"]), axis=1
        )
        schema_failures = failures[schema_mask]
        if not schema_failures.empty:
            details = ", ".join(
                sorted(
                    {
                        f"{row['check']} [{row['failure_case']}]"
                        for _, row in schema_failures.iterrows()
                    }
                )
            )
            raise SchemaContractViolation(
                f"{schema.name}: schema-tier violation ({details})"
            ) from exc

        value_failures = failures[~schema_mask]
        value_rejects = [
            ContractValueReject(
                index=row["index"],
                column=str(row["column"]),
                check=str(row["check"]),
                failure_case=row["failure_case"],
            )
            for _, row in value_failures.iterrows()
        ]
        bad_index = list(dict.fromkeys(value_failures["index"].tolist()))
        valid_df = df.drop(index=bad_index)
        return ContractResult(valid_df=valid_df, value_rejects=value_rejects)

    return ContractResult(valid_df=validated, value_rejects=[])


# ── Contracts ────────────────────────────────────────────────────────────────
#
# One per ingestion source. Each is the executable spec of the frame the asset
# is about to write. ``strict=False`` tolerates extra/reordered source columns;
# ``coerce=False`` so dtype drift is detected (a schema-tier hard-fail) rather
# than silently coerced.

# yfinance OHLCV (validated on the normalised source frame, before our own
# derived columns). Columns after ohlcv_raw's normalisation: date + the five
# price columns + volume. Prices are nullable (NaN on a non-trading row is
# benign, not a reject); volume must be non-negative (impossible otherwise).
OHLCV_CONTRACT = pa.DataFrameSchema(
    {
        "date": pa.Column("datetime64[ns]", nullable=False, required=True),
        "open": pa.Column(float, nullable=True, required=True),
        "high": pa.Column(float, nullable=True, required=True),
        "low": pa.Column(float, nullable=True, required=True),
        "close": pa.Column(float, nullable=True, required=True),
        "adj_close": pa.Column(float, nullable=True, required=True),
        # nullable=False: a NaN volume cannot coerce to int64 downstream
        # (df["volume"].astype) -- quarantine that single row (value-tier) rather
        # than letting it pass the contract and crash the whole partition later.
        "volume": pa.Column(nullable=False, required=True, checks=pa.Check.ge(0)),
    },
    strict=False,
    coerce=False,
    name="ohlcv",
)

# yfinance fundamentals (validated on the assembled per-period frame -- yfinance
# hands statements indexed by line-item, not a flat frame, so the contracted
# "source shape" is the normalised per-period frame the extractor produces).
# period_type is the enum the extractor sets; a value outside it is a value-tier
# reject. shares counts must be non-negative.
FUNDAMENTALS_CONTRACT = pa.DataFrameSchema(
    {
        "ticker": pa.Column(str, nullable=False, required=True),
        "period_end": pa.Column(nullable=False, required=True),
        "period_type": pa.Column(
            str, nullable=False, required=True, checks=pa.Check.isin(["quarterly", "annual"])
        ),
        "revenue": pa.Column(float, nullable=True, required=True),
        "gross_profit": pa.Column(float, nullable=True, required=True),
        "net_income": pa.Column(float, nullable=True, required=True),
        "total_assets": pa.Column(float, nullable=True, required=True),
        "total_liabilities": pa.Column(float, nullable=True, required=True),
        "current_assets": pa.Column(float, nullable=True, required=True),
        "current_liabilities": pa.Column(float, nullable=True, required=True),
        "free_cash_flow": pa.Column(float, nullable=True, required=True),
        "ebitda": pa.Column(float, nullable=True, required=True),
        "total_debt": pa.Column(float, nullable=True, required=True),
        "cash_and_equivalents": pa.Column(float, nullable=True, required=True),
        # nullable=True (QNT-382): share counts are per-period from the balance
        # sheet; a period yfinance has no count for legitimately carries NULL
        # (the columns are Nullable(UInt64) and the asset inserts None, not NaN).
        # ge(0) still quarantines a negative count on the rows that have one.
        "shares_outstanding": pa.Column(nullable=True, required=True, checks=pa.Check.ge(0)),
        "implied_shares_outstanding": pa.Column(
            nullable=True, required=True, checks=pa.Check.ge(0)
        ),
        "market_cap": pa.Column(float, nullable=True, required=True),
    },
    strict=False,
    coerce=False,
    name="fundamentals",
)

# Finnhub /company-news (validated on the raw article frame, before per-article
# processing -- so a renamed/missing Finnhub key is a schema-tier hard-fail
# rather than silently becoming an "unusable" reject). Only the keys the asset
# depends on are required; per-row value handling (empty headline, bad datetime)
# stays in the asset's existing _article_to_row drop path, so no value checks are
# attached here.
NEWS_RAW_CONTRACT = pa.DataFrameSchema(
    {
        "headline": pa.Column(str, nullable=True, required=True),
        "url": pa.Column(str, nullable=True, required=True),
        "datetime": pa.Column(nullable=True, required=True),
        "summary": pa.Column(str, nullable=True, required=True),
    },
    strict=False,
    coerce=False,
    name="news_raw",
)

# EDGAR 8-K earnings releases (QNT-260). EDGAR hands us a discovery JSON
# (efts.sec.gov full-text search) plus per-filing HTML, so — like fundamentals —
# the contracted "source shape" is the assembled per-release frame the asset
# builds just before the ClickHouse write, not a single raw payload. A renamed
# EFTS key or a missing column is a schema-tier hard-fail; an empty body (HTML
# clean produced nothing) is a value-tier reject routed to ingest_rejects, since
# a release we can't extract narrative from has no RAG value. filing_date is
# pinned to datetime64 (the asset normalises EDGAR's YYYY-MM-DD strings) so a
# date-parse regression is caught here, not as a silent epoch-0 row downstream.
EARNINGS_RELEASE_CONTRACT = pa.DataFrameSchema(
    {
        "doc_id": pa.Column(nullable=False, required=True, checks=pa.Check.ge(0)),
        "ticker": pa.Column(str, nullable=False, required=True),
        "cik": pa.Column(str, nullable=False, required=True),
        "accession": pa.Column(str, nullable=False, required=True),
        "form": pa.Column(str, nullable=False, required=True),
        "items": pa.Column(str, nullable=False, required=True),
        "filing_date": pa.Column("datetime64[ns]", nullable=False, required=True),
        # period_ending is intentionally omitted: it is a Nullable(Date) column a
        # missing EDGAR value legitimately leaves NULL, so it is not part of the
        # contracted shape. Add it here if a future schema change makes it load-
        # bearing (so a rename/type drift on it would be caught).
        "exhibit": pa.Column(str, nullable=False, required=True),
        "title": pa.Column(str, nullable=True, required=True),
        "url": pa.Column(str, nullable=False, required=True),
        # Empty/whitespace body is a value-tier reject: the filing was discovered
        # and fetched, but HTML cleaning yielded no narrative — quarantine the row
        # rather than embedding an empty document downstream.
        "body": pa.Column(
            str, nullable=False, required=True, checks=pa.Check.str_length(min_value=1)
        ),
    },
    strict=False,
    coerce=False,
    name="earnings_release",
)
