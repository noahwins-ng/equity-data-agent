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


def _is_transient_qdrant_error(exc: BaseException) -> bool:
    """QNT-117: classify a Qdrant exception as transient (worth retrying).

    Retry on:
      * ``ResourceExhaustedResponse`` — 429 with Retry-After (server-imposed rate-limit).
      * ``ResponseHandlingException`` wrapping an ``httpx.TransportError`` —
        qdrant_client's HTTP layer (``api_client.send_inner``) catches every
        request-level exception and re-raises as ``ResponseHandlingException``,
        so this is the path raw transport failures take in practice.
      * ``UnexpectedResponse`` with a 5xx status — server-side error.

    Everything else (auth 401/403, other 4xx, validation errors wrapped in
    ``ResponseHandlingException``) fails loud on first attempt.
    """
    import httpx
    from qdrant_client.common.client_exceptions import ResourceExhaustedResponse
    from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

    if isinstance(exc, ResourceExhaustedResponse):
        return True
    if isinstance(exc, ResponseHandlingException):
        return isinstance(exc.source, httpx.TransportError)
    if isinstance(exc, UnexpectedResponse):
        return exc.status_code is not None and 500 <= exc.status_code < 600
    return False


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
                if not _is_transient_qdrant_error(exc):
                    raise
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

    def delete_points_by_filter(self, collection: str, query_filter: Any) -> None:
        """Delete points in ``collection`` matching ``query_filter``.

        Used by news_embeddings (QNT-145) to GC ticker-scoped points whose
        ``published_at`` has aged past the rolling 7-day window. ADR-009
        designed Qdrant as the rolling-7d semantic search index; without GC
        the asset's in-window upsert filter let aged points accumulate
        monotonically, eventually drowning live points and locking the
        QNT-93 orphan check into permanent WARN.

        Retry semantics match ``upsert_points``: transient 5xx / 429 /
        transport errors retry up to ``_MAX_RETRIES``; everything else
        (auth, validation) fails loud on first attempt.
        """
        from qdrant_client.models import FilterSelector

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                self._client().delete(
                    collection_name=collection,
                    points_selector=FilterSelector(filter=query_filter),
                    wait=True,
                )
                return
            except Exception as exc:
                if not _is_transient_qdrant_error(exc):
                    raise
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "Qdrant delete failed (attempt %d/%d): %s — retrying in %.1fs",
                        attempt,
                        _MAX_RETRIES,
                        exc,
                        _RETRY_DELAY,
                    )
                    time.sleep(_RETRY_DELAY)
        raise RuntimeError(f"Qdrant delete failed after {_MAX_RETRIES} attempts") from last_exc

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

    def collection_dimension(self, collection: str) -> int:
        """Return the configured vector size for ``collection``.

        Qdrant enforces vector size at collection creation and rejects any upsert
        with a different size, so this value is a storage-side guarantee for every
        point in the collection — the 384-dim asset check (QNT-93) reads it here.
        """
        info = self._client().get_collection(collection_name=collection)
        vectors_config = info.config.params.vectors
        # Single-vector collections expose ``.size`` directly; named-vector
        # collections return a dict. QNT-93 only uses single-vector collections.
        size = getattr(vectors_config, "size", None)
        if size is None:
            raise RuntimeError(
                f"Qdrant collection {collection!r} has no single-vector size "
                f"(named-vector collections are not supported by this helper)"
            )
        return int(size)

    def scroll_ids(
        self,
        collection: str,
        query_filter: Any | None = None,
        page_size: int = 10_000,
        max_pages: int = 100,
    ) -> list[int]:
        """Return all point IDs from ``collection`` matching ``query_filter``.

        Paginates through the collection via Qdrant's ``next_page_offset`` so
        callers get a complete list rather than silently-truncated page 1.
        ``max_pages`` is a safety cap — hitting it raises ``RuntimeError`` so a
        runaway collection surfaces as a failed asset check with a stack trace,
        rather than passing on partial data.
        """
        client = self._client()
        all_ids: list[int] = []
        offset: Any = None
        for _ in range(max_pages):
            records, next_offset = client.scroll(
                collection_name=collection,
                scroll_filter=query_filter,
                limit=page_size,
                with_payload=False,
                with_vectors=False,
                offset=offset,
            )
            all_ids.extend(int(r.id) for r in records)
            if next_offset is None:
                return all_ids
            offset = next_offset
        raise RuntimeError(
            f"scroll_ids hit safety cap of {max_pages} pages × {page_size} for "
            f"{collection!r}; collection is larger than the current check assumes"
        )
