from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from dagster import ConfigurableResource
from pydantic import Field
from shared.config import settings

if TYPE_CHECKING:
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAY = 2.0  # seconds


class QdrantCollectionSpec(ConfigurableResource):
    """Static description of a Qdrant collection. Kept next to the resource so
    callers pass a single object to ``ensure_collection`` instead of loose args."""

    name: str
    vector_size: int
    distance: str = "Cosine"  # one of Cosine | Euclid | Dot
    payload_indexes: dict[str, str] = Field(default_factory=dict)
    """Map of payload_field → schema_type (e.g. {"ticker": "keyword", "published_at": "integer"}).
    Indexes are created idempotently."""


class QdrantResource(ConfigurableResource):
    """Dagster resource wrapping a Qdrant Cloud client.

    Mirrors ``ClickHouseResource``: empty defaults that fall back to
    ``shared.settings`` so tests can override via Dagster config without
    touching env vars. Retries transient I/O errors with a flat backoff
    (``_MAX_RETRIES`` / ``_RETRY_DELAY``) consistent with the ClickHouse path.
    """

    url: str = Field(default="")
    api_key: str = Field(default="")
    timeout: int = 30
    cloud_inference: bool = True  # ADR-009: embed server-side

    def _client(self) -> QdrantClient:
        from qdrant_client import QdrantClient

        return QdrantClient(
            url=self.url or settings.QDRANT_URL,
            api_key=self.api_key or settings.QDRANT_API_KEY,
            timeout=self.timeout,
            cloud_inference=self.cloud_inference,
        )

    def ensure_collection(self, spec: QdrantCollectionSpec) -> None:
        """Idempotently create the collection + payload indexes.

        Called on every asset run so a fresh Qdrant (or new collection) gets
        bootstrapped without a separate migration path — analogous to
        ``CREATE TABLE IF NOT EXISTS`` for ClickHouse.
        """
        from qdrant_client.models import Distance, PayloadSchemaType, VectorParams

        distance = {
            "Cosine": Distance.COSINE,
            "Euclid": Distance.EUCLID,
            "Dot": Distance.DOT,
        }[spec.distance]

        client = self._client()
        existing = {c.name for c in client.get_collections().collections}
        if spec.name not in existing:
            client.create_collection(
                collection_name=spec.name,
                vectors_config=VectorParams(size=spec.vector_size, distance=distance),
            )
            logger.info("Created Qdrant collection %s", spec.name)

        schema_map = {
            "keyword": PayloadSchemaType.KEYWORD,
            "integer": PayloadSchemaType.INTEGER,
            "float": PayloadSchemaType.FLOAT,
            "bool": PayloadSchemaType.BOOL,
        }
        for field, schema_name in spec.payload_indexes.items():
            schema = schema_map[schema_name]
            # create_payload_index is idempotent on the server; duplicate calls
            # return 200 with "already exists" so no try/except is needed.
            client.create_payload_index(
                collection_name=spec.name,
                field_name=field,
                field_schema=schema,
            )

    def upsert_points(self, collection: str, points: list[PointStruct]) -> None:
        """Upsert points into ``collection``. Retries transient failures."""
        if not points:
            return

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                self._client().upsert(collection_name=collection, points=points, wait=True)
                return
            except Exception as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "Qdrant upsert failed (attempt %d/%d): %s — retrying in %.1fs",
                        attempt,
                        _MAX_RETRIES,
                        exc,
                        _RETRY_DELAY,
                    )
                    time.sleep(_RETRY_DELAY)
        raise RuntimeError(f"Qdrant upsert failed after {_MAX_RETRIES} attempts") from last_exc

    def count(self, collection: str, query_filter: Any | None = None) -> int:
        """Return the number of points in a collection (optionally filtered).

        Thin wrapper used by tests + asset-check diagnostics.
        """
        res = self._client().count(
            collection_name=collection,
            count_filter=query_filter,
            exact=True,
        )
        return res.count
