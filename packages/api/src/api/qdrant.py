"""Thin Qdrant Cloud client wrapper shared by search endpoints.

Mirrors ``api.clickhouse.get_client``: one cached client per process, created
lazily on first use. ``cloud_inference=True`` so callers can pass a
``Document(text, model)`` query and let Qdrant embed server-side — matching
how the ``news_embeddings`` Dagster asset writes points.
"""

from __future__ import annotations

from functools import lru_cache

from qdrant_client import QdrantClient
from shared.config import settings


@lru_cache(maxsize=1)
def get_client() -> QdrantClient:
    # timeout=8 accommodates Qdrant Cloud Inference cold-start (server-side
    # embed + ANN) while still meeting the <1s AC for warm queries — a 3s
    # timeout trips the endpoint's empty-results fallback silently under
    # contention and masks transient slowness as "no results".
    return QdrantClient(
        url=settings.QDRANT_URL,
        api_key=settings.QDRANT_API_KEY,
        timeout=8,
        cloud_inference=True,
    )
