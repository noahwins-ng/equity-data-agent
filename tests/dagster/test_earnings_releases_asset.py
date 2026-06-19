"""Asset-level tests for earnings_releases_raw (QNT-260).

Drives the asset through Dagster's ``build_asset_context`` with the three EDGAR
network functions monkeypatched to canned data, so the row-assembly, stable
doc_id / idempotency, reject routing, and the QNT-259 contract wiring are pinned
without touching the live API. The EDGAR client's own discovery/clean/chunk
contract is covered in test_edgar_feeds.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import httpx
import pandas as pd
from dagster import build_asset_context
from dagster_pipelines.assets.earnings_releases_raw import (
    EarningsReleasesConfig,
    _doc_id,
    earnings_releases_raw,
)
from dagster_pipelines.edgar_feeds import FilingRef
from dagster_pipelines.rejects import REJECTS_TABLE

# The asset function shadows the submodule of the same name on the package, so
# __import__ with a fromlist is how to reach the module object for monkeypatch
# (same trick test_source_contracts uses for ohlcv_raw / fundamentals).
asset_mod = __import__(
    "dagster_pipelines.assets.earnings_releases_raw", fromlist=["discover_earnings_filings"]
)

_TABLE = "equity_raw.earnings_releases_raw"


@dataclass
class _RecordingClickHouse:
    inserts: list[tuple[str, pd.DataFrame]] = field(default_factory=list)
    # Accessions the table already holds for the partition ticker — the
    # skip-already-ingested short-circuit queries these before any fetch.
    existing_accessions: list[str] = field(default_factory=list)

    def query_df(self, query: str, parameters: dict | None = None) -> pd.DataFrame:
        return pd.DataFrame({"accession": self.existing_accessions})

    def insert_df(self, table: str, df: pd.DataFrame) -> None:
        self.inserts.append((table, df))

    def table_rows(self) -> pd.DataFrame:
        frames = [df for table, df in self.inserts if table == _TABLE]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def reject_rows(self) -> pd.DataFrame:
        frames = [df for table, df in self.inserts if table == REJECTS_TABLE]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _filing(accession: str, filing_date: date) -> FilingRef:
    return FilingRef(
        ticker="NVDA",
        cik="0001045810",
        accession=accession,
        filing_date=filing_date,
        period_ending=filing_date,
        items="2.02,9.01",
        title="NVIDIA CORP",
    )


def _patch_edgar(
    monkeypatch,
    *,
    filings: list[FilingRef],
    resolve,
    body,
) -> None:
    monkeypatch.setattr(asset_mod, "discover_earnings_filings", lambda *a, **k: filings)
    monkeypatch.setattr(asset_mod, "resolve_exhibit", resolve)
    monkeypatch.setattr(asset_mod, "fetch_clean_text", body)


def test_asset_assembles_rows_with_stable_doc_id(monkeypatch) -> None:
    filings = [_filing("0001045810-25-000228", date(2025, 11, 19))]
    url = "https://www.sec.gov/Archives/edgar/data/1045810/000104581025000228/pr.htm"
    _patch_edgar(
        monkeypatch,
        filings=filings,
        resolve=lambda f, client: ("EX-99.1", url),
        body=lambda u, client: (
            "NVIDIA Announces Record Results\nRevenue rose sharply this quarter."
        ),
    )

    ch = _RecordingClickHouse()
    ctx = build_asset_context(partition_key="NVDA")
    earnings_releases_raw(ctx, EarningsReleasesConfig(), clickhouse=ch)  # type: ignore[arg-type]

    rows = ch.table_rows()
    assert len(rows) == 1
    row = rows.iloc[0]
    assert row["doc_id"] == _doc_id(url)  # stable, derived from the exhibit URL
    assert row["ticker"] == "NVDA"
    assert row["exhibit"] == "EX-99.1"
    assert row["form"] == "8-K"
    # Title comes from the first cleaned line of the body.
    assert row["title"] == "NVIDIA Announces Record Results"
    assert ch.reject_rows().empty


def test_asset_routes_empty_body_to_reject_sink(monkeypatch) -> None:
    filings = [_filing("0001045810-25-000228", date(2025, 11, 19))]
    _patch_edgar(
        monkeypatch,
        filings=filings,
        resolve=lambda f, client: ("EX-99.1", "https://sec.gov/x.htm"),
        body=lambda u, client: "   ",  # cleans to whitespace -> empty
    )

    ch = _RecordingClickHouse()
    ctx = build_asset_context(partition_key="NVDA")
    earnings_releases_raw(ctx, EarningsReleasesConfig(), clickhouse=ch)  # type: ignore[arg-type]

    assert ch.table_rows().empty  # nothing written to the corpus
    rejects = ch.reject_rows()
    assert (rejects["reason"] == "empty_body").any()


def test_asset_routes_unresolved_exhibit_to_reject_sink(monkeypatch) -> None:
    filings = [_filing("0001045810-25-000228", date(2025, 11, 19))]
    _patch_edgar(
        monkeypatch,
        filings=filings,
        resolve=lambda f, client: None,  # no exhibit found
        body=lambda u, client: "unused",
    )

    ch = _RecordingClickHouse()
    ctx = build_asset_context(partition_key="NVDA")
    earnings_releases_raw(ctx, EarningsReleasesConfig(), clickhouse=ch)  # type: ignore[arg-type]

    assert ch.table_rows().empty
    assert (ch.reject_rows()["reason"] == "exhibit_unresolved").any()


def test_asset_records_per_document_fetch_error(monkeypatch) -> None:
    filings = [_filing("0001045810-25-000228", date(2025, 11, 19))]

    def _boom(u, client):
        raise httpx.HTTPStatusError(
            "500", request=httpx.Request("GET", u), response=httpx.Response(500)
        )

    _patch_edgar(
        monkeypatch,
        filings=filings,
        resolve=lambda f, client: ("EX-99.1", "https://sec.gov/x.htm"),
        body=_boom,
    )

    ch = _RecordingClickHouse()
    ctx = build_asset_context(partition_key="NVDA")
    earnings_releases_raw(ctx, EarningsReleasesConfig(), clickhouse=ch)  # type: ignore[arg-type]

    assert ch.table_rows().empty
    assert (ch.reject_rows()["reason"] == "fetch_error").any()


def test_asset_idempotent_doc_id_across_runs(monkeypatch) -> None:
    """Same release re-fetched yields the same doc_id (ReplacingMergeTree dedup)."""
    filings = [_filing("0001045810-25-000228", date(2025, 11, 19))]
    url = "https://www.sec.gov/Archives/edgar/data/1045810/000104581025000228/pr.htm"
    _patch_edgar(
        monkeypatch,
        filings=filings,
        resolve=lambda f, client: ("EX-99.1", url),
        body=lambda u, client: "Headline\nBody prose for the quarter.",
    )

    doc_ids = []
    for _ in range(2):
        ch = _RecordingClickHouse()
        ctx = build_asset_context(partition_key="NVDA")
        earnings_releases_raw(ctx, EarningsReleasesConfig(), clickhouse=ch)  # type: ignore[arg-type]
        doc_ids.append(int(ch.table_rows().iloc[0]["doc_id"]))

    assert doc_ids[0] == doc_ids[1]


def test_asset_skips_already_ingested_filings(monkeypatch) -> None:
    """A discovered filing whose accession is already stored must NOT be
    re-resolved or re-fetched — the skip-already-ingested short-circuit avoids
    the two redundant EDGAR requests per stored release on steady-state weeks."""
    new = _filing("0001045810-26-000051", date(2026, 5, 20))
    old = _filing("0001045810-25-000228", date(2025, 11, 19))
    resolve_calls: list[str] = []
    fetch_calls: list[str] = []

    def _resolve(f, client):
        resolve_calls.append(f.accession)
        return ("EX-99.1", f"https://sec.gov/{f.accession}.htm")

    def _body(u, client):
        fetch_calls.append(u)
        return "Headline\nBody prose for the quarter."

    _patch_edgar(monkeypatch, filings=[new, old], resolve=_resolve, body=_body)

    ch = _RecordingClickHouse(existing_accessions=[old.accession])
    ctx = build_asset_context(partition_key="NVDA")
    earnings_releases_raw(ctx, EarningsReleasesConfig(), clickhouse=ch)  # type: ignore[arg-type]

    # Only the new filing was resolved/fetched; the already-ingested one skipped.
    assert resolve_calls == [new.accession]
    assert len(fetch_calls) == 1
    rows = ch.table_rows()
    assert list(rows["accession"]) == [new.accession]


def test_asset_no_new_filings_skips_insert(monkeypatch) -> None:
    """When every discovered filing is already ingested, the asset fetches
    nothing, writes no rows, and still emits the 0-count reject metric."""
    f = _filing("0001045810-25-000228", date(2025, 11, 19))
    resolved = False

    def _resolve(filing, client):
        nonlocal resolved
        resolved = True
        return ("EX-99.1", "https://sec.gov/x.htm")

    _patch_edgar(monkeypatch, filings=[f], resolve=_resolve, body=lambda u, client: "x\ny")

    ch = _RecordingClickHouse(existing_accessions=[f.accession])
    ctx = build_asset_context(partition_key="NVDA")
    earnings_releases_raw(ctx, EarningsReleasesConfig(), clickhouse=ch)  # type: ignore[arg-type]

    assert resolved is False  # never reached the fetch path
    assert ch.table_rows().empty
    assert ch.reject_rows().empty  # 0-count emitted as metadata, no rows written
