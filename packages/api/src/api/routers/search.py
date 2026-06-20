"""Search endpoints — semantic news search over Qdrant Cloud (QNT-55).

``GET /api/v1/search/news?query=earnings&ticker=NVDA&limit=5`` issues a vector
search against the ``equity_news`` collection. The query string is sent as a
``Document(text, model)`` so Qdrant Cloud Inference embeds it server-side with
the same ``sentence-transformers/all-minilm-l6-v2`` model the
``news_embeddings`` Dagster asset uses when writing points — keeping embed-time
and query-time vectors in the same space.

The endpoint is defensive by design: if Qdrant is unreachable, the collection
is missing, or no points match, it returns ``[]`` with HTTP 200 rather than
surfacing the error. The frontend renders "no results" the same way as
"service down" — a partial-outage UX that matches ``/api/v1/health``'s
``degraded`` state.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from shared.config import settings
from shared.retrieval import bm25_ranking, cohere_rerank, reciprocal_rank_fusion
from shared.tickers import TICKERS

from api.qdrant import get_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/search", tags=["search"])

COLLECTION = "equity_news"
EMBED_MODEL = "sentence-transformers/all-minilm-l6-v2"

# Pagination page size for the BM25 corpus scroll (hybrid path). The corpus is
# the 7-day rolling, ticker-scoped news slice (ADR-009) — tens of docs — so one
# page almost always suffices; the loop is a safety net, not a hot loop.
_SCROLL_PAGE = 256

# QNT-226: relevance filter. MiniLM-L6 cosine scores on short finance text are
# query-dependent and cluster tightly -- a clean-window calibration pull showed
# top scores ranging 0.42 ("TSLA with ASML") to 0.63 ("NVDA CEO data center")
# across queries, with genuinely-relevant hits on a weak-signal query landing
# BELOW a strong query's padding (the ASML-deal headline scored 0.37, under
# generic Tesla-stock noise at 0.42). So a query-independent absolute floor is
# unsafe: any floor high enough to cut a strong query's tail would erase a weak
# query's signal. We keep hits within ``_RELEVANCE_GAP`` of the top score, which
# trims clear low-score padding relative to the best match without a fixed
# cutoff. ``_MIN_SCORE`` is a degenerate-query guard only -- it never binds on
# observed real queries (the lowest relevant hit measured was ~0.35) and exists
# to drop a query that matches nothing (uniformly sub-0.30 scores).
#
# This is a tail-trim, not a precision fix: on weak-discrimination queries
# MiniLM ranks noise above signal, and no gap value reorders that -- a larger
# embedding model is the real fix (out of scope per the ticket). Lexical
# re-ranking on query entities would help but is its own change.
_RELEVANCE_GAP = 0.08
_MIN_SCORE = 0.30


def _apply_relevance_filter(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop hits that fall a relevance gap below the top match.

    ``rows`` arrives pre-sorted by descending score (Qdrant ranks the response),
    so ``rows[0]`` is the top match. Keeps every row at or above
    ``max(top - _RELEVANCE_GAP, _MIN_SCORE)``. An empty response is returned
    unchanged; a single row is kept unless its score is below ``_MIN_SCORE`` (the
    degenerate-query guard — a lone sub-floor hit means the query matched
    nothing meaningful and is dropped).
    """
    if not rows:
        return rows
    top = rows[0]["score"]
    if not isinstance(top, int | float):
        return rows
    cutoff = max(top - _RELEVANCE_GAP, _MIN_SCORE)
    return [r for r in rows if isinstance(r["score"], int | float) and r["score"] >= cutoff]


def _row_from_payload(payload: dict[str, Any], score: float | None) -> dict[str, Any]:
    """Build the display row the frontend + agent consume from a Qdrant payload.

    Shared by the dense and hybrid paths so both surface identical fields
    (``headline, source, date, score, url, body``). ``date`` is an ISO date
    string derived from the stored unix-seconds ``published_at``.
    """
    published_unix = payload.get("published_at")
    if isinstance(published_unix, int | float):
        date_str: str | None = datetime.fromtimestamp(published_unix, tz=UTC).date().isoformat()
    else:
        date_str = None
    return {
        "headline": payload.get("headline"),
        "source": payload.get("source"),
        "date": date_str,
        "score": score,
        "url": payload.get("url"),
        # QNT-225: the article summary (Finnhub body) so the agent reads the
        # story, not just the headline. Empty string for points embedded before
        # QNT-225 (headline-only) until they roll out of the 7-day window.
        "body": payload.get("body") or "",
    }


