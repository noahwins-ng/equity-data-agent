"""Offline unit contracts for the RAG corpus snapshot export (QNT-265).

The live export (``agent.evals.snapshot_export.main``) needs Qdrant, so it is NOT
collected here. These lock the pure producer-side invariants a corpus-free run can
still verify: the per-corpus native-granularity row shape + point-id contract
(AC2/AC5), the corpus tag on every row (AC5), the manifest layout (AC1/AC3), and
the qrels id round-trip logic (AC4) -- exercised against a fake Qdrant client so
the whole bundle path runs with no network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from agent.evals.snapshot_export import (
    earnings_row,
    export_snapshot,
    news_row,
    render_manifest,
    render_roundtrip,
    roundtrip_report,
    scroll_corpus_rows,
)

# blake2b(f"{ticker}:{url_id}") / (f"{ticker}:{doc_id}:{chunk_index}") produce
# UInt64 ids; the snapshot carries them as decimal strings (the qrels key form).
NEWS_PID = 12580411958722629317
EARN_PID = 11301284694445750202


@dataclass
class _FakePoint:
    id: int | str  # Qdrant ids are int here; str exercises the non-int skip path
    payload: dict[str, Any]


class _FakeQdrant:
    """Minimal scroll-only stand-in: one page per collection, then stop."""

    def __init__(self, by_collection: dict[str, list[_FakePoint]]) -> None:
        self._by_collection = by_collection

    def scroll(
        self,
        collection_name: str,
        limit: int,
        with_payload: bool,
        with_vectors: bool,
        offset: Any,
    ) -> tuple[list[_FakePoint], Any]:
        # offset is None on the first (only) call; returning None ends pagination.
        return self._by_collection.get(collection_name, []), None


def _news_payload() -> dict[str, Any]:
    return {
        "ticker": "AAPL",
        "published_at": 1_717_200_000,  # 2024-06-01
        "url": "https://example.com/a",
        "headline": "Apple ships a thing",
        "body": "Body text.",
        "source": "finnhub",
    }


def _earnings_payload() -> dict[str, Any]:
    return {
        "ticker": "NVDA",
        "doc_id": 42,
        "filing_date": 1_717_200_000,
        "section": "MD&A",
        "chunk_index": 3,
        "url": "https://sec.gov/x",
        "title": "8-K",
        "text": "Balance sheet chunk.",
    }


def test_news_row_is_article_level() -> None:
    row = news_row(NEWS_PID, _news_payload())
    assert row["point_id"] == str(NEWS_PID)  # decimal string, the qrels key form
    assert row["corpus"] == "news"
    assert row["ticker"] == "AAPL"
    assert row["date"] == "2024-06-01"
    assert row["source_url"] == "https://example.com/a"
    # embedded text reconstructed exactly as news_embeddings built it
    assert row["text"] == "Apple ships a thing\n\nBody text."
    # article-level: no chunk_index / section
    assert "chunk_index" not in row
    assert "section" not in row


def test_news_row_headline_only_when_body_empty() -> None:
    payload = _news_payload() | {"body": ""}
    assert news_row(NEWS_PID, payload)["text"] == "Apple ships a thing"


def test_earnings_row_is_chunk_level() -> None:
    row = earnings_row(EARN_PID, _earnings_payload())
    assert row["point_id"] == str(EARN_PID)
    assert row["corpus"] == "earnings"
    assert row["ticker"] == "NVDA"
    assert row["doc_id"] == 42
    assert row["chunk_index"] == 3  # chunk-level fields present
    assert row["section"] == "MD&A"
    assert row["text"] == "Balance sheet chunk."
    assert row["source_url"] == "https://sec.gov/x"


def test_earnings_row_exports_plain_chunk_even_with_context() -> None:
    # Contextual-enrichment payload shape (EARNINGS_CONTEXTUAL on): the vector
    # embedded a blurb-prefixed chunk, but the snapshot deliberately exports the
    # plain chunk (PRD section 5 has no context field) -- and never emits a
    # `context` key. export_snapshot warns loudly in this mode.
    payload = _earnings_payload() | {"context": "This chunk is from NVDA's Q2 8-K."}
    row = earnings_row(EARN_PID, payload)
    assert row["text"] == "Balance sheet chunk."
    assert "context" not in row


def test_scroll_empty_corpus_yields_no_rows() -> None:
    client = _FakeQdrant({"equity_news": []})
    assert scroll_corpus_rows(client, "news") == []  # type: ignore[arg-type]


def test_scroll_skips_non_int_point_ids() -> None:
    # A UUID/legacy point id can't be a qrels key -- it's dropped (and warned).
    client = _FakeQdrant(
        {
            "equity_news": [
                _FakePoint("a-uuid-id", _news_payload()),
                _FakePoint(NEWS_PID, _news_payload()),
            ]
        }  # type: ignore[arg-type]
    )
    rows = scroll_corpus_rows(client, "news")  # type: ignore[arg-type]
    assert [r["point_id"] for r in rows] == [str(NEWS_PID)]


def test_roundtrip_pass() -> None:
    report = roundtrip_report({"1", "2"}, ["1", "2", "3"])
    assert report.ok
    assert report.matched == 2
    assert report.orphans == []
    assert report.duplicate_ids == []


def test_roundtrip_orphan_label_fails() -> None:
    report = roundtrip_report({"1", "99"}, ["1", "2"])
    assert not report.ok
    assert report.orphans == ["99"]


def test_roundtrip_duplicate_id_fails() -> None:
    report = roundtrip_report({"1"}, ["1", "1", "2"])
    assert not report.ok
    assert report.duplicate_ids == ["1"]


def test_export_snapshot_end_to_end(tmp_path, monkeypatch) -> None:
    client = _FakeQdrant(
        {
            "equity_news": [_FakePoint(NEWS_PID, _news_payload())],
            "equity_earnings": [_FakePoint(EARN_PID, _earnings_payload())],
        }
    )
    # qrels reference exactly the two exported points -> clean round-trip.
    import agent.evals.snapshot_export as se

    monkeypatch.setattr(
        se,
        "load_qrels_trec",
        lambda _path: {"q1": {str(NEWS_PID): 1}, "q2": {str(EARN_PID): 1}},
    )

    manifest, report = export_snapshot(tmp_path, client=cast(Any, client))

    # layout: corpus/*.jsonl, labels/*, manifest.json all present
    assert (tmp_path / "corpus" / "news.jsonl").exists()
    assert (tmp_path / "corpus" / "earnings.jsonl").exists()
    assert (tmp_path / "labels" / "retrieval.yaml").exists()
    assert (tmp_path / "labels" / "retrieval_qrels.trec").exists()
    assert (tmp_path / "manifest.json").exists()

    # manifest: per-corpus counts + checksums + git sha
    assert manifest["corpus_counts"] == {"news": 1, "earnings": 1}
    assert len(manifest["git_sha"]) == 40
    assert manifest["files"]["corpus/news.jsonl"]["sha256"]
    assert set(manifest["eval_framing"]) == {"news", "earnings"}

    # every row is corpus-tagged (AC5)
    news_lines = (tmp_path / "corpus" / "news.jsonl").read_text().splitlines()
    assert json.loads(news_lines[0])["corpus"] == "news"

    # AC4 round-trip clean, and both receipts render without error
    assert report.ok
    assert "RESULT: PASS" in render_roundtrip(report)
    assert "git_sha" in render_manifest(manifest)
