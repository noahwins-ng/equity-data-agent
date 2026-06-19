"""SEC EDGAR 8-K earnings-release client for the earnings corpus (QNT-260).

The second RAG corpus (after news). 8-K Item 2.02 filings carry the quarterly
earnings *narrative* (management framing + guidance) as Exhibit 99.1 — medium-
length, high signal-per-token, quarterly cadence. The quantitative numbers are
already covered by the fundamentals table, so only the narrative is RAG
material ("the LLM never does arithmetic"); see docs/v2-overall-enhancement.md
Track 2 (2.1-2.2).

Three EDGAR surfaces, all free, no API key (SEC fair-use just needs a declared
User-Agent with a contact address — ``settings.SEC_EDGAR_USER_AGENT``):

  1. Discovery — the full-text search API (``efts.sec.gov/LATEST/search-index``)
     returns 8-K hits filtered by CIK + date window; we keep the ones whose
     ``items`` contain "2.02" (earnings releases).
  2. Exhibit resolution — the full-submission ``.txt`` begins with an SGML
     manifest listing every document's ``<TYPE>`` / ``<FILENAME>``; a Range
     request fetches just that manifest (~16 KB) to locate EX-99.1 without
     downloading the multi-MB submission.
  3. Document fetch — the EX-99.1 HTML on ``www.sec.gov/Archives``, cleaned to
     text via BeautifulSoup.

``chunk_release`` does the embed-time work: section-aware chunking so each chunk
fits the MiniLM context window and carries a coarse section label for the
downstream retrieval/routing eval (QNT-261/263).
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime

import httpx
from bs4 import BeautifulSoup
from shared.config import settings
from shared.tickers import TICKERS

logger = logging.getLogger(__name__)

EFTS_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

# Ticker -> CIK (zero-padded to 10 digits, EDGAR's canonical form). Verified
# against https://www.sec.gov/files/company_tickers.json on 2026-06-19. CIKs are
# permanent identifiers — a company keeps its CIK across ticker/name changes —
# so a static map is both stable and testable, mirroring the NEWS_RELEVANCE /
# TICKER_METADATA registry pattern. The assert below pins coverage to TICKERS so
# adding a ticker without its CIK fails at import, not at request time.
TICKER_CIK: dict[str, str] = {
    "NVDA": "0001045810",
    "AAPL": "0000320193",
    "MSFT": "0000789019",
    "GOOGL": "0001652044",
    "AMZN": "0001018724",
    "META": "0001326801",
    "TSLA": "0001318605",
    "MU": "0000723125",
    "AMD": "0000002488",
    "INTC": "0000050863",
}

assert set(TICKER_CIK.keys()) == set(TICKERS), (
    "TICKER_CIK must cover every TICKERS entry. Adding a ticker requires its SEC "
    "CIK here so the EDGAR earnings-release ingest can resolve its filings."
)

# The 8-K item code for "Results of Operations and Financial Condition" — the
# earnings release. A single 8-K can report multiple items (e.g. "2.02,9.01");
# we keep any filing whose item set includes this one.
_EARNINGS_ITEM = "2.02"

# Full-text query. Unquoted (broad recall) — the ``items`` filter below is the
# precision lever, so a release that phrases its cover differently is still kept
# as long as EDGAR tagged it Item 2.02.
_EFTS_QUERY = "Results of Operations"

_REQUEST_TIMEOUT_SECONDS = 30.0

# SEC fair-use allows up to 10 req/s; we stay well under at ~4 req/s with jitter
# to remain a polite client. Process-local limiter — each Dagster run-worker is
# its own subprocess, so this is a per-partition limit (mirrors news_feeds.py).
_INTER_REQUEST_SECONDS = 0.25
_REQUEST_JITTER_SECONDS = 0.1
_last_request_at: float = 0.0

# Bytes of the full-submission .txt to Range-fetch. The SGML manifest listing
# every document's TYPE/FILENAME sits at the very top, ahead of the document
# bodies, so this is plenty to identify EX-99.1 without pulling the whole file.
_MANIFEST_BYTES = 16384

# Section-aware chunking. all-MiniLM-L6-v2 truncates past ~256 word-pieces
# (~1000 chars), so chunks target a sub-window with a small overlap so a
# sentence split across a boundary still embeds whole on one side.
_CHUNK_MAX_CHARS = 900
_CHUNK_OVERLAP_CHARS = 120
_MAX_HEADING_CHARS = 80


def edgar_headers() -> dict[str, str]:
    """Headers for every EDGAR request. SEC rejects requests without a
    contact-carrying User-Agent (403); ``settings`` supplies a valid default."""
    return {
        "User-Agent": settings.SEC_EDGAR_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
    }


def _sleep_for_rate_limit() -> None:
    """Block until the next EDGAR request is allowed by the rate budget.

    Mirrors news_feeds._sleep_for_finnhub_rate_limit: no-op if enough wall-clock
    has passed since the last call, otherwise sleeps the remainder plus jitter.
    """
    global _last_request_at
    now = time.monotonic()
    deadline = _last_request_at + _INTER_REQUEST_SECONDS
    if now < deadline:
        time.sleep(deadline - now + random.uniform(0, _REQUEST_JITTER_SECONDS))
    _last_request_at = time.monotonic()


@dataclass(frozen=True)
class FilingRef:
    """A discovered 8-K Item 2.02 earnings release, before document fetch."""

    ticker: str
    cik: str  # zero-padded 10-digit
    accession: str  # with dashes, e.g. "0001045810-25-000228"
    filing_date: date
    period_ending: date | None
    items: str  # comma-joined, e.g. "2.02,9.01"
    title: str  # EDGAR display name / file description

    @property
    def accession_nodash(self) -> str:
        return self.accession.replace("-", "")

    @property
    def filing_dir_url(self) -> str:
        """Archives directory for this filing (CIK is the un-padded integer)."""
        return f"{ARCHIVES_BASE}/{int(self.cik)}/{self.accession_nodash}"


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def discover_earnings_filings(
    ticker: str,
    *,
    since: date,
    until: date,
    client: httpx.Client | None = None,
) -> list[FilingRef]:
    """Return 8-K Item 2.02 earnings releases for ``ticker`` in [since, until].

    Queries the EDGAR full-text search API scoped to the ticker's CIK, then
    keeps hits whose ``items`` include 2.02, deduped by accession (the same
    filing surfaces once per matching document otherwise). Sorted newest-first.

    Raises KeyError if ``ticker`` has no CIK mapping (guarded by the module
    assert for portfolio tickers). HTTP errors propagate to the caller's
    Dagster RetryPolicy.
    """
    cik = TICKER_CIK[ticker]
    params = {
        "q": _EFTS_QUERY,
        "forms": "8-K",
        "ciks": cik,
        "startdt": since.isoformat(),
        "enddt": until.isoformat(),
    }

    owns_client = client is None
    http = client or httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS, headers=edgar_headers())
    try:
        _sleep_for_rate_limit()
        response = http.get(EFTS_SEARCH_URL, params=params)
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            http.close()

    hits = payload.get("hits", {}).get("hits", [])
    by_accession: dict[str, FilingRef] = {}
    for hit in hits:
        source = hit.get("_source", {})
        items = source.get("items") or []
        if _EARNINGS_ITEM not in items:
            continue
        accession = source.get("adsh")
        if not accession or accession in by_accession:
            continue
        filing_date = _parse_date(source.get("file_date"))
        if filing_date is None:
            continue
        display_names = source.get("display_names") or []
        by_accession[accession] = FilingRef(
            ticker=ticker,
            cik=cik,
            accession=accession,
            filing_date=filing_date,
            period_ending=_parse_date(source.get("period_ending")),
            items=",".join(items),
            title=(display_names[0] if display_names else source.get("file_description", "")),
        )

    return sorted(by_accession.values(), key=lambda f: f.filing_date, reverse=True)


# SGML manifest entries: each <DOCUMENT> block opens with <TYPE> then later
# <FILENAME>. The submission header lists all of them upfront, in order.
_TYPE_RE = re.compile(r"<TYPE>([^\s<]+)")
_FILENAME_RE = re.compile(r"<FILENAME>([^\s<]+)")


def resolve_exhibit(filing: FilingRef, *, client: httpx.Client) -> tuple[str, str] | None:
    """Locate the earnings-release exhibit document for ``filing``.

    Range-fetches the full-submission .txt manifest and returns
    ``(exhibit_type, document_url)`` for EX-99.1 (the press release). Falls back
    to any EX-99.x, then to the primary .htm document if no EX-99 exhibit is
    listed. Returns None if the manifest can't be parsed.
    """
    txt_url = f"{filing.filing_dir_url}/{filing.accession}.txt"
    headers = {**edgar_headers(), "Range": f"bytes=0-{_MANIFEST_BYTES}"}
    _sleep_for_rate_limit()
    response = client.get(txt_url, headers=headers)
    response.raise_for_status()
    manifest = response.text

    # Pair each <TYPE> with the <FILENAME> inside its own <DOCUMENT> block — i.e.
    # the first <FILENAME> between this <TYPE> and the next one. Scoping to the
    # block (rather than "first filename anywhere after") means a block that
    # carries a <TYPE> but no <FILENAME> (some filers' inline cover pages) is
    # skipped rather than stealing the next block's filename and mispairing.
    type_matches = list(_TYPE_RE.finditer(manifest))
    docs: list[tuple[str, str]] = []
    for i, m in enumerate(type_matches):
        block_end = type_matches[i + 1].start() if i + 1 < len(type_matches) else len(manifest)
        fn = _FILENAME_RE.search(manifest, m.end(), block_end)
        if fn:
            docs.append((m.group(1).upper(), fn.group(1)))

    if not docs:
        return None

    def _url(filename: str) -> str:
        return f"{filing.filing_dir_url}/{filename}"

    for doc_type, filename in docs:
        if doc_type == "EX-99.1" and filename.lower().endswith((".htm", ".html", ".txt")):
            return doc_type, _url(filename)
    for doc_type, filename in docs:
        if doc_type.startswith("EX-99") and filename.lower().endswith((".htm", ".html", ".txt")):
            return doc_type, _url(filename)
    # No EX-99 — the narrative may be inline in the cover 8-K. Use the primary
    # document (first .htm), tagged with its real type so the row records the
    # fallback.
    for doc_type, filename in docs:
        if filename.lower().endswith((".htm", ".html")):
            return doc_type, _url(filename)
    return None


def fetch_clean_text(url: str, *, client: httpx.Client) -> str:
    """Fetch an EDGAR document and return its cleaned narrative text."""
    _sleep_for_rate_limit()
    response = client.get(url, headers=edgar_headers())
    response.raise_for_status()
    return clean_html(response.text)


# Leading lines the SEC iXBRL document wrapper injects before the real content.
# Filers vary: NVIDIA emits the type token ``EX-99.1``; Apple additionally emits
# the spelled-out ``Exhibit 99.1``. Both, plus the source filename, a bare
# sequence number, and the literal "Document", are dropped so the stored body
# and its derived title start at the actual press-release headline.
_WRAPPER_LINE_RE = re.compile(
    r"^(ex-?\d+(\.\d+)?|exhibit\s+\d+(\.\d+)?|document|\d+|[\w.\-]+\.html?)$",
    re.IGNORECASE,
)


def clean_html(html: str) -> str:
    """Strip an EDGAR HTML document to normalised narrative text.

    Uses BeautifulSoup's text extraction with newline separators (SEC releases
    style headings via <font>/<span>, not <h*>, so there's no semantic structure
    to preserve), collapses intra-line whitespace, drops empty lines, and trims
    the iXBRL document-wrapper boilerplate from the top.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    raw = soup.get_text(separator="\n")

    lines: list[str] = []
    for line in raw.splitlines():
        collapsed = re.sub(r"\s+", " ", line).strip()
        if collapsed:
            lines.append(collapsed)

    # Drop the leading wrapper tokens (bounded scan — only the first few lines).
    start = 0
    for i, line in enumerate(lines[:8]):
        if _WRAPPER_LINE_RE.match(line):
            start = i + 1
        else:
            break
    return "\n".join(lines[start:])


