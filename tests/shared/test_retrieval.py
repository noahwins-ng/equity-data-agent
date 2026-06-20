"""Unit tests for the pure hybrid-retrieval primitives (QNT-262).

RRF fusion and BM25 ranking are deterministic and dependency-light, so they are
unit-tested directly here (the Qdrant orchestration that feeds them lives in the
api/eval consumers and is tested there). Cohere rerank is network-bound and
tested via its no-op/fallback contract in the API hybrid tests.
"""

from __future__ import annotations

from shared.retrieval import bm25_ranking, cohere_rerank, reciprocal_rank_fusion


def test_rrf_rewards_agreement_across_rankers() -> None:
    # "b" is mid-ranked by both retrievers; "a" is #1 in one but absent from the
    # other. RRF should lift the doc both agree on above either's lone favourite.
    dense = ["a", "b", "c"]
    sparse = ["d", "b", "e"]
    fused = reciprocal_rank_fusion([dense, sparse])
    ids = [doc for doc, _ in fused]
    assert ids[0] == "b"
    # Every doc surfaced by either ranker appears exactly once.
    assert set(ids) == {"a", "b", "c", "d", "e"}


def test_rrf_scores_descending_and_deterministic() -> None:
    fused = reciprocal_rank_fusion([["x", "y"], ["x", "z"]])
    scores = [s for _, s in fused]
    assert scores == sorted(scores, reverse=True)
    # "x" is #1 in both lists -> strictly highest.
    assert fused[0][0] == "x"


def test_rrf_empty_input() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


def test_bm25_ranks_exact_term_match_first() -> None:
    corpus = {
        "1": "NVDA data center revenue grew",
        "2": "NVDA inks SK Hynix HBM supply deal",
        "3": "chip demand was mixed",
    }
    ranked = bm25_ranking(corpus, "SK Hynix supply", limit=5)
    assert ranked[0] == "2"


def test_bm25_excludes_zero_score_docs() -> None:
    # A query term present in no doc -> no lexical hits, empty ranking (so fusion
    # is not padded with noise).
    corpus = {"1": "alpha beta", "2": "gamma delta"}
    assert bm25_ranking(corpus, "zzz", limit=5) == []


def test_bm25_empty_corpus_or_query() -> None:
    assert bm25_ranking({}, "anything", limit=5) == []
    assert bm25_ranking({"1": "text"}, "   ", limit=5) == []


def test_cohere_rerank_returns_none_without_key() -> None:
    # No key -> no-op (None), so the caller keeps the fused order. No network.
    assert cohere_rerank("q", {"1": "doc"}, api_key="", model="rerank-v3.5", top_n=5) is None


def test_cohere_rerank_returns_none_for_empty_docs() -> None:
    assert cohere_rerank("q", {}, api_key="key", model="rerank-v3.5", top_n=5) is None
