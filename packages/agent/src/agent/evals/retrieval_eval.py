"""Component-level RAG retrieval eval (QNT-261).

The news-search eval (``news_search_eval``) scores *structural* relevance (does a
returned hit contain an expected term) and reports a single rolling hit-rate. It
cannot tell a retrieval miss from a synthesis miss, and it has no notion of
*ranking* quality. This harness adds the industry-standard **stage-1 retrieval
eval**: a curated relevance set (query -> relevant doc ids) scored with classic
IR metrics (recall@k / MRR / nDCG) via ``ir_measures``. It is DETERMINISTIC and
LLM-free -- separate from the generation judge (``judge`` / ``dialogue_judge``)
by design (see docs/v2-overall-enhancement.md "RAG eval framework").

Standard IR layout -- three files joined by query id (``goldens/``):

* ``retrieval.yaml``      -- topics: query text + ``anchor_terms`` (the relevance
  criterion). Hand-authored, comment-rich; the human artifact.
* ``retrieval_qrels.trec`` -- labels: ``qid 0 docid 1`` per relevant doc. Captured
  from the live corpus by ``--label``; the committed ground truth.
* ``retrieval_run.trec``  -- the frozen dense-retrieval ranking, captured by
  ``--baseline``; what the CI gate scores offline.

doc_id scheme (load-bearing). The doc id is the **Qdrant point id** (UInt64) --
what a vector search returns and what aligns across both corpora and the later
S3-Vectors substrate (QNT-260/270):

* ``equity_news``     -- point id = ``blake2b(f"{ticker}:{url_id}")``
* ``equity_earnings`` -- point id = ``blake2b(f"{ticker}:{doc_id}:{chunk_index}")``

Relevance labels come from an **independent lexical criterion** (a doc is
relevant to a query if its payload text contains one of the query's
``anchor_terms``), scanned over the *full* ticker-scoped corpus -- not from the
dense ranking under test. That independence is what makes recall/MRR/nDCG
meaningful: we measure whether dense retrieval surfaces the lexically-relevant
docs, scored against a ground truth it did not produce. Labels + run are frozen
TREC files so the CI gate is reproducible even as the live corpus rolls;
``anchor_terms`` document how the labels were derived so they regenerate.

Three modes:

* ``--label`` (live, needs Qdrant) -- scan the corpus, write ``retrieval_qrels.trec``.
* ``--baseline`` (live, needs Qdrant) -- run current dense retrieval, write the
  frozen ``retrieval_run.trec``, append the aggregate metrics to ``history.csv``,
  and print the scorecard. Records the AC4 baseline.
* (default) ``--score`` (offline, no network) -- load the frozen qrels + run,
  compute metrics via ir_measures, and exit non-zero if any metric is below its
  gate floor. This is the per-PR CI gate
  (``tests/agent/evals/test_retrieval_eval.py`` runs it under ``-m eval``).

Examples::

    uv run python -m agent.evals.retrieval_eval --label
    uv run python -m agent.evals.retrieval_eval --baseline
    uv run python -m agent.evals.retrieval_eval            # offline score + gate
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import ir_measures
import yaml
from ir_measures import RR, R, nDCG
from shared.config import settings
from shared.retrieval import (
    bm25_ranking,
    cohere_rerank,
    contextualize_chunk,
    contextualized_text,
    reciprocal_rank_fusion,
)
from shared.tickers import TICKERS

from agent.evals.spine import append_suite_history, suite_history_path

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

logger = logging.getLogger(__name__)

_GOLDENS = Path(__file__).parent / "goldens"
RETRIEVAL_GOLDENS_PATH = _GOLDENS / "retrieval.yaml"
RETRIEVAL_QRELS_PATH = _GOLDENS / "retrieval_qrels.trec"
RETRIEVAL_RUN_PATH = _GOLDENS / "retrieval_run.trec"
# QNT-262: the frozen SERVED-path ranking -- hybrid (dense + BM25 RRF) + Cohere
# rerank -- written by ``--hybrid --rerank``. This is the GATED run after the
# QNT-262 follow-up: prod serves this path, so ``score_offline`` scores it.
# ``retrieval_run.trec`` (dense) is kept as the committed A/B reference (the
# "before"); only the rerank run is frozen here (``run_hybrid`` writes it only
# when rerank is active, so a fused-only ``--hybrid`` can't clobber the gated
# run with weaker numbers).
RETRIEVAL_RUN_HYBRID_PATH = _GOLDENS / "retrieval_run_hybrid.trec"

# Same embed model + collections the production search path uses, so the
# baseline measures the retrieval the system actually serves (api/routers/search
# + api/qdrant; assets/{news,earnings}_embeddings). MiniLM-L6 -> 384-dim cosine.
EMBED_MODEL = "sentence-transformers/all-minilm-l6-v2"
COLLECTIONS = {"news": "equity_news", "earnings": "equity_earnings"}

# QNT-273: the contextual A/B index. Same chunks + point ids + plain payload as
# ``equity_earnings``, but each vector embeds ``context_blurb \n\n chunk`` — so
# dense retrieval over this collection vs the plain one is the contextual lift,
# scored against the one shared qrels file (ids match across both).
CONTEXTUAL_COLLECTION = "equity_earnings_ctx"

# Min labeled queries for the set to reliably catch a >5% regression (design-doc
# calibration: a 50-q set is the floor). Upper bound mirrors the 200 target.
MIN_QUERIES = 50
MAX_QUERIES = 200

# Top-k pulled from the dense index for the run. Recall@20 needs the full 20, and
# the 4-8 band is the faithfulness-relevant cut-off -- 20 covers both.
RUN_DEPTH = 20

# Per-PR gate floors. Set ~0.08 below the recorded baseline (see history.csv) so
# the gate catches a real regression without failing on day one; re-derive
# against a fresh baseline when retrieval changes. NB these are regression
# tripwires anchored to the MEASURED baseline, NOT the design-doc aspirational
# targets (recall@20 >= 0.8).
#
# QNT-262 follow-up: PROMOTED to the SERVED path -- prod now serves hybrid (dense
# + BM25 RRF) + Cohere Rerank 3.5, so the gate scores that frozen run
# (retrieval_run_hybrid.trec), not the dense-only one.
#
# QNT-265 re-derivation (2026-07-12): the news corpus was fully reingested since
# QNT-261 labeled it (every news point_id reassigned), so the frozen qrels + runs
# were relabeled + rebaselined against the current corpus (the snapshot-export AC4
# round-trip forced this). Fresh served baseline over the 51 refreshed queries:
# R@5 0.527, R@20 0.767, RR 0.894, nDCG@10 0.799 (vs dense 0.305/0.591/0.633/0.524
# the dense reference records -- the +0.22/+0.18/+0.26/+0.27 rerank lift is the
# news="treatment" arm). Floors re-derived ~0.08 below the fresh served numbers.
GATE_FLOORS: dict[str, float] = {
    "R@5": 0.45,
    "R@20": 0.68,
    "RR": 0.81,
    "nDCG@10": 0.72,
}

# ir_measures metric objects, evaluated together in one pass.
METRICS = [R @ 5, R @ 20, RR, nDCG @ 10]

# QNT-293 follow-up: retrieval writes one aggregate row per run to its own
# per-suite history file. Columns are exactly the retrieval metrics; the envelope
# (run_id/git_sha/prompt_version/suite) is stamped by append_suite_history.
RETRIEVAL_HISTORY_PATH = suite_history_path("retrieval")
RETRIEVAL_FIELDS = (
    "eval_type",
    "recall_at_5",
    "recall_at_20",
    "mrr",
    "ndcg_at_10",
    "retrieval_n",
)


@dataclass(frozen=True)
class RetrievalQuery:
    """One topic from goldens/retrieval.yaml."""

    id: str
    corpus: str  # "news" | "earnings"
    ticker: str
    query: str
    anchor_terms: tuple[str, ...]


def load_retrieval_queries(path: Path = RETRIEVAL_GOLDENS_PATH) -> list[RetrievalQuery]:
    """Parse + validate the topics file into typed queries.

    Validates here (unique ids, ticker in TICKERS, known corpus, anchor_terms
    present, query-count floor/cap) so every consumer reads from one authority.
    """
    raw = yaml.safe_load(path.read_text())
    rows = raw.get("queries") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"{path}: missing top-level `queries` list")

    records: list[RetrievalQuery] = []
    seen: set[str] = set()
    for entry in rows:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each query must be a mapping, got {type(entry)}")
        try:
            qid = str(entry["id"])
            corpus = str(entry["corpus"])
            ticker = str(entry["ticker"])
            query = str(entry["query"])
        except KeyError as exc:
            raise ValueError(f"{path}: query missing field {exc}") from exc
        if qid in seen:
            raise ValueError(f"{path}: duplicate query id {qid!r}")
        if corpus not in COLLECTIONS:
            raise ValueError(f"{path}: query {qid!r} has unknown corpus {corpus!r}")
        if ticker not in TICKERS:
            raise ValueError(f"{path}: query {qid!r} references unknown ticker {ticker!r}")
        anchor_terms = tuple(str(t) for t in entry.get("anchor_terms", []))
        if not anchor_terms:
            raise ValueError(f"{path}: query {qid!r} must list anchor_terms")
        seen.add(qid)
        records.append(
            RetrievalQuery(
                id=qid, corpus=corpus, ticker=ticker, query=query, anchor_terms=anchor_terms
            )
        )

    if len(records) < MIN_QUERIES:
        raise ValueError(f"{path}: {len(records)} queries, need at least {MIN_QUERIES}")
    if len(records) > MAX_QUERIES:
        raise ValueError(f"{path}: {len(records)} queries, exceeds cap of {MAX_QUERIES}")
    return records


# --- frozen TREC qrels + run I/O (offline; the CI gate path) -------------------


def write_qrels_trec(qrels: dict[str, dict[str, int]], path: Path = RETRIEVAL_QRELS_PATH) -> None:
    """Persist labels as a TREC qrels file: ``qid 0 docid relevance`` per line."""
    lines: list[str] = []
    for qid in sorted(qrels):
        for docid in sorted(qrels[qid]):
            lines.append(f"{qid} 0 {docid} {qrels[qid][docid]}")
    path.write_text("\n".join(lines) + "\n")


def load_qrels_trec(path: Path = RETRIEVAL_QRELS_PATH) -> dict[str, dict[str, int]]:
    """Load a TREC qrels file into the ir_measures qrels dict."""
    qrels: dict[str, dict[str, int]] = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        qid, _, docid, rel = parts
        qrels.setdefault(qid, {})[docid] = int(rel)
    return qrels


def write_run_trec(run: dict[str, dict[str, float]], path: Path = RETRIEVAL_RUN_PATH) -> None:
    """Persist the dense ranking as a TREC run file (the frozen baseline).

    ``qid Q0 docid rank score TAG`` per line -- the standard format ir_measures
    parses, committed so the CI gate scores it offline without re-querying
    Qdrant (the corpus rolls; the frozen ranking does not).
    """
    lines: list[str] = []
    for qid in sorted(run):
        ranked = sorted(run[qid].items(), key=lambda kv: kv[1], reverse=True)
        # 1-based rank per TREC convention (ir_measures re-derives order from the
        # score column, but these run files are the producer side of the S3
        # snapshot handoff, so keep them interoperable with external TREC tools).
        for rank, (docid, score) in enumerate(ranked, start=1):
            lines.append(f"{qid} Q0 {docid} {rank} {score:.6f} qnt261")
    path.write_text("\n".join(lines) + "\n")


def load_run_trec(path: Path = RETRIEVAL_RUN_PATH) -> dict[str, dict[str, float]]:
    """Load a TREC run file into the ir_measures run dict."""
    run: dict[str, dict[str, float]] = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) != 6:
            continue
        qid, _, docid, _, score, _ = parts
        run.setdefault(qid, {})[docid] = float(score)
    return run


def compute_metrics(
    qrels: dict[str, dict[str, int]], run: dict[str, dict[str, float]]
) -> dict[str, float]:
    """Aggregate recall@5/@20, MRR (RR), and nDCG@10 via ir_measures."""
    agg = ir_measures.calc_aggregate(METRICS, qrels, run)
    return {str(metric): float(value) for metric, value in agg.items()}


def gate_failures(metrics: dict[str, float]) -> list[str]:
    """Metrics that fell below their gate floor -- empty == pass."""
    failures: list[str] = []
    for name, floor in GATE_FLOORS.items():
        value = metrics.get(name)
        if value is None or value < floor:
            failures.append(f"{name}={value if value is None else round(value, 4)} < floor {floor}")
    return failures


# --- live helpers (need Qdrant Cloud; not imported on the offline scoring path) --


def _qdrant_client() -> QdrantClient:
    """A direct Qdrant Cloud client for the eval harness.

    The agent reaches retrieval through the FastAPI search endpoint, but that
    endpoint returns display fields (no point id) and applies a relevance-gap
    tail-trim (QNT-226) that would truncate the ranking recall@20 needs. This is
    eval tooling measuring the dense substrate directly, so it talks to Qdrant
    with the SAME model + collections the API uses. ``cloud_inference=True`` lets
    a ``Document(text, model)`` query embed server-side at query time.
    """
    from qdrant_client import QdrantClient

    return QdrantClient(
        url=settings.QDRANT_URL,
        api_key=settings.QDRANT_API_KEY,
        timeout=15,
        cloud_inference=True,
    )


def _searchable_text(corpus: str, payload: dict[str, Any]) -> str:
    """The payload text the lexical relevance criterion scans, per corpus."""
    if corpus == "news":
        return f"{payload.get('headline') or ''} {payload.get('body') or ''}"
    return (
        f"{payload.get('title') or ''} {payload.get('section') or ''} {payload.get('text') or ''}"
    )


def scan_relevant_ids(client: QdrantClient, query: RetrievalQuery) -> list[int]:
    """Lexically label the full ticker-scoped corpus for one query.

    Scrolls every point for the query's ticker in its corpus and marks a point
    relevant if its searchable text contains (case-insensitive) any of the
    query's ``anchor_terms``. Independent of the dense ranking under test -- this
    is the ground truth, not a re-ranking. Returns sorted ids for a stable diff.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    collection = COLLECTIONS[query.corpus]
    flt = Filter(must=[FieldCondition(key="ticker", match=MatchValue(value=query.ticker))])
    terms = [t.lower() for t in query.anchor_terms]

    relevant: list[int] = []
    offset: Any = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            scroll_filter=flt,
            limit=256,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        for p in points:
            haystack = _searchable_text(query.corpus, p.payload or {}).lower()
            if any(term in haystack for term in terms) and isinstance(p.id, int):
                relevant.append(p.id)
        if offset is None:
            break
    return sorted(relevant)