@dataclass(frozen=True)
class Chunk:
    """One section-tagged chunk of a release body, ready to embed."""

    index: int
    section: str
    text: str


def _is_heading(line: str, next_line: str | None) -> bool:
    """Heuristic: is ``line`` a section heading?

    SEC releases carry no heading tags, so headings are inferred from text
    shape: short, alphabetic, not a sentence, not a numeric table row, and
    followed by prose. The "followed by prose" gate rejects table labels
    (``Revenue`` above ``$57,006``) which are otherwise heading-shaped.

    Real release section headings ("Highlights", "Non-GAAP Measures", "CFO
    Commentary", "Conference Call and Webcast Information") never contain a comma
    or an "@", whereas the two recurring false positives — the dateline
    ("SANTA CLARA, Calif.--May 28, 2025") and contact emails
    ("press@nvidia.com") — always do, so those characters are a clean exclusion.
    """
    if len(line) > _MAX_HEADING_CHARS or line.startswith(("•", "-", "*")):
        return False
    if line.endswith((".", "?", "!", ":", ";", ",")):
        return False
    if "," in line or "@" in line:
        return False
    if not re.search(r"[A-Za-z]{3}", line):
        return False
    # Numeric/currency table rows are not headings.
    numeric = sum(c.isdigit() or c in "$%,.()" for c in line)
    if numeric / max(len(line), 1) > 0.3:
        return False
    if not next_line:
        return False
    # The line after a heading should read like prose, not another short label.
    return len(next_line) > _MAX_HEADING_CHARS or next_line.endswith((".", "?", "!"))


