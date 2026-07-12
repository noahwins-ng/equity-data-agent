"""RAG corpus snapshot export -- producer side of the Track-3 (AWS) seam (QNT-265).

Track 3 (the AWS re-platform, separate repo "Equity RAG on AWS") consumes this
monorepo's corpus + relevance labels as a **build-time data handoff, not a
runtime or code coupling**. This script freezes a versioned, checksummed
snapshot bundle the AWS repo re-embeds (Titan V2) and re-indexes (S3 Vectors).
It is the producer side of ``equity-rag-aws docs/PRD.md section 5``; that section
is the consumer-side contract this implements against.

Two corpora, TWO native granularities -- preserved, not normalized (the
granularity asymmetry is the two-arm eval design, not a bug):

* **earnings** (``equity_earnings``) is CHUNK-level -- one row per chunk, carrying
  ``chunk_index`` + ``section``. point_id = ``blake2b(f"{ticker}:{doc_id}:{chunk_index}")``.
* **news** (``equity_news``) is ARTICLE-level -- one row per (ticker, article), no
  ``chunk_index``/``section``; ``text`` is the embedded headline + body.
  point_id = ``blake2b(f"{ticker}:{url_id}")``.

Both derivations are the **shared cross-repo id contract**: identical to the
in-repo Qdrant point-id schemes (``assets/earnings_embeddings.point_id`` /
``assets/news_embeddings.point_id``), and identical to the ids the retrieval-eval
qrels key on. The snapshot is sourced directly from the Qdrant payloads so the
exact embedded text and its point_id travel together -- a doc-level export would
force the consumer to reproduce our chunker to rejoin labels to text.

Output layout (``--out <dir>``; documented cross-repo contract, PRD section 5)::

    <out>/
      corpus/news.jsonl          one row per equity_news point (article-level)
      corpus/earnings.jsonl      one row per equity_earnings point (chunk-level)
      labels/retrieval.yaml      the topics, copied verbatim
      labels/retrieval_qrels.trec  the TREC qrels, copied verbatim
      manifest.json              per-corpus counts, date window, git SHA, checksums

Row fields (per PRD section 5): ``point_id``, ``doc_id``, ``corpus``, ``ticker``,
``date``, ``text``, ``source_url``, plus ``chunk_index`` + ``section`` for
earnings only. Every row and every label is corpus-tagged (``news`` | ``earnings``)
so the cloud eval can score per-corpus.

The bundle is NEVER committed to git in either repo -- news rows carry
vendor-sourced (Finnhub) article bodies and the consumer repo is public. ``--out``
is a plain CLI argument (default ``rag-snapshot/``, gitignored here); the canonical
staging location is the consumer repo's gitignored ``data/`` folder, and the durable
home is S3 (QNT-267). AC3/AC4 receipts are pasted into the PR from this script's
stdout, so no bundle files need to live in this repo's tree.

Usage::

    uv run python -m agent.evals.snapshot_export --out ../equity-rag-aws/data
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shared.config import settings

from agent.evals.retrieval_eval import (
    COLLECTIONS,
    RETRIEVAL_GOLDENS_PATH,
    RETRIEVAL_QRELS_PATH,
    _qdrant_client,
    load_qrels_trec,
)

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

logger = logging.getLogger(__name__)

# Default repo-local output dir. Gitignored (see .gitignore) so an in-repo export
# of vendor-sourced news bodies can never be committed. Prefer an explicit --out
# pointing at the consumer repo's data/ folder for real staging.
DEFAULT_OUT = Path("rag-snapshot")

# Eval framing baked into the manifest (PRD section 5): the cloud eval is a
# designed two-arm comparison -- news is ranking-hard (rerank pays), earnings is
# dense-saturated (rerank barely moves it). Without this the regime finding is
# invisible downstream.
CORPUS_FRAMING: dict[str, str] = {
    "news": "treatment -- ranking-hard; hybrid + rerank lifts here",
    "earnings": "control -- dense-saturated; rerank barely moves it",
}


def _iso_date(ts: Any) -> str:
    """Unix-seconds payload timestamp -> ``YYYY-MM-DD`` (UTC)."""
    return datetime.fromtimestamp(int(ts), tz=UTC).strftime("%Y-%m-%d")


def news_row(point_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """One article-level snapshot row from an ``equity_news`` Qdrant point.

    news is article-level (one point per article, no chunking), so ``doc_id`` is
    the article URL -- the document *is* the point, and the URL is the only
    article-level identity in the payload. ``text`` reconstructs the EMBEDDED text
    exactly as ``news_embeddings`` built it (``headline\\n\\nbody``, or the headline
    alone when the body is empty) so the exact embedded text travels with its id.
    """
    headline = str(payload.get("headline") or "")
    body = str(payload.get("body") or "").strip()
    text = f"{headline}\n\n{body}" if body else headline
    url = str(payload.get("url") or "")
    return {
        "point_id": str(point_id),
        "doc_id": url,
        "corpus": "news",
        "ticker": str(payload.get("ticker") or ""),
        "date": _iso_date(payload["published_at"]),
        "text": text,
        "source_url": url,
    }


def earnings_row(point_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """One chunk-level snapshot row from an ``equity_earnings`` Qdrant point.

    earnings is chunk-level: ``doc_id`` groups a release's chunks, ``chunk_index``
    is the position within the release, ``section`` tags the 8-K section. ``text``
    is the plain chunk text stored in the payload -- the field PRD section 5
    specifies (no ``context`` field on the consumer side).

    Under the default (non-contextual) config this plain chunk IS the embedded
    text. When ``settings.EARNINGS_CONTEXTUAL`` is on, the live vector embeds an
    index-time context blurb prepended to the chunk while the payload ``text``
    stays plain -- so in that mode the exported ``text`` is the chunk, not the
    exact vector input. That drift is not silent: ``export_snapshot`` warns loudly
    when the flag is set (the consumer re-embeds with Titan anyway, so the plain
    chunk is the right, contract-conformant text to carry either way).
    """
    return {
        "point_id": str(point_id),
        "doc_id": int(payload["doc_id"]),
        "corpus": "earnings",
        "ticker": str(payload.get("ticker") or ""),
        "date": _iso_date(payload["filing_date"]),
        "text": str(payload.get("text") or ""),
        "source_url": str(payload.get("url") or ""),
        "chunk_index": int(payload.get("chunk_index", 0)),
        "section": str(payload.get("section") or ""),
    }


_ROW_BUILDERS = {"news": news_row, "earnings": earnings_row}


def scroll_corpus_rows(client: QdrantClient, corpus: str) -> list[dict[str, Any]]:
    """Scroll every point in a corpus collection into native-granularity rows.

    Reads the whole collection (all tickers) with payloads but no vectors -- the
    consumer re-embeds with Titan V2, so vectors are deliberately not exported.
    Rows are returned sorted by (ticker, point_id) for a stable, diffable bundle.
    """
    collection = COLLECTIONS[corpus]
    builder = _ROW_BUILDERS[corpus]
    rows: list[dict[str, Any]] = []
    skipped = 0
    offset: Any = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            limit=256,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        for p in points:
            if isinstance(p.id, int):
                rows.append(builder(p.id, p.payload or {}))
            else:
                skipped += 1
        if offset is None:
            break
    if skipped:
        # A non-int id (legacy/UUID point) can't be a qrels key, but dropping it
        # silently would hide a real corpus anomaly -- surface it. The AC4 round-
        # trip also catches it if such an id happens to be labeled (as an orphan).
        logger.warning("%s: skipped %d non-integer point id(s)", corpus, skipped)
    rows.sort(key=lambda r: (r["ticker"], r["point_id"]))  # string sort: stable, diffable
    return rows


def write_jsonl(rows: list[dict[str, Any]], path: Path) -> None:
    """Write one JSON object per line (UTF-8, non-ASCII preserved)."""
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def sha256_file(path: Path) -> str:
    """SHA-256 hex digest of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_sha() -> str:
    """Current HEAD SHA -- stamps the manifest so the export is reproducible."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class RoundTripReport:
    """AC4: does every qrels point_id join to exactly one snapshot row?"""

    total_qrels_ids: int
    matched: int
    orphans: list[str]  # qrels ids with no snapshot row
    duplicate_ids: list[str]  # point_ids appearing in >1 snapshot row

    @property
    def ok(self) -> bool:
        return not self.orphans and not self.duplicate_ids


def roundtrip_report(qrels_ids: set[str], snapshot_ids: list[str]) -> RoundTripReport:
    """Join committed qrels ids against the snapshot rows (across BOTH corpora).

    The real id invariant (AC4): every qrels point_id joins to exactly one row.
    Two ways to violate it -- an orphan label (qrels id absent from the snapshot)
    or a duplicate snapshot id (a qrels id joining to more than one row). Both are
    reported; ``ok`` is true only when neither occurs.
    """
    counts = Counter(snapshot_ids)
    duplicate_ids = sorted(pid for pid, c in counts.items() if c > 1)
    snap_set = set(snapshot_ids)
    orphans = sorted(qrels_ids - snap_set)
    matched = len(qrels_ids & snap_set)
    return RoundTripReport(len(qrels_ids), matched, orphans, duplicate_ids)


def export_snapshot(
    out_dir: Path, client: QdrantClient | None = None
) -> tuple[dict[str, Any], RoundTripReport]:
    """Produce the frozen snapshot bundle and run the AC4 id round-trip.

    Writes ``corpus/{news,earnings}.jsonl`` (native granularity, from the Qdrant
    payloads), copies the relevance labels verbatim, and writes ``manifest.json``
    (per-corpus counts, date window, git SHA, per-file checksums). Returns the
    manifest and the qrels->snapshot round-trip report.
    """
    client = client or _qdrant_client()

    # earnings `text` is the plain chunk (PRD section 5). When contextual
    # enrichment is on, the live equity_earnings vector embedded a blurb-prefixed
    # chunk, so the plain `text` is no longer the exact vector input -- warn so the
    # operator knows before the bundle ships (see earnings_row docstring).
    if settings.EARNINGS_CONTEXTUAL:
        logger.warning(
            "EARNINGS_CONTEXTUAL is on: exported earnings `text` is the plain chunk, "
            "but the live vectors were context-enriched -- text != exact vector input"
        )

    corpus_dir = out_dir / "corpus"
    labels_dir = out_dir / "labels"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    labels_dir.mkdir(parents=True, exist_ok=True)

    corpus_counts: dict[str, int] = {}
    date_window: dict[str, dict[str, str | None]] = {}
    file_meta: dict[str, dict[str, Any]] = {}
    all_ids: list[str] = []

    for corpus in ("news", "earnings"):
        rows = scroll_corpus_rows(client, corpus)
        path = corpus_dir / f"{corpus}.jsonl"
        write_jsonl(rows, path)
        corpus_counts[corpus] = len(rows)
        dates = [r["date"] for r in rows]
        date_window[corpus] = (
            {"min": min(dates), "max": max(dates)} if dates else {"min": None, "max": None}
        )
        all_ids.extend(r["point_id"] for r in rows)
        file_meta[f"corpus/{corpus}.jsonl"] = {"rows": len(rows), "sha256": sha256_file(path)}
        logger.info("%s: %d rows", corpus, len(rows))

    for src, name in (
        (RETRIEVAL_GOLDENS_PATH, "retrieval.yaml"),
        (RETRIEVAL_QRELS_PATH, "retrieval_qrels.trec"),
    ):
        dst = labels_dir / name
        dst.write_bytes(src.read_bytes())
        file_meta[f"labels/{name}"] = {"sha256": sha256_file(dst)}

    manifest = {
        "snapshot_date": datetime.now(UTC).isoformat(),
        "git_sha": _git_sha(),
        "corpus_counts": corpus_counts,
        "date_window": date_window,
        "eval_framing": CORPUS_FRAMING,
        "files": file_meta,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    qrels = load_qrels_trec(RETRIEVAL_QRELS_PATH)
    qrels_ids = {docid for docs in qrels.values() for docid in docs}
    report = roundtrip_report(qrels_ids, all_ids)
    return manifest, report


def render_manifest(manifest: dict[str, Any]) -> str:
    """AC3 receipt: the manifest as a PR-pasteable scorecard."""
    counts = manifest["corpus_counts"]
    lines = [
        "SNAPSHOT MANIFEST",
        f"  snapshot_date: {manifest['snapshot_date']}",
        f"  git_sha:       {manifest['git_sha']}",
        f"  rows:          news={counts['news']}  earnings={counts['earnings']}",
    ]
    for corpus in ("news", "earnings"):
        win = manifest["date_window"][corpus]
        lines.append(f"  {corpus} window: {win['min']} .. {win['max']}")
    lines.append("  checksums:")
    for rel, meta in manifest["files"].items():
        rows = f"  rows={meta['rows']}" if "rows" in meta else ""
        lines.append(f"    {rel:<28} sha256={meta['sha256'][:16]}...{rows}")
    return "\n".join(lines)


def render_roundtrip(report: RoundTripReport) -> str:
    """AC4 receipt: the qrels->snapshot join result as a PR-pasteable scorecard."""
    lines = [
        "QRELS ID ROUND-TRIP (every qrels point_id -> exactly one snapshot row)",
        f"  qrels ids:  {report.total_qrels_ids}",
        f"  matched:    {report.matched}",
        f"  orphans:    {len(report.orphans)}",
        f"  duplicates: {len(report.duplicate_ids)}",
    ]
    if report.orphans:
        shown = ", ".join(report.orphans[:10])
        more = f" (+{len(report.orphans) - 10} more)" if len(report.orphans) > 10 else ""
        lines.append(f"  ORPHAN ids: {shown}{more}")
    if report.duplicate_ids:
        shown = ", ".join(report.duplicate_ids[:10])
        lines.append(f"  DUPLICATE ids: {shown}")
    lines.append("  RESULT: PASS" if report.ok else "  RESULT: FAIL")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.evals.snapshot_export")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="Output bundle directory (default: rag-snapshot/, gitignored). "
        "Point at the consumer repo's data/ folder to stage the real handoff.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    manifest, report = export_snapshot(args.out)
    print(f"wrote snapshot bundle -> {args.out}\n")
    print(render_manifest(manifest))
    print()
    print(render_roundtrip(report))
    return 0 if report.ok else 1


__all__ = [
    "CORPUS_FRAMING",
    "DEFAULT_OUT",
    "RoundTripReport",
    "earnings_row",
    "export_snapshot",
    "news_row",
    "render_manifest",
    "render_roundtrip",
    "roundtrip_report",
    "scroll_corpus_rows",
    "sha256_file",
    "write_jsonl",
]


if __name__ == "__main__":
    sys.exit(main())
