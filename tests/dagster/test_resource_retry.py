"""QNT-117: narrowed retry-on-Exception in ClickHouse + Qdrant resources.

Confirms each resource only retries on transient errors (transport / 5xx) and
fails loud immediately on auth, schema, programming, and other 4xx errors —
preserving the 3 × 2s retry budget for the cases where it actually helps.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from clickhouse_connect.driver.exceptions import (
    DatabaseError,
    OperationalError,
    ProgrammingError,
)
from dagster_pipelines.resources.clickhouse import ClickHouseResource
from dagster_pipelines.resources.qdrant import (
    QdrantResource,
    _is_transient_qdrant_error,
)
from qdrant_client.common.client_exceptions import ResourceExhaustedResponse
from qdrant_client.http.exceptions import (
    ResponseHandlingException,
    UnexpectedResponse,
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make retry tests instant — we don't care about wall-clock backoff."""
    monkeypatch.setattr("dagster_pipelines.resources.clickhouse.time.sleep", lambda _s: None)
    monkeypatch.setattr("dagster_pipelines.resources.qdrant.time.sleep", lambda _s: None)


# ---------------------------------------------------------------------------
# ClickHouse
# ---------------------------------------------------------------------------


def _patch_clickhouse_client(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    side_effect: Any,
) -> MagicMock:
    """Replace ``ClickHouseResource._client`` with a mock whose method behaves
    per ``side_effect``. Returns the underlying method mock so tests can assert
    call counts."""
    client = MagicMock()
    getattr(client, method).side_effect = side_effect
    monkeypatch.setattr(ClickHouseResource, "_client", lambda self: client)
    return getattr(client, method)


def test_clickhouse_retries_on_operational_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transient OperationalError → retry, eventually succeed."""
    result_sentinel = object()
    method = _patch_clickhouse_client(
        monkeypatch,
        "query",
        side_effect=[OperationalError("blip"), OperationalError("blip"), result_sentinel],
    )

    res = ClickHouseResource().execute("SELECT 1")

    assert res is result_sentinel
    assert method.call_count == 3


def test_clickhouse_exhausts_retries_on_persistent_operational_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent OperationalError exhausts the retry budget → RuntimeError."""
    method = _patch_clickhouse_client(
        monkeypatch,
        "query",
        side_effect=OperationalError("still down"),
    )

    with pytest.raises(RuntimeError, match="ClickHouse execute failed"):
        ClickHouseResource().execute("SELECT 1")

    assert method.call_count == 3


def test_clickhouse_does_not_retry_database_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """DatabaseError (non-transient — schema mismatch, auth) fails loud on first attempt."""
    method = _patch_clickhouse_client(
        monkeypatch,
        "query",
        side_effect=DatabaseError("Code: 60. Unknown table"),
    )

    with pytest.raises(DatabaseError):
        ClickHouseResource().execute("SELECT * FROM does_not_exist")

    assert method.call_count == 1


def test_clickhouse_does_not_retry_programming_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """ProgrammingError (SQL syntax, wrong column) fails loud on first attempt."""
    method = _patch_clickhouse_client(
        monkeypatch,
        "query",
        side_effect=ProgrammingError("Syntax error"),
    )

    with pytest.raises(ProgrammingError):
        ClickHouseResource().execute("SELECT bogus")

    assert method.call_count == 1


def test_clickhouse_query_df_narrowed_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """query_df shares the retry shell — verify it also fails loud on DatabaseError."""
    method = _patch_clickhouse_client(
        monkeypatch,
        "query_df",
        side_effect=DatabaseError("auth failed"),
    )

    with pytest.raises(DatabaseError):
        ClickHouseResource().query_df("SELECT 1")

    assert method.call_count == 1


def test_clickhouse_insert_df_narrowed_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """insert_df shares the retry shell — verify it also fails loud on DatabaseError."""
    import pandas as pd

    method = _patch_clickhouse_client(
        monkeypatch,
        "insert_df",
        side_effect=DatabaseError("column count mismatch"),
    )

    with pytest.raises(DatabaseError):
        ClickHouseResource().insert_df("equity_raw.foo", pd.DataFrame({"x": [1]}))

    assert method.call_count == 1


# ---------------------------------------------------------------------------
# Qdrant — classifier
# ---------------------------------------------------------------------------


def test_classifier_retries_response_handling_wrapping_transport() -> None:
    """The real prod path: qdrant_client.api_client.send_inner wraps every
    request-level exception in ResponseHandlingException, so a transport
    failure surfaces as RHE(source=httpx.TransportError) — not a raw httpx
    error. A raw httpx exception cannot escape the qdrant client today."""
    wrapped = ResponseHandlingException(httpx.ConnectTimeout("slow"))
    assert _is_transient_qdrant_error(wrapped) is True