def _bm25_text(payload: dict[str, Any]) -> str:
    """The lexical haystack BM25 scores — headline + body, mirroring embed text."""
    headline = str(payload.get("headline") or "")
    body = str(payload.get("body") or "")
    return f"{headline}\n\n{body}".strip()


def _scroll_ticker_corpus(
    client: Any, query_filter: Any
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Page the ticker-scoped corpus into ``{id: bm25_text}`` + ``{id: payload}``.

    The full ticker slice (not just the dense hits) feeds BM25 so a lexical-only
    match the dense ranker missed can still enter the fusion — the whole point of
    hybrid. Ids are stringified to share a key space with the dense ranking.
    """
    corpus_text: dict[str, str] = {}
    payloads: dict[str, dict[str, Any]] = {}
    offset: Any = None
    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION,
            scroll_filter=query_filter,
            limit=_SCROLL_PAGE,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        for p in points:
            payload = p.payload or {}
            doc_id = str(p.id)
            corpus_text[doc_id] = _bm25_text(payload)
            payloads[doc_id] = payload
        if offset is None:
            break
    return corpus_text, payloads


def _hybrid_search(query: str, ticker: str, limit: int, rerank: bool) -> list[dict[str, Any]]:
    """Dense + BM25 RRF fusion (QNT-262), with an optional Cohere rerank layer.

    Fuses the dense MiniLM ranking with a client-side BM25 ranking over the
    ticker-scoped corpus via RRF; when ``rerank`` is set and a Cohere key is
    configured, a cross-encoder reorders the fused candidate set for precision.
    Falls back to ``[]`` on any Qdrant transient — same degraded contract as the
    dense path. The cosine relevance-gap trim (QNT-226) is dense-only and does
    not apply here: fusion + rerank ordering (and BM25 zero-score exclusion) do
    the trimming, and RRF scores are not on the cosine scale that trim assumes.
    """
    import httpx
    from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
    from qdrant_client.models import Document, FieldCondition, Filter, MatchValue

    flt = Filter(must=[FieldCondition(key="ticker", match=MatchValue(value=ticker))])
    # Wide candidate pool so the reranker / fusion can pull a buried hit into the
    # returned top-k (4-8). RERANK_CANDIDATES is the fused-set width; cap the
    # dense fetch so a large `limit` (public endpoint, up to 50) can't drive an
    # oversized dense query — the BM25 corpus is the full ticker slice regardless.
    fetch_k = min(max(settings.RERANK_CANDIDATES, limit * 4), 50)
    client = get_client()
    try:
        dense = client.query_points(
            collection_name=COLLECTION,
            query=Document(text=query, model=EMBED_MODEL),
            query_filter=flt,
            limit=fetch_k,
            with_payload=False,
        )
        corpus_text, payloads = _scroll_ticker_corpus(client, flt)
    except (ResponseHandlingException, UnexpectedResponse, httpx.HTTPError) as exc:
        logger.warning(
            "Qdrant hybrid search failed for query=%r ticker=%r", query, ticker, exc_info=exc
        )
        return []

    dense_ids = [str(p.id) for p in dense.points]
    dense_scores = {str(p.id): p.score for p in dense.points}
    bm25_ids = bm25_ranking(corpus_text, query, limit=fetch_k)
    fused = reciprocal_rank_fusion([dense_ids, bm25_ids])
    candidate_ids = [doc_id for doc_id, _ in fused[: settings.RERANK_CANDIDATES]]

    order = candidate_ids
    rerank_scores: dict[str, float] = {}
    if rerank and settings.COHERE_API_KEY:
        docs = {doc_id: corpus_text.get(doc_id, "") for doc_id in candidate_ids}
        reranked = cohere_rerank(
            query,
            docs,
            api_key=settings.COHERE_API_KEY,
            model=settings.COHERE_RERANK_MODEL,
            top_n=limit,
        )
        if reranked is not None:
            order = [doc_id for doc_id, _ in reranked]
            rerank_scores = dict(reranked)

    rows: list[dict[str, Any]] = []
    for doc_id in order[:limit]:
        payload = payloads.get(doc_id)
        if payload is None:
            continue
        # Reranked rows carry the Cohere relevance score (0-1, cosine-like);
        # non-reranked rows keep the dense cosine where the doc was a dense hit,
        # else None (a BM25-only hit has no comparable vector score).
        score = rerank_scores.get(doc_id, dense_scores.get(doc_id))
        rows.append(_row_from_payload(payload, score))
    return rows


@router.get("/news")
def search_news(
    query: str = Query(
        ...,
        min_length=1,
        max_length=512,
        description="Natural-language search text",
    ),
    ticker: str | None = Query(
        default=None,
        description="Restrict results to a single ticker from shared.tickers.TICKERS",
    ),
    limit: int = Query(default=5, ge=1, le=50),
    hybrid: bool = Query(
        default=False,
        description="QNT-262: fuse dense + BM25 (RRF). Requires a ticker; ignored otherwise.",
    ),
    rerank: bool = Query(
        default=False,
        description="QNT-262: Cohere rerank the fused set. No-ops without a Cohere key.",
    ),
) -> list[dict[str, Any]]:
    """Return the top-``limit`` news headlines most semantically similar to ``query``.

    Response rows carry the display fields required by the frontend + agent
    (``headline, source, date, score, url``) pre-sorted by descending
    relevance. ``date`` is an ISO date string derived from the stored unix
    seconds payload — the frontend renders it directly without reparsing.
    """
    import httpx
    from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
    from qdrant_client.models import Document, FieldCondition, Filter, MatchValue

    if ticker is not None:
        ticker = ticker.upper()
        if ticker not in TICKERS:
            raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")

    # QNT-262: hybrid (dense + BM25 RRF, optional rerank) needs a ticker to keep
    # the BM25 corpus scroll bounded. Without one, or with the master switch off,
    # fall through to the unchanged dense path (and its QNT-226 relevance trim).
    if hybrid and settings.HYBRID_SEARCH_ENABLED and ticker is not None:
        return _hybrid_search(query, ticker, limit, rerank)

    query_filter: Filter | None = None
    if ticker is not None:
        query_filter = Filter(must=[FieldCondition(key="ticker", match=MatchValue(value=ticker))])

    try:
        response = get_client().query_points(
            collection_name=COLLECTION,
            query=Document(text=query, model=EMBED_MODEL),
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
    except (ResponseHandlingException, UnexpectedResponse, httpx.HTTPError) as exc:
        # Transient outages (network, timeout, missing collection) must not
        # take the API down — the frontend renders [] as "no news" and the
        # agent's news tool degrades to the report endpoint. Auth/config
        # errors fall through to the default 500 handler so misconfigured
        # credentials surface loudly instead of silently returning [].
        logger.warning(
            "Qdrant search failed for query=%r ticker=%r",
            query,
            ticker,
            exc_info=exc,
        )
        return []

    rows: list[dict[str, Any]] = []
    for point in response.points:
        payload = point.payload or {}
        published_unix = payload.get("published_at")
        if isinstance(published_unix, int | float):
            date_str = datetime.fromtimestamp(published_unix, tz=UTC).date().isoformat()
        else:
            date_str = None
        rows.append(
            {
                "headline": payload.get("headline"),
                "source": payload.get("source"),
                "date": date_str,
                "score": point.score,
                "url": payload.get("url"),
                # QNT-225: the article summary (Finnhub body) so the agent reads
                # the story, not just the headline. Empty string for points
                # embedded before QNT-225 (headline-only) until they roll out of
                # the 7-day window; the agent renders it only when present.
                "body": payload.get("body") or "",
            }
        )
    # QNT-226: trim low-relevance padding before returning. The agent folds
    # these rows into the synthesis prompt AND surfaces them as provenance, so
    # dropping the tail here benefits both consumers in one place.
    return _apply_relevance_filter(rows)
