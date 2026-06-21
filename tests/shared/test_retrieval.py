"""Unit tests for the pure hybrid-retrieval primitives (QNT-262).

RRF fusion and BM25 ranking are deterministic and dependency-light, so they are
unit-tested directly here (the Qdrant orchestration that feeds them lives in the
api/eval consumers and is tested there). Cohere rerank is network-bound and
tested via its no-op/fallback contract in the API hybrid tests.
"""

from __future__ import annotations

from typing import Any

import httpx
from shared.retrieval import (
    bm25_ranking,
    cohere_rerank,
    contextualize_chunk,
    contextualized_text,
    reciprocal_rank_fusion,
)


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


# --- QNT-273: contextual chunk enrichment ------------------------------------


def test_contextualized_text_prepends_or_passes_through() -> None:
    assert (
        contextualized_text("A 2025 release.", "Revenue rose.")
        == "A 2025 release.\n\nRevenue rose."
    )
    # Empty/whitespace context -> plain chunk, never a stray blank prefix.
    assert contextualized_text("", "Revenue rose.") == "Revenue rose."
    assert contextualized_text("   ", "Revenue rose.") == "Revenue rose."


def test_contextualize_chunk_empty_chunk_short_circuits() -> None:
    # No chunk -> no network call, empty context.
    assert contextualize_chunk("doc", "  ", base_url="http://x", model="m") == ""


def test_contextualize_chunk_success(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url, **kwargs):  # noqa: ANN001, ANN003
        captured["url"] = url
        captured["json"] = kwargs["json"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": " NVIDIA Q1 data center revenue.\n"}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    out = contextualize_chunk(
        "full doc", "a chunk", base_url="http://proxy", model="equity-agent/small"
    )
    assert out == "NVIDIA Q1 data center revenue."  # stripped
    assert captured["url"] == "http://proxy/chat/completions"
    assert captured["json"]["model"] == "equity-agent/small"


def test_contextualize_chunk_failure_returns_empty(monkeypatch) -> None:
    def boom(url, **kwargs):  # noqa: ANN001, ANN003
        raise httpx.ConnectError("proxy down")

    monkeypatch.setattr(httpx, "post", boom)
    # Graceful: a proxy failure yields "" so the caller embeds the plain chunk.
    assert contextualize_chunk("doc", "chunk", base_url="http://x", model="m") == ""
