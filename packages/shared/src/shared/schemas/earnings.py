from datetime import date, datetime

from pydantic import BaseModel


class EarningsReleaseRow(BaseModel):
    """Maps to equity_raw.earnings_releases_raw.

    One row per 8-K Item 2.02 earnings release (QNT-260). ``body`` is the
    cleaned narrative text of the EX-99.1 press release — the RAG-material
    portion of the filing (management framing + guidance). The quantitative
    numbers already land in the fundamentals table, so only the narrative is
    embedded downstream (equity_earnings Qdrant collection).

    ``doc_id`` reuses the blake2b URL-hash scheme used for news (and Qdrant
    point ids), so re-runs are idempotent on ReplacingMergeTree.
    """

    doc_id: int  # blake2b(url) truncated to UInt64
    ticker: str
    cik: str
    accession: str  # EDGAR accession number, e.g. "0001045810-25-000228"
    form: str  # always "8-K" for this corpus
    items: str  # comma-separated 8-K item codes, e.g. "2.02,9.01"
    filing_date: date
    period_ending: date | None = None
    exhibit: str  # source exhibit type, e.g. "EX-99.1"
    title: str  # release headline / EDGAR display name
    url: str  # EX-99.1 document URL on www.sec.gov/Archives
    body: str  # cleaned narrative text
    fetched_at: datetime | None = None
