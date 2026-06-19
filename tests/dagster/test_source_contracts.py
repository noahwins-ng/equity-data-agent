"""Source-boundary data-contract tests (QNT-259).

Pins the two-tier failure policy:

  * SCHEMA violation (renamed/missing column, dtype change, empty frame) ->
    ``validate_contract`` raises ``SchemaContractViolation``, and -- wired into
    an ingestion asset -- that propagates out of the asset body (hard-fails the
    Dagster partition, which fires the QNT-62 Discord run-failure sensor).
  * VALUE violation (out-of-range cell) -> the row is quarantined via the
    ``ingest_rejects`` sink and the clean rows proceed; the asset does NOT fail.

The contract-level tests exercise each mutated fixture against the helper; the
asset-level tests prove the policy end-to-end through ``ohlcv_raw`` /
``fundamentals`` (schema -> raises, value -> reject row lands).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
import pytest
from dagster import build_asset_context
from dagster_pipelines.assets.fundamentals import fundamentals
from dagster_pipelines.assets.ohlcv_raw import OHLCVConfig, ohlcv_raw
from dagster_pipelines.rejects import REJECTS_TABLE
from shared.contracts import (
    EARNINGS_RELEASE_CONTRACT,
    FUNDAMENTALS_CONTRACT,
    NEWS_RAW_CONTRACT,
    OHLCV_CONTRACT,
    ContractResult,
    SchemaContractViolation,
    validate_contract,
)

_OHLCV_MODULE = __import__("dagster_pipelines.assets.ohlcv_raw", fromlist=["yf"])
_FUND_MODULE = __import__("dagster_pipelines.assets.fundamentals", fromlist=["yf"])


# ── fixtures ──────────────────────────────────────────────────────────────────


def _ohlcv_source_frame() -> pd.DataFrame:
    """A clean normalised yfinance OHLCV frame (columns as ohlcv_raw produces
    them right after normalisation, before its derived columns)."""
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-02", "2026-01-05"]),
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.5, 102.5],
            "adj_close": [101.5, 102.5],
            "volume": [1_000_000, 1_200_000],
        }
    )


def _yf_download_frame() -> pd.DataFrame:
    """What ``yf.download`` returns: a DatetimeIndex + capitalised columns.

    ohlcv_raw lowercases and resets the index, so this drives the asset end to
    end through its real normalisation path.
    """
    idx = pd.DatetimeIndex(pd.to_datetime(["2026-01-02", "2026-01-05"]), name="Date")
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0],
            "High": [102.0, 103.0],
            "Low": [99.0, 100.0],
            "Close": [101.5, 102.5],
            "Adj Close": [101.5, 102.5],
            "Volume": [1_000_000, 1_200_000],
        },
        index=idx,
    )


# ── contract-level: schema tier raises ────────────────────────────────────────


def test_renamed_column_raises() -> None:
    bad = _ohlcv_source_frame().rename(columns={"close": "closing_price"})
    with pytest.raises(SchemaContractViolation):
        validate_contract(bad, OHLCV_CONTRACT)


def test_dropped_column_raises() -> None:
    bad = _ohlcv_source_frame().drop(columns=["volume"])
    with pytest.raises(SchemaContractViolation):
        validate_contract(bad, OHLCV_CONTRACT)


def test_dtype_change_raises() -> None:
    bad = _ohlcv_source_frame().assign(close=["oops", "still-oops"])
    with pytest.raises(SchemaContractViolation):
        validate_contract(bad, OHLCV_CONTRACT)


def test_empty_frame_raises() -> None:
    with pytest.raises(SchemaContractViolation):
        validate_contract(_ohlcv_source_frame().iloc[0:0], OHLCV_CONTRACT)


def test_extra_column_is_tolerated() -> None:
    """Extra/reordered source columns don't break us -> not a violation."""
    ok = _ohlcv_source_frame().assign(dividends=[0.0, 0.0])
    result = validate_contract(ok, OHLCV_CONTRACT)
    assert len(result.valid_df) == 2


# ── contract-level: value tier quarantines ────────────────────────────────────


def test_value_violation_quarantines_row_not_raises() -> None:
    """A negative-volume row is dropped from valid_df and surfaced as a value
    reject -- the frame's shape is fine, so no hard-fail."""
    frame = _ohlcv_source_frame()
    frame.loc[1, "volume"] = -5
    result: ContractResult = validate_contract(frame, OHLCV_CONTRACT)
    assert len(result.valid_df) == 1
    assert result.valid_df["volume"].tolist() == [1_000_000]
    assert [(r.column, r.failure_case) for r in result.value_rejects] == [("volume", -5)]