def test_classifier_retries_resource_exhausted_response() -> None:
    """429 with Retry-After raises ResourceExhaustedResponse — separate class
    from UnexpectedResponse, so it needs its own classifier branch."""
    assert _is_transient_qdrant_error(ResourceExhaustedResponse("rate-limited", 5)) is True


def test_classifier_does_not_retry_response_handling_wrapping_validation_error() -> None:
    """ResponseHandlingException can also wrap a ValidationError (response-parse
    failure). That's a real schema mismatch — fail loud, don't retry."""
    wrapped = ResponseHandlingException(ValueError("bad schema"))
    assert _is_transient_qdrant_error(wrapped) is False


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_classifier_retries_unexpected_response_5xx(status: int) -> None:
    exc = UnexpectedResponse(
        status_code=status,
        reason_phrase="server error",
        content=b"",
        headers=httpx.Headers(),
    )
    assert _is_transient_qdrant_error(exc) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_classifier_does_not_retry_unexpected_response_4xx(status: int) -> None:
    exc = UnexpectedResponse(
        status_code=status,
        reason_phrase="client error",
        content=b"",
        headers=httpx.Headers(),
    )
    assert _is_transient_qdrant_error(exc) is False


def test_classifier_does_not_retry_arbitrary_exception() -> None:
    assert _is_transient_qdrant_error(RuntimeError("something else")) is False


# ---------------------------------------------------------------------------
# Qdrant — upsert retry behavior
# ---------------------------------------------------------------------------


def _patch_qdrant_client(monkeypatch: pytest.MonkeyPatch, side_effect: Any) -> MagicMock:
    client = MagicMock()
    client.upsert.side_effect = side_effect
    monkeypatch.setattr(QdrantResource, "_client", lambda self: client)
    return client.upsert


def _qdrant_point() -> Any:
    from qdrant_client.models import PointStruct

    return PointStruct(id=1, vector=[0.0, 0.0, 0.0])


def test_qdrant_retries_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport errors arrive at upsert_points wrapped as ResponseHandlingException
    (qdrant_client.api_client.send_inner re-raises every request exception that
    way). Mirror the prod path here — raising raw httpx errors would bypass the
    classifier branch this test is meant to exercise."""
    wrapped = ResponseHandlingException(httpx.ConnectTimeout("slow"))
    method = _patch_qdrant_client(monkeypatch, side_effect=[wrapped, wrapped, None])

    QdrantResource().upsert_points("c", [_qdrant_point()])

    assert method.call_count == 3


def test_qdrant_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 with Retry-After is the most common transient signal in prod —
    must consume the retry budget, not fail loud on first attempt."""
    method = _patch_qdrant_client(
        monkeypatch,
        side_effect=[ResourceExhaustedResponse("slow down", 1), None],
    )

    QdrantResource().upsert_points("c", [_qdrant_point()])

    assert method.call_count == 2


def test_qdrant_retries_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    server_5xx = UnexpectedResponse(
        status_code=503,
        reason_phrase="unavailable",
        content=b"",
        headers=httpx.Headers(),
    )
    method = _patch_qdrant_client(monkeypatch, side_effect=[server_5xx, None])

    QdrantResource().upsert_points("c", [_qdrant_point()])

    assert method.call_count == 2


def test_qdrant_fails_fast_on_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """401/403 is auth misconfig — should not consume the retry budget."""
    auth_fail = UnexpectedResponse(
        status_code=401,
        reason_phrase="unauthorized",
        content=b"",
        headers=httpx.Headers(),
    )
    method = _patch_qdrant_client(monkeypatch, side_effect=auth_fail)

    with pytest.raises(UnexpectedResponse):
        QdrantResource().upsert_points("c", [_qdrant_point()])

    assert method.call_count == 1


def test_qdrant_fails_fast_on_arbitrary_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anything outside the transient set re-raises on first attempt."""
    method = _patch_qdrant_client(monkeypatch, side_effect=RuntimeError("oops"))

    with pytest.raises(RuntimeError, match="oops"):
        QdrantResource().upsert_points("c", [_qdrant_point()])

    assert method.call_count == 1


def test_qdrant_exhausts_retries_on_persistent_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistent = ResponseHandlingException(httpx.ConnectError("down"))
    method = _patch_qdrant_client(monkeypatch, side_effect=persistent)

    with pytest.raises(RuntimeError, match="Qdrant upsert failed"):
        QdrantResource().upsert_points("c", [_qdrant_point()])

    assert method.call_count == 3


def test_qdrant_empty_points_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """No points → don't even build the client. Preserves prior behavior."""
    called = False

    def record_construction(_self: object) -> object:
        nonlocal called
        called = True
        return MagicMock()

    monkeypatch.setattr(QdrantResource, "_client", record_construction)

    QdrantResource().upsert_points("c", [])

    assert called is False
