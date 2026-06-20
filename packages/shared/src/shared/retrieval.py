"""Hybrid-retrieval primitives: BM25 lexical ranking, RRF fusion, Cohere rerank.

QNT-262 adds hybrid retrieval (dense + sparse/BM25, fused with reciprocal rank
fusion) and an optional Cohere Rerank 3.5 cross-encoder layer on top. These are
the *pure* building blocks — no Qdrant, no embedding model — so both consumers
of retrieval share one implementation:

* the production search path (``api.routers.search``), and
* the component-level retrieval eval (``agent.evals.retrieval_eval``).

Each consumer owns its own Qdrant access (dense query + the corpus scroll that
feeds BM25); this module only fuses and reranks the resulting id lists. The
collections stay dense-only — BM25 is computed client-side over the
ticker-scoped corpus, so there is no schema change and no re-index (see the
QNT-262 design decision in docs/v2-overall-enhancement.md Track 2).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

# Reciprocal-rank-fusion constant. 60 is the value from the original Cormack
# et al. RRF paper and the Qdrant/Elastic defaults; it damps the contribution of
# deep-rank hits so a doc ranked #1 by one retriever dominates a doc ranked #40
# by both. Not tuned per-corpus — the standard default is the honest baseline.
RRF_K = 60

# Lightweight word tokenizer shared by BM25. Lowercase + split on non-word runs:
# good enough for the entity/event terms hybrid exists to catch ("SK Hynix", a
# named lawsuit) without dragging in an NLP dependency.
_TOKEN_RE = re.compile(r"\w+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]], *, k: int = RRF_K
) -> list[tuple[str, float]]:
    """Fuse several ranked id lists into one, scored by reciprocal rank fusion.

    Each element of ``rankings`` is a list of doc ids ordered best-first (rank 1
    = most relevant). A doc's fused score is ``sum(1 / (k + rank))`` over every
    list it appears in (1-based rank), so a doc surfaced by multiple retrievers
    outranks one surfaced strongly by a single retriever. Returns ``(id, score)``
    pairs sorted by descending score; ties broken by id for determinism.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def bm25_ranking(corpus: Mapping[str, str], query: str, *, limit: int) -> list[str]:
    """Rank ``corpus`` ({id: text}) against ``query`` by BM25, best-first.

    Pure lexical signal — the half of hybrid that catches exact entity/event
    terms where dense MiniLM blurs them. Returns up to ``limit`` ids ordered by
    descending BM25 score. An empty corpus or query yields ``[]``.
    """
    if not corpus or not query.strip():
        return []
    from rank_bm25 import BM25Okapi

    ids = list(corpus)
    tokenized = [_tokenize(corpus[i]) for i in ids]
    bm25 = BM25Okapi(tokenized)
    scores = bm25.get_scores(_tokenize(query))
    ranked = sorted(zip(ids, scores, strict=True), key=lambda kv: (-kv[1], kv[0]))
    # Drop zero-score docs: a doc sharing no query term is not a lexical hit and
    # should not pad the fusion candidate list with noise.
    return [doc_id for doc_id, score in ranked[:limit] if score > 0.0]


def cohere_rerank(
    query: str,
    documents: Mapping[str, str],
    *,
    api_key: str,
    model: str,
    top_n: int,
) -> list[tuple[str, float]] | None:
    """Reorder ``documents`` ({id: text}) by Cohere Rerank relevance to ``query``.

    Returns ``(id, relevance_score)`` for the top-``top_n`` documents, descending.
    Returns ``None`` (not an exception) when the key is empty or the call fails,
    so callers fall back to the fused order rather than erroring — rerank is an
    additive precision layer, never a hard dependency of the search path.
    """
    if not api_key or not documents:
        return None

    import httpx

    ids = list(documents)
    texts = [documents[i] for i in ids]
    try:
        response = httpx.post(
            "https://api.cohere.com/v2/rerank",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "query": query,
                "documents": texts,
                "top_n": min(top_n, len(texts)),
            },
            timeout=10.0,
        )
        response.raise_for_status()
        results = response.json()["results"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning(
            "cohere_rerank failed (model=%s): %s — falling back to fused order", model, exc
        )
        return None

    reranked: list[tuple[str, float]] = []
    for item in results:
        idx = item.get("index")
        if isinstance(idx, int) and 0 <= idx < len(ids):
            reranked.append((ids[idx], float(item.get("relevance_score", 0.0))))
    return reranked


__all__ = [
    "RRF_K",
    "bm25_ranking",
    "cohere_rerank",
    "reciprocal_rank_fusion",
]
