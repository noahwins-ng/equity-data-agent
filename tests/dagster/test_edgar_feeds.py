"""Tests for the SEC EDGAR earnings-release client (QNT-260).

Hermetic — every network call is driven through ``httpx.MockTransport`` with a
canned EDGAR payload, so the discovery → exhibit-resolution → clean → chunk
contract is pinned without touching the live API. A vendor field rename or a
shape change in the EFTS hit / SGML manifest surfaces here, not in production.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest
from dagster_pipelines.edgar_feeds import (
    TICKER_CIK,
    FilingRef,
    chunk_release,
    clean_html,
    discover_earnings_filings,
    resolve_exhibit,
)
from shared.tickers import TICKERS

# ── CIK registry ──────────────────────────────────────────────────────────────


def test_cik_map_covers_every_ticker() -> None:
    # The module-level assert already enforces this at import; pin it explicitly
    # so a future ticker addition fails in this test, not only at import time.
    assert set(TICKER_CIK) == set(TICKERS)
    assert all(len(cik) == 10 and cik.isdigit() for cik in TICKER_CIK.values())


# ── discover_earnings_filings ───────────────────────────────────────────────


def _efts_hit(adsh: str, file_date: str, items: list[str]) -> dict[str, Any]:
    return {
        "_id": f"{adsh}:doc.htm",
        "_source": {
            "ciks": ["0001045810"],
            "adsh": adsh,
            "file_date": file_date,
            "period_ending": file_date,
            "items": items,
            "form": "8-K",
            "display_names": ["NVIDIA CORP  (NVDA)  (CIK 0001045810)"],
        },
    }


def _efts_client(payload: dict[str, Any]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_discover_filters_to_item_202_and_dedups() -> None:
    payload = {
        "hits": {
            "hits": [
                _efts_hit("0001045810-25-000228", "2025-11-19", ["2.02", "9.01"]),
                # Same filing, second matching document — must dedup by accession.
                {
                    "_id": "0001045810-25-000228:ex991.htm",
                    "_source": {
                        "ciks": ["0001045810"],
                        "adsh": "0001045810-25-000228",
                        "file_date": "2025-11-19",
                        "period_ending": "2025-10-26",
                        "items": ["2.02", "9.01"],
                        "form": "8-K",
                        "display_names": ["NVIDIA CORP  (NVDA)"],
                    },
                },
                # A non-earnings 8-K (no Item 2.02) — must be dropped.
                _efts_hit("0001045810-25-000300", "2025-12-01", ["5.02"]),
                _efts_hit("0001045810-25-000115", "2025-05-28", ["2.02", "9.01"]),
            ]
        }
    }
    filings = discover_earnings_filings(
        "NVDA",
        since=date(2025, 1, 1),
        until=date(2025, 12, 31),
        client=_efts_client(payload),
    )
    accessions = [f.accession for f in filings]
    assert accessions == ["0001045810-25-000228", "0001045810-25-000115"]  # newest first
    assert "0001045810-25-000300" not in accessions  # item filter dropped it
    assert filings[0].items == "2.02,9.01"
    assert filings[0].filing_date == date(2025, 11, 19)


def test_discover_empty_hits_returns_empty() -> None:
    filings = discover_earnings_filings(
        "NVDA",
        since=date(2025, 1, 1),
        until=date(2025, 12, 31),
        client=_efts_client({"hits": {"hits": []}}),
    )
    assert filings == []


# ── resolve_exhibit ──────────────────────────────────────────────────────────

_MANIFEST = """<SEC-HEADER>
<DOCUMENT>
<TYPE>8-K
<FILENAME>nvda-20251119.htm
<DESCRIPTION>8-K
<DOCUMENT>
<TYPE>EX-99.1
<FILENAME>q3fy26pr.htm
<DESCRIPTION>EX-99.1
<DOCUMENT>
<TYPE>EX-99.2
<FILENAME>q3fy26cfocommentary.htm
<DESCRIPTION>EX-99.2
"""


def _filing() -> FilingRef:
    return FilingRef(
        ticker="NVDA",
        cik="0001045810",
        accession="0001045810-25-000228",
        filing_date=date(2025, 11, 19),
        period_ending=date(2025, 10, 26),
        items="2.02,9.01",
        title="NVIDIA CORP",
    )


def test_resolve_exhibit_picks_ex_991() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(206, text=_MANIFEST)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = resolve_exhibit(_filing(), client=client)
    assert result is not None
    exhibit_type, url = result
    assert exhibit_type == "EX-99.1"
    assert url.endswith("/1045810/000104581025000228/q3fy26pr.htm")


def test_resolve_exhibit_skips_type_block_without_filename() -> None:
    # A <DOCUMENT> with a <TYPE> but no <FILENAME> (some filers' inline cover
    # pages) must be skipped, not paired with the next block's filename. The
    # per-block scan keeps EX-99.1 resolving to its own document.
    manifest = (
        "<DOCUMENT>\n<TYPE>8-K\n<DESCRIPTION>8-K\n"  # cover: TYPE but no FILENAME
        "<DOCUMENT>\n<TYPE>EX-99.1\n<FILENAME>pr.htm\n<DESCRIPTION>EX-99.1\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(206, text=manifest)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = resolve_exhibit(_filing(), client=client)
    assert result is not None
    exhibit_type, url = result
    assert exhibit_type == "EX-99.1"
    assert url.endswith("pr.htm")


def test_resolve_exhibit_falls_back_to_primary_htm_when_no_ex99() -> None:
    manifest = "<DOCUMENT>\n<TYPE>8-K\n<FILENAME>only-cover.htm\n<DESCRIPTION>8-K\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(206, text=manifest)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = resolve_exhibit(_filing(), client=client)
    assert result is not None
    exhibit_type, url = result
    assert exhibit_type == "8-K"
    assert url.endswith("only-cover.htm")


# ── clean_html ───────────────────────────────────────────────────────────────


def test_clean_html_strips_wrapper_and_tags() -> None:
    html = """
    <html><head><style>.x{}</style></head><body>
    <span>EX-99.1</span><br><span>2</span><br><span>q3fy26pr.htm</span><br>
    <span>Document</span><br>
    <p>NVIDIA Announces Financial Results for Third Quarter Fiscal 2026</p>
    <p>Record revenue of $57.0 billion, up 62% from a year ago.</p>
    <script>ignore()</script>
    </body></html>
    """
    text = clean_html(html)
    lines = text.splitlines()
    # iXBRL wrapper tokens dropped; real headline is the first line.
    assert lines[0] == "NVIDIA Announces Financial Results for Third Quarter Fiscal 2026"
    assert "Record revenue of $57.0 billion" in text
    assert "ignore()" not in text  # script content removed


def test_clean_html_strips_word_form_exhibit_wrapper() -> None:
    # Apple's filer wrapper spells out "Exhibit 99.1" (vs NVIDIA's "EX-99.1"),
    # so the stripper must catch both forms or the title leaks the boilerplate.
    html = (
        "<body><span>EX-99.1</span><br><span>2</span><br>"
        "<span>a8-kex991.htm</span><br><span>EX-99.1</span><br>"
        "<span>Document</span><br><span>Exhibit 99.1</span><br>"
        "<p>Apple reports second quarter results</p>"
        "<p>Services revenue reaches a new all-time high this period.</p></body>"
    )
    lines = clean_html(html).splitlines()
    assert lines[0] == "Apple reports second quarter results"


# ── chunk_release ────────────────────────────────────────────────────────────


def test_chunk_release_sections_and_indices() -> None:
    body = "\n".join(
        [
            "NVIDIA Announces Record Results",
            "This is the opening summary paragraph with enough prose to read as body text "
            "rather than a heading, describing the quarter in narrative form.",
            "CFO Commentary",
            "The chief financial officer provided extended commentary on margins and the "
            "outlook for the coming quarter, in full sentences that read as real prose.",
        ]
    )
    chunks = chunk_release(body, max_chars=200, overlap=40)
    assert len(chunks) >= 2
    # Indices are contiguous from 0 (stable point_id derivation).
    assert [c.index for c in chunks] == list(range(len(chunks)))
    # Every chunk respects the size budget.
    assert all(len(c.text) <= 200 for c in chunks)
    # Section headings were detected (not all "Summary").
    sections = {c.section for c in chunks}
    assert "CFO Commentary" in sections


def test_chunk_release_empty_body() -> None:
    assert chunk_release("") == []


def test_chunk_release_rejects_dateline_and_email_headings() -> None:
    # The dateline and contact emails are heading-shaped (short, alphabetic) but
    # must not become section labels — they carry a comma / "@" respectively.
    body = "\n".join(
        [
            "SANTA CLARA, Calif.—May 28, 2025—",
            "NVIDIA today reported record revenue for the quarter in full prose form.",
            "press@nvidia.com",
            "Investor relations follow-up details continue here in proper sentence form.",
            "Non-GAAP Measures",
            "The company uses non-GAAP measures as a supplement, described in real prose.",
        ]
    )
    sections = {c.section for c in chunk_release(body, max_chars=300, overlap=40)}
    assert "Non-GAAP Measures" in sections
    assert not any("@" in s or "," in s for s in sections)


@pytest.mark.parametrize("ticker", ["NVDA", "AAPL", "INTC"])
def test_filing_dir_url_uses_unpadded_cik(ticker: str) -> None:
    ref = FilingRef(
        ticker=ticker,
        cik=TICKER_CIK[ticker],
        accession="0001045810-25-000228",
        filing_date=date(2025, 11, 19),
        period_ending=None,
        items="2.02",
        title="",
    )
    # CIK in the Archives path is the integer form (no zero-padding).
    assert f"/{int(TICKER_CIK[ticker])}/" in ref.filing_dir_url
    assert ref.accession_nodash == "000104581025000228"
