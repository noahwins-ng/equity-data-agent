"""Per-PR deterministic RAG eval gate (QNT-261 AC5).

This is the merge gate the design doc wires into ci.yml: the ``ir_measures``
retrieval metrics + the in-house number-grounding faithfulness check, both
DETERMINISTIC and LLM-free, scored against committed frozen artifacts (no
network, no secrets). Marked ``eval`` so ci.yml runs it as its own blocking step
(``-m eval``); the live ``--label`` / ``--baseline`` modes are NOT collected here
(they need Qdrant). Mirrors test_news_search_yaml: lock the invariants the live
runner can't catch without a corpus.
"""

from __future__ import annotations

import pytest
from agent.evals.golden_set import HISTORY_FIELDS
from agent.evals.hallucination import check
from agent.evals.retrieval_eval import (
    GATE_FLOORS,
    MAX_QUERIES,
    MIN_QUERIES,
    compute_metrics,
    gate_failures,
    load_qrels_trec,
    load_retrieval_queries,
    load_run_trec,
)
from shared.tickers import TICKERS

pytestmark = pytest.mark.eval


# --- topics file (AC1) ---------------------------------------------------------


def test_topics_load_and_validate() -> None:
    queries = load_retrieval_queries()
    assert MIN_QUERIES <= len(queries) <= MAX_QUERIES


def test_topic_ids_unique() -> None:
    ids = [q.id for q in load_retrieval_queries()]
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"


def test_topic_tickers_in_registry() -> None:
    for q in load_retrieval_queries():
        assert q.ticker in TICKERS, f"{q.id}: unknown ticker {q.ticker!r}"


def test_topics_cover_news_and_earnings() -> None:
    corpora = {q.corpus for q in load_retrieval_queries()}
    assert {"news", "earnings"} <= corpora, f"both corpora required, got {corpora}"


# --- frozen labels + run align with the topics (AC1/AC4) -----------------------


def test_qrels_align_with_topics() -> None:
    topic_ids = {q.id for q in load_retrieval_queries()}
    qrels = load_qrels_trec()
    assert set(qrels) == topic_ids, "qrels out of sync with topics -- re-run --label"
    for qid, judged in qrels.items():
        assert judged, f"{qid}: no relevant docs labeled"


def test_run_covers_every_topic() -> None:
    topic_ids = {q.id for q in load_retrieval_queries()}
    run = load_run_trec()
    assert set(run) == topic_ids, "frozen run out of sync with topics -- re-run --baseline"


# --- the retrieval gate (AC2/AC5): metrics meet their floors -------------------


def test_retrieval_metrics_pass_gate() -> None:
    qrels = load_qrels_trec()
    run = load_run_trec()
    metrics = compute_metrics(qrels, run)
    failures = gate_failures(metrics)
    assert not failures, "retrieval regression: " + "; ".join(failures)


def test_all_floors_have_metrics() -> None:
    """Every gated metric name must actually be produced by compute_metrics,
    otherwise a typo'd floor key would silently never gate."""
    metrics = compute_metrics(load_qrels_trec(), load_run_trec())
    for name in GATE_FLOORS:
        assert name in metrics, f"gate floor {name!r} has no matching metric"


# --- history schema carries the retrieval columns (AC3) ------------------------


def test_history_schema_has_retrieval_columns() -> None:
    for col in ("recall_at_5", "recall_at_20", "mrr", "ndcg_at_10", "retrieval_n"):
        assert col in HISTORY_FIELDS, f"history.csv missing retrieval column {col!r}"


# --- number-grounding faithfulness gate (AC5, second deterministic layer) ------


def test_number_grounding_flags_unsupported() -> None:
    """A thesis figure absent from every report is an unsupported (hallucinated)
    number -- the gate must catch it."""
    result = check("Revenue hit $48 billion this quarter.", ["Revenue was $35 billion."])
    assert not result.ok
    assert "48" in result.unsupported


def test_number_grounding_passes_grounded() -> None:
    """A thesis whose figures all appear in a report is clean -- the gate must
    not false-positive on grounded numbers."""
    result = check("RSI is 72.5 and revenue was $35 billion.", ["RSI 72.5; revenue $35 billion."])
    assert result.ok, f"unexpected unsupported numbers: {result.unsupported}"