def test_nan_volume_quarantines_not_crashes() -> None:
    """A NaN volume can't coerce to int64 downstream, so volume is nullable=False:
    the NaN row is quarantined (value-tier) and the clean row proceeds, rather
    than passing the contract and crashing the partition at astype()."""
    frame = _ohlcv_source_frame()
    frame["volume"] = frame["volume"].astype(float)
    frame.loc[1, "volume"] = float("nan")
    result = validate_contract(frame, OHLCV_CONTRACT)
    assert result.valid_df["volume"].tolist() == [1_000_000.0]
    assert [r.column for r in result.value_rejects] == ["volume"]


def test_news_contract_missing_finnhub_key_raises() -> None:
    """A Finnhub payload missing the 'headline' key is a schema-tier hard-fail,
    not a silent per-article 'unusable' degrade."""
    articles = pd.DataFrame([{"url": "https://x/a", "datetime": 1_714_000_000, "summary": "..."}])
    with pytest.raises(SchemaContractViolation):
        validate_contract(articles, NEWS_RAW_CONTRACT)


def _fundamentals_frame() -> pd.DataFrame:
    """A clean assembled per-period fundamentals frame (one quarterly row)."""
    floats = [
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
        "market_cap",
    ]
    row = {
        "ticker": "AAPL",
        "period_end": pd.Timestamp("2026-03-31").date(),
        "period_type": "quarterly",
        "shares_outstanding": 1_000,
        "implied_shares_outstanding": 1_000,
        **{c: 1.0 for c in floats},
    }
    return pd.DataFrame([row])


def test_fundamentals_dtype_drift_raises() -> None:
    """A fundamentals contract also hard-fails on dtype drift (revenue -> str),
    exercising the schema-tier path for a second contract."""
    bad = _fundamentals_frame().assign(revenue=["not-a-number"])
    with pytest.raises(SchemaContractViolation):
        validate_contract(bad, FUNDAMENTALS_CONTRACT)


def test_fundamentals_clean_frame_passes() -> None:
    """The clean fundamentals frame validates with no rejects (calibration guard)."""
    result = validate_contract(_fundamentals_frame(), FUNDAMENTALS_CONTRACT)
    assert len(result.valid_df) == 1 and result.value_rejects == []


# ── asset-level: policy end to end ────────────────────────────────────────────


@dataclass
class _RecordingClickHouse:
    inserts: list[tuple[str, pd.DataFrame]] = field(default_factory=list)

    def insert_df(self, table: str, df: pd.DataFrame) -> None:
        self.inserts.append((table, df))

    def reject_rows(self) -> pd.DataFrame:
        frames = [df for table, df in self.inserts if table == REJECTS_TABLE]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def test_ohlcv_asset_hard_fails_on_schema_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """yfinance renaming a column makes the asset raise -- the partition fails
    (and the QNT-62 sensor fires) rather than writing a malformed frame."""
    drifted = _yf_download_frame().rename(columns={"Close": "ClosingPrice"})
    monkeypatch.setattr(_OHLCV_MODULE.yf, "download", lambda *a, **k: drifted)
    monkeypatch.setattr(_OHLCV_MODULE.time, "sleep", lambda _s: None)

    ch = _RecordingClickHouse()
    ctx = build_asset_context(partition_key="NVDA")
    with pytest.raises(SchemaContractViolation):
        ohlcv_raw(ctx, OHLCVConfig(period="5d"), clickhouse=ch)  # type: ignore[arg-type]

    # Nothing was written to the OHLCV table on a hard-fail.
    assert not any(table == "equity_raw.ohlcv_raw" for table, _ in ch.inserts)