def dense_run_ids(
    client: QdrantClient,
    query: RetrievalQuery,
    *,
    depth: int = RUN_DEPTH,
    collection: str | None = None,
) -> list[tuple[int, float]]:
    """Run current dense retrieval -- the ranking under test.

    Returns up to ``depth`` ``(point_id, score)`` pairs, descending by score,
    reproducing the production search path (same model, collection, ticker
    filter) minus the API's relevance-gap trim so the full ranking is scored.

    ``collection`` overrides the corpus default so the QNT-273 A/B can score the
    SAME query against the contextual index (``equity_earnings_ctx``); point ids
    are identical across the two collections, so one qrels file scores both.
    """
    from qdrant_client.models import Document, FieldCondition, Filter, MatchValue

    collection = collection or COLLECTIONS[query.corpus]
    flt = Filter(must=[FieldCondition(key="ticker", match=MatchValue(value=query.ticker))])
    response = client.query_points(
        collection_name=collection,
        query=Document(text=query.query, model=EMBED_MODEL),
        query_filter=flt,
        limit=depth,
        with_payload=False,
    )
    return [(p.id, float(p.score)) for p in response.points if isinstance(p.id, int)]


def corpus_texts(client: QdrantClient, query: RetrievalQuery) -> dict[str, str]:
    """Scroll the ticker-scoped corpus into ``{str(point_id): searchable_text}``.

    The BM25 half of hybrid scores the *full* ticker slice (same scan
    ``scan_relevant_ids`` walks), so a lexical-only doc the dense ranker missed
    can still enter the fusion. Uses ``_searchable_text`` so the BM25 haystack
    matches the relevance criterion's, per corpus.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    collection = COLLECTIONS[query.corpus]
    flt = Filter(must=[FieldCondition(key="ticker", match=MatchValue(value=query.ticker))])
    texts: dict[str, str] = {}
    offset: Any = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            scroll_filter=flt,
            limit=256,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        for p in points:
            if isinstance(p.id, int):
                texts[str(p.id)] = _searchable_text(query.corpus, p.payload or {})
        if offset is None:
            break
    return texts


def hybrid_run_ids(
    client: QdrantClient,
    query: RetrievalQuery,
    *,
    depth: int = RUN_DEPTH,
    rerank: bool = False,
) -> list[tuple[int, float]]:
    """Hybrid retrieval under test: dense + BM25 RRF, optionally Cohere-reranked.

    Fuses the dense ranking (``dense_run_ids``) with a BM25 ranking over the
    ticker corpus (``corpus_texts``) via RRF; when ``rerank`` is set and a Cohere
    key is configured, the fused candidate set is reordered by the cross-encoder.
    Returns up to ``depth`` ``(point_id, score)`` pairs descending — the same
    shape ``dense_run_ids`` returns, so the two are A/B-scored identically.
    """
    dense = dense_run_ids(client, query, depth=depth)
    dense_ids = [str(pid) for pid, _ in dense]
    corpus = corpus_texts(client, query)
    bm25_ids = bm25_ranking(corpus, query.query, limit=depth)
    fused = reciprocal_rank_fusion([dense_ids, bm25_ids])

    candidate_ids = [doc_id for doc_id, _ in fused[: settings.RERANK_CANDIDATES]]
    ordered: list[tuple[str, float]] = fused[:depth]
    if rerank and settings.COHERE_API_KEY:
        docs = {doc_id: corpus.get(doc_id, "") for doc_id in candidate_ids}
        reranked = cohere_rerank(
            query.query,
            docs,
            api_key=settings.COHERE_API_KEY,
            model=settings.COHERE_RERANK_MODEL,
            top_n=len(candidate_ids),
        )
        if reranked is not None:
            ordered = reranked[:depth]

    return [(int(doc_id), score) for doc_id, score in ordered]


# --- history.csv tracking ------------------------------------------------------


def append_retrieval_history(
    metrics: dict[str, float],
    *,
    n_queries: int,
    run_id: str | None = None,
    history_path: Path = RETRIEVAL_HISTORY_PATH,
) -> str:
    """Append one aggregate retrieval row to ``retrieval_history.csv``.

    QNT-293 follow-up: one row per run in the retrieval suite's own file
    (columns = :data:`RETRIEVAL_FIELDS`), stamped by
    :func:`spine.append_suite_history` with the shared envelope so a retrieval
    regression is bisectable against the same git_sha + prompt_version as every
    other suite. ``history_path`` overrides the default file (CI / tests).
    """
    import uuid

    rid = run_id or uuid.uuid4().hex[:8]
    row = {
        "eval_type": "retrieval",
        "recall_at_5": round(metrics.get("R@5", 0.0), 4),
        "recall_at_20": round(metrics.get("R@20", 0.0), 4),
        "mrr": round(metrics.get("RR", 0.0), 4),
        "ndcg_at_10": round(metrics.get("nDCG@10", 0.0), 4),
        "retrieval_n": n_queries,
    }
    return append_suite_history("retrieval", RETRIEVAL_FIELDS, [row], run_id=rid, path=history_path)


def summarise(metrics: dict[str, float], *, n_queries: int, label: str = "baseline") -> str:
    """Human-readable scorecard + gate verdict for stdout / the README."""
    failures = gate_failures(metrics)
    lines = [
        f"RETRIEVAL EVAL ({label}; {n_queries} labeled queries, deterministic ir_measures)",
        f"  recall@5:  {metrics.get('R@5', 0.0):.4f}  (floor {GATE_FLOORS['R@5']})",
        f"  recall@20: {metrics.get('R@20', 0.0):.4f}  (floor {GATE_FLOORS['R@20']})",
        f"  MRR:       {metrics.get('RR', 0.0):.4f}  (floor {GATE_FLOORS['RR']})",
        f"  nDCG@10:   {metrics.get('nDCG@10', 0.0):.4f}  (floor {GATE_FLOORS['nDCG@10']})",
    ]
    lines.append("  GATE: FAIL -- " + "; ".join(failures) if failures else "  GATE: PASS")
    return "\n".join(lines)


def summarise_comparison(
    dense: dict[str, float], variant: dict[str, float], *, n_queries: int, label: str
) -> str:
    """Before/after scorecard: dense baseline vs a ``label`` variant, with deltas.

    The AC3 artifact — pasted into the PR so the lift (or null result) of the
    variant under test (hybrid+rerank for QNT-262, contextual for QNT-273) is
    quantified against the same live corpus, not an assumption. The variant
    column header tracks ``label`` so the scorecard never mislabels the run.
    """
    lines = [
        f"RETRIEVAL EVAL -- dense vs {label} ({n_queries} labeled queries, ir_measures)",
        f"  {'metric':<10} {'dense':>8} {label[:8]:>8} {'delta':>8}",
    ]
    for name in ("R@5", "R@20", "RR", "nDCG@10"):
        d = dense.get(name, 0.0)
        v = variant.get(name, 0.0)
        lines.append(f"  {name:<10} {d:>8.4f} {v:>8.4f} {v - d:>+8.4f}")
    return "\n".join(lines)


def _check_alignment(queries: list[RetrievalQuery], *names: tuple[str, set[str]]) -> list[str]:
    """Return human-readable mismatches between the topic ids and each id set."""
    topic_ids = {q.id for q in queries}
    problems: list[str] = []
    for label, ids in names:
        missing = topic_ids - ids
        extra = ids - topic_ids
        if missing:
            problems.append(f"{label} missing {len(missing)} queries: {', '.join(sorted(missing))}")
        if extra:
            problems.append(f"{label} has {len(extra)} unknown ids: {', '.join(sorted(extra))}")
    return problems


# --- modes ---------------------------------------------------------------------


def label_corpus() -> int:
    """``--label``: scan the live corpus -> write the frozen qrels file."""
    queries = load_retrieval_queries()
    client = _qdrant_client()
    qrels: dict[str, dict[str, int]] = {}
    empties: list[str] = []
    for q in queries:
        ids = scan_relevant_ids(client, q)
        qrels[q.id] = {str(pid): 1 for pid in ids}
        if not ids:
            empties.append(q.id)
        logger.info("labeled %s: %d relevant", q.id, len(ids))
    write_qrels_trec(qrels)
    print(f"wrote {RETRIEVAL_QRELS_PATH} ({sum(len(v) for v in qrels.values())} judgments)")
    if empties:
        print(
            f"WARNING: {len(empties)} queries matched zero docs (re-anchor before "
            f"committing): {', '.join(empties)}",
            file=sys.stderr,
        )
        return 1
    return 0


def run_baseline() -> int:
    """``--baseline``: live dense run -> frozen run file + history row + scorecard."""
    queries = load_retrieval_queries()
    qrels = load_qrels_trec()
    problems = _check_alignment(queries, ("qrels", set(qrels)))
    if problems:
        print("label the corpus first (--label):\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    client = _qdrant_client()
    run: dict[str, dict[str, float]] = {}
    for q in queries:
        run[q.id] = {str(pid): score for pid, score in dense_run_ids(client, q)}
    write_run_trec(run)
    metrics = compute_metrics(qrels, run)
    rid = append_retrieval_history(metrics, n_queries=len(queries))
    # Dense is the A/B reference, not the gate (the gate scores the served
    # hybrid+rerank run); skip the gate verdict here to avoid implying it.
    print(summarise(metrics, n_queries=len(queries), label="dense reference"))
    print(f"\nfrozen run: {RETRIEVAL_RUN_PATH}\nhistory run_id: {rid}")
    return 0


def run_hybrid(*, rerank: bool) -> int:
    """``--hybrid``: live dense vs hybrid A/B over the labeled set (QNT-262).

    Runs current dense retrieval and hybrid (dense + BM25 RRF [+ Cohere rerank])
    over the SAME live corpus in one pass, scores both against the frozen qrels,
    appends a history row, and prints the before/after scorecard. ``--rerank``
    adds the Cohere layer; it no-ops (logs, falls back to fused) when
    COHERE_API_KEY is unset.

    The frozen served run (``retrieval_run_hybrid.trec``, the GATED artifact) is
    rewritten ONLY when rerank is active -- so a fused-only ``--hybrid`` is a
    print-only diagnostic that can't clobber the gated run with weaker numbers.
    """
    queries = load_retrieval_queries()
    qrels = load_qrels_trec()
    problems = _check_alignment(queries, ("qrels", set(qrels)))
    if problems:
        print("label the corpus first (--label):\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1

    rerank_active = rerank and bool(settings.COHERE_API_KEY)
    if rerank and not rerank_active:
        print(
            "note: --rerank requested but COHERE_API_KEY unset -- measuring fused-only",
            file=sys.stderr,
        )
    label = "hybrid+rerank" if rerank_active else "hybrid"

    # Cohere Rerank 3.5 trial = 10 rpm. One rerank call per query, so throttle to
    # stay under the ceiling -- otherwise 429s degrade calls to the fused order
    # (cohere_rerank returns None) and silently UNDERSTATE the rerank lift. No
    # throttle on the fused-only path (no network per query).
    import time

    throttle_s = 60.0 / 10 if rerank_active else 0.0

    client = _qdrant_client()
    dense_run: dict[str, dict[str, float]] = {}
    hybrid_run: dict[str, dict[str, float]] = {}
    for q in queries:
        dense_run[q.id] = {str(pid): score for pid, score in dense_run_ids(client, q)}
        hybrid_run[q.id] = {
            str(pid): score for pid, score in hybrid_run_ids(client, q, rerank=rerank)
        }
        if throttle_s:
            time.sleep(throttle_s)

    dense_metrics = compute_metrics(qrels, dense_run)
    hybrid_metrics = compute_metrics(qrels, hybrid_run)
    rid = append_retrieval_history(hybrid_metrics, n_queries=len(queries))
    print(summarise_comparison(dense_metrics, hybrid_metrics, n_queries=len(queries), label=label))
    # Only the served path (rerank active) is the gated artifact; freeze it. A
    # fused-only run prints the A/B but leaves the committed gated run intact.
    if rerank_active:
        write_run_trec(hybrid_run, RETRIEVAL_RUN_HYBRID_PATH)
        print(f"\nfrozen served run: {RETRIEVAL_RUN_HYBRID_PATH}")
    print(f"history run_id: {rid}")
    return 0


def build_contextual_index(
    client: QdrantClient, queries: list[RetrievalQuery], *, throttle_s: float = 1.0
) -> int:
    """Build/refresh ``equity_earnings_ctx`` — the contextual A/B index (QNT-273).

    For every earnings ticker in the labeled set, scroll the plain
    ``equity_earnings`` points, reconstruct each release's parent document from
    its chunks (ordered by ``chunk_index``), generate a 1-sentence context blurb
    per chunk (``shared.contextualize_chunk`` -> LiteLLM free model), and upsert
    the enriched embedding under the SAME point id + plain payload. Idempotent on
    a SUCCESSFUL enrichment: a re-run skips points whose ctx payload already
    carries a non-empty ``context`` and retries the rest, so a 429 mid-build (the
    blurb falls back to "") is healed by re-running rather than frozen plain.
    Returns the number of chunks enriched this run.

    The parent doc is reconstructed from the indexed chunks (not re-read from
    ClickHouse) so the harness needs only Qdrant + the LiteLLM proxy — no tunnel.
    """
    import time

    from qdrant_client.models import (
        Distance,
        Document,
        FieldCondition,
        Filter,
        MatchValue,
        PayloadSchemaType,
        PointStruct,
        VectorParams,
    )

    tickers = sorted({q.ticker for q in queries if q.corpus == "earnings"})
    existing_cols = {c.name for c in client.get_collections().collections}
    if CONTEXTUAL_COLLECTION not in existing_cols:
        client.create_collection(
            collection_name=CONTEXTUAL_COLLECTION,
            vectors_config=VectorParams(size=384, distance=Distance.COSINE),
        )
        client.create_payload_index(
            collection_name=CONTEXTUAL_COLLECTION,
            field_name="ticker",
            field_schema=PayloadSchemaType.KEYWORD,
        )

    enriched = 0
    for ticker in tickers:
        flt = Filter(must=[FieldCondition(key="ticker", match=MatchValue(value=ticker))])
        # plain points (id + payload) and the ids already SUCCESSFULLY enriched.
        plain: list[tuple[int, dict[str, Any]]] = []
        offset: Any = None
        while True:
            recs, offset = client.scroll(
                collection_name=COLLECTIONS["earnings"],
                scroll_filter=flt,
                limit=256,
                with_payload=True,
                with_vectors=False,
                offset=offset,
            )
            plain.extend((int(r.id), r.payload or {}) for r in recs if isinstance(r.id, int))
            if offset is None:
                break
        done_ids = _ctx_enriched_ids(client, flt)

        # Reconstruct each release's document from its chunks (chunk_index order).
        docs: dict[int, str] = {}
        by_doc: dict[int, list[tuple[int, str]]] = {}
        for _pid, payload in plain:
            by_doc.setdefault(int(payload.get("doc_id", 0)), []).append(
                (int(payload.get("chunk_index", 0)), str(payload.get("text", "")))
            )
        for doc_id, chunks in by_doc.items():
            docs[doc_id] = " ".join(text for _idx, text in sorted(chunks))

        points: list[PointStruct] = []
        for pid, payload in plain:
            if pid in done_ids:
                continue
            text = str(payload.get("text", ""))
            document = docs.get(int(payload.get("doc_id", 0)), text)
            blurb = contextualize_chunk(
                document,
                text,
                base_url=settings.LITELLM_BASE_URL,
                model=settings.CONTEXT_MODEL,
                max_doc_chars=settings.CONTEXT_MAX_DOC_CHARS,
            )
            if blurb:
                enriched += 1
            time.sleep(throttle_s)
            points.append(
                PointStruct(
                    id=pid,
                    vector=Document(text=contextualized_text(blurb, text), model=EMBED_MODEL),
                    payload={**payload, "context": blurb},
                )
            )
        if points:
            client.upsert(collection_name=CONTEXTUAL_COLLECTION, points=points, wait=True)
        logger.info(
            "ctx %s: %d plain, %d upserted this run (%d already enriched)",
            ticker,
            len(plain),
            len(points),
            len(done_ids),
        )
    return enriched


def _ctx_enriched_ids(client: QdrantClient, flt: Any) -> set[int]:
    """Ctx point ids whose payload already carries a non-empty ``context``.

    Only successfully-enriched points count as done, so a re-run retries the ones
    a 429 left plain (empty context). Returns ``set()`` if the ctx collection does
    not exist yet (first build); a transient/auth error propagates rather than
    masquerading as "nothing enriched" (which would force a full re-enrichment).
    """
    from qdrant_client.http.exceptions import UnexpectedResponse

    ids: set[int] = set()
    offset: Any = None
    try:
        while True:
            recs, offset = client.scroll(
                collection_name=CONTEXTUAL_COLLECTION,
                scroll_filter=flt,
                limit=256,
                with_payload=True,
                with_vectors=False,
                offset=offset,
            )
            ids.update(
                int(r.id)
                for r in recs
                if isinstance(r.id, int) and (r.payload or {}).get("context")
            )
            if offset is None:
                return ids
    except UnexpectedResponse as exc:
        if exc.status_code == 404:  # collection not created yet on the first build
            return set()
        raise


def run_contextual() -> int:
    """``--contextual``: build the ctx index, then A/B plain vs contextual (QNT-273).

    Enriches + embeds the earnings corpus into ``equity_earnings_ctx`` (idempotent),
    then runs dense retrieval over the plain ``equity_earnings`` and the contextual
    index for the EARNINGS labeled queries, scores both against the shared qrels,
    appends a history row, and prints the before/after scorecard. This is the AC3
    artifact — the measured lift (or null result) that the ship-or-revert decision
    is recorded against in the PR.
    """
    queries = load_retrieval_queries()
    earnings = [q for q in queries if q.corpus == "earnings"]
    if not earnings:
        print("no earnings queries in the labeled set — nothing to A/B", file=sys.stderr)
        return 1
    qrels = load_qrels_trec()
    problems = _check_alignment(queries, ("qrels", set(qrels)))
    if problems:
        print("label the corpus first (--label):\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1

    client = _qdrant_client()
    n_enriched = build_contextual_index(
        client, earnings, throttle_s=settings.CONTEXT_THROTTLE_SECONDS
    )
    print(f"contextual index ready: {n_enriched} chunks enriched this run\n")

    earnings_qrels = {q.id: qrels[q.id] for q in earnings if q.id in qrels}
    plain_run: dict[str, dict[str, float]] = {}
    ctx_run: dict[str, dict[str, float]] = {}
    for q in earnings:
        plain_run[q.id] = {str(pid): s for pid, s in dense_run_ids(client, q)}
        ctx_run[q.id] = {
            str(pid): s for pid, s in dense_run_ids(client, q, collection=CONTEXTUAL_COLLECTION)
        }

    plain_metrics = compute_metrics(earnings_qrels, plain_run)
    ctx_metrics = compute_metrics(earnings_qrels, ctx_run)
    rid = append_retrieval_history(ctx_metrics, n_queries=len(earnings))
    print(
        summarise_comparison(
            plain_metrics, ctx_metrics, n_queries=len(earnings), label="contextual"
        )
    )
    print(f"\nhistory run_id: {rid}")
    return 0


def score_offline() -> int:
    """Default: offline frozen qrels + SERVED run -> metrics + gate. The CI path.

    QNT-262 follow-up: scores the frozen hybrid+rerank run (the path prod serves),
    not the dense reference, so the per-PR gate guards the served retrieval.
    """
    queries = load_retrieval_queries()
    qrels = load_qrels_trec()
    run = load_run_trec(RETRIEVAL_RUN_HYBRID_PATH)
    problems = _check_alignment(queries, ("qrels", set(qrels)), ("run", set(run)))
    if problems:
        print(
            "frozen artifacts out of sync with topics (re-run --label / --hybrid --rerank):\n  "
            + "\n  ".join(problems),
            file=sys.stderr,
        )
        return 1
    metrics = compute_metrics(qrels, run)
    print(summarise(metrics, n_queries=len(queries), label="served path: hybrid+rerank"))
    return 1 if gate_failures(metrics) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.evals.retrieval_eval")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--label", action="store_true", help="Write qrels from the live corpus.")
    group.add_argument("--baseline", action="store_true", help="Record dense baseline (live).")
    group.add_argument(
        "--hybrid",
        action="store_true",
        help="Live dense-vs-hybrid A/B + before/after scorecard (QNT-262).",
    )
    group.add_argument(
        "--contextual",
        action="store_true",
        help="Build the contextual index + dense plain-vs-contextual A/B (QNT-273).",
    )
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="With --hybrid, add the Cohere rerank layer (no-op without a key).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.label:
        return label_corpus()
    if args.baseline:
        return run_baseline()
    if args.hybrid:
        return run_hybrid(rerank=args.rerank)
    if args.contextual:
        return run_contextual()
    return score_offline()


__all__ = [
    "EMBED_MODEL",
    "GATE_FLOORS",
    "MAX_QUERIES",
    "METRICS",
    "MIN_QUERIES",
    "RETRIEVAL_FIELDS",
    "RETRIEVAL_GOLDENS_PATH",
    "RETRIEVAL_HISTORY_PATH",
    "RETRIEVAL_QRELS_PATH",
    "RETRIEVAL_RUN_HYBRID_PATH",
    "RETRIEVAL_RUN_PATH",
    "RUN_DEPTH",
    "CONTEXTUAL_COLLECTION",
    "RetrievalQuery",
    "append_retrieval_history",
    "build_contextual_index",
    "compute_metrics",
    "corpus_texts",
    "gate_failures",
    "hybrid_run_ids",
    "load_qrels_trec",
    "load_retrieval_queries",
    "load_run_trec",
    "summarise",
    "summarise_comparison",
    "write_qrels_trec",
    "write_run_trec",
]


if __name__ == "__main__":
    sys.exit(main())