def _split_text(text: str, *, max_chars: int, overlap: int) -> list[str]:
    """Split ``text`` into <= max_chars windows on word boundaries with overlap.

    A single token longer than ``max_chars`` (rare in cleaned prose) is emitted
    as its own over-budget chunk; the embed model truncates it server-side.
    """
    words = text.split()
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for word in words:
        add = len(word) + (1 if current else 0)
        if length + add > max_chars and current:
            chunks.append(" ".join(current))
            # Re-seed the next window with a word-level tail for overlap.
            tail: list[str] = []
            tail_len = 0
            for w in reversed(current):
                if tail_len + len(w) + 1 > overlap:
                    break
                tail.insert(0, w)
                tail_len += len(w) + 1
            current = tail
            length = tail_len
        current.append(word)
        length += add
    if current:
        chunks.append(" ".join(current))
    return chunks


def chunk_release(
    body: str,
    *,
    max_chars: int = _CHUNK_MAX_CHARS,
    overlap: int = _CHUNK_OVERLAP_CHARS,
) -> list[Chunk]:
    """Split a cleaned release body into section-aware chunks.

    Walks the text line by line, tracking the current section via the heading
    heuristic, accumulating prose into per-section buffers, then windows each
    section to the embed-model context budget. Chunk indices are global and
    contiguous so ``point_id(ticker, doc_id, chunk_index)`` stays stable across
    re-runs (idempotent upsert).
    """
    lines = [line for line in body.splitlines() if line.strip()]
    sections: list[tuple[str, str]] = []
    current_section = "Summary"
    buffer: list[str] = []
    for i, line in enumerate(lines):
        next_line = lines[i + 1] if i + 1 < len(lines) else None
        if _is_heading(line, next_line):
            if buffer:
                sections.append((current_section, " ".join(buffer)))
                buffer = []
            current_section = line
        else:
            buffer.append(line)
    if buffer:
        sections.append((current_section, " ".join(buffer)))

    chunks: list[Chunk] = []
    index = 0
    for section, section_text in sections:
        for piece in _split_text(section_text, max_chars=max_chars, overlap=overlap):
            chunks.append(Chunk(index=index, section=section[:_MAX_HEADING_CHARS], text=piece))
            index += 1
    return chunks