def test_ohlcv_asset_quarantines_value_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A negative-volume row routes to the reject sink; the clean row still
    lands in the OHLCV table and the asset succeeds."""
    frame = _yf_download_frame()
    frame.loc[frame.index[1], "Volume"] = -5
    monkeypatch.setattr(_OHLCV_MODULE.yf, "download", lambda *a, **k: frame)
    monkeypatch.setattr(_OHLCV_MODULE.time, "sleep", lambda _s: None)

    ch = _RecordingClickHouse()
    ctx = build_asset_context(partition_key="NVDA")
    ohlcv_raw(ctx, OHLCVConfig(period="5d"), clickhouse=ch)  # type: ignore[arg-type]

    rejects = ch.reject_rows()
    assert len(rejects) == 1
    assert rejects["reason"].iloc[0] == "contract_value_violation"
    ohlcv_writes = [df for table, df in ch.inserts if table == "equity_raw.ohlcv_raw"]
    assert len(ohlcv_writes) == 1
    assert len(ohlcv_writes[0]) == 1  # only the clean row


def test_fundamentals_asset_quarantines_bad_period_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """A period_type outside the {quarterly, annual} enum is a value violation:
    the row is quarantined and the asset does not hard-fail."""
    quarterly = pd.DataFrame(
        {"Total Revenue": [100.0], "Gross Profit": [60.0], "Net Income": [20.0]},
        index=pd.to_datetime(["2026-03-31"]),
    ).T

    class _FakeStock:
        def __init__(self) -> None:
            self.info = {"ebitda": 0, "sharesOutstanding": 1, "marketCap": 0}
            self.quarterly_financials = quarterly
            self.quarterly_balance_sheet = pd.DataFrame()
            self.quarterly_cashflow = pd.DataFrame()
            self.financials = pd.DataFrame()
            self.balance_sheet = pd.DataFrame()
            self.cashflow = pd.DataFrame()

    monkeypatch.setattr(_FUND_MODULE.yf, "Ticker", lambda _t: _FakeStock())
    monkeypatch.setattr(_FUND_MODULE.time, "sleep", lambda _s: None)
    # Force the extracted row's period_type out of the enum to drive the value tier.
    orig = _FUND_MODULE._extract_periods

    def _mutated(*args: object, **kwargs: object) -> list[dict]:
        rows = orig(*args, **kwargs)  # type: ignore[arg-type]
        for r in rows:
            r["period_type"] = "monthly"
        return rows

    monkeypatch.setattr(_FUND_MODULE, "_extract_periods", _mutated)

    ch = _RecordingClickHouse()
    ctx = build_asset_context(partition_key="AAPL")
    fundamentals(ctx, clickhouse=ch)  # type: ignore[arg-type]

    rejects = ch.reject_rows()
    assert (rejects["reason"] == "contract_value_violation").any()
    # The malformed row was dropped before the fundamentals write.
    fund_writes = [df for table, df in ch.inserts if table == "equity_raw.fundamentals"]
    assert all(df.empty for df in fund_writes) or not fund_writes


def test_clean_inputs_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4 guard at unit scope: a clean OHLCV payload produces no rejects and
    writes every row -- the contract layer adds no false hard-fail."""
    monkeypatch.setattr(_OHLCV_MODULE.yf, "download", lambda *a, **k: _yf_download_frame())
    monkeypatch.setattr(_OHLCV_MODULE.time, "sleep", lambda _s: None)

    ch = _RecordingClickHouse()
    ctx = build_asset_context(partition_key="NVDA")
    ohlcv_raw(ctx, OHLCVConfig(period="5d"), clickhouse=ch)  # type: ignore[arg-type]

    assert ch.reject_rows().empty
    ohlcv_writes = [df for table, df in ch.inserts if table == "equity_raw.ohlcv_raw"]
    assert len(ohlcv_writes) == 1 and len(ohlcv_writes[0]) == 2


# ── EARNINGS_RELEASE_CONTRACT (QNT-260) ──────────────────────────────────────


def _earnings_source_frame() -> pd.DataFrame:
    """A clean assembled earnings-release frame (columns as
    earnings_releases_raw builds them right before the ClickHouse write)."""
    return pd.DataFrame(
        {
            "doc_id": [123, 456],
            "ticker": ["NVDA", "NVDA"],
            "cik": ["0001045810", "0001045810"],
            "accession": ["0001045810-25-000228", "0001045810-25-000115"],
            "form": ["8-K", "8-K"],
            "items": ["2.02,9.01", "2.02,9.01"],
            "filing_date": pd.to_datetime(["2025-11-19", "2025-05-28"]),
            "exhibit": ["EX-99.1", "EX-99.1"],
            "title": ["NVIDIA Announces Q3 Results", "NVIDIA Announces Q1 Results"],
            "url": ["https://sec.gov/a.htm", "https://sec.gov/b.htm"],
            "body": ["Record revenue narrative ...", "Strong quarter narrative ..."],
        }
    )


def test_earnings_clean_frame_passes() -> None:
    result = validate_contract(_earnings_source_frame(), EARNINGS_RELEASE_CONTRACT)
    assert not result.value_rejects
    assert len(result.valid_df) == 2


def test_earnings_missing_column_is_schema_violation() -> None:
    df = _earnings_source_frame().drop(columns=["body"])
    with pytest.raises(SchemaContractViolation):
        validate_contract(df, EARNINGS_RELEASE_CONTRACT)


def test_earnings_empty_body_is_value_reject() -> None:
    df = _earnings_source_frame()
    df.loc[0, "body"] = ""  # cleaned to nothing -> quarantine, not crash
    result = validate_contract(df, EARNINGS_RELEASE_CONTRACT)
    assert len(result.value_rejects) == 1
    assert result.value_rejects[0].column == "body"
    # The clean row survives.
    assert len(result.valid_df) == 1
    assert result.valid_df.iloc[0]["doc_id"] == 456


def test_earnings_filing_date_dtype_drift_is_schema_violation() -> None:
    df = _earnings_source_frame()
    df["filing_date"] = ["2025-11-19", "2025-05-28"]  # strings, not datetime64
    with pytest.raises(SchemaContractViolation):
        validate_contract(df, EARNINGS_RELEASE_CONTRACT)
