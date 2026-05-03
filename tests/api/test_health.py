"""Tests for /health and /api/v1/health.

Covers the deploy-identity contract added in QNT-51:
- 200 when ClickHouse is reachable (even if Qdrant is not)
- 503 when ClickHouse is unreachable
- ``deploy.git_sha`` comes from the GIT_SHA env var
- ``deploy.dagster_assets`` / ``dagster_checks`` are integers
- Both paths return identical payloads so monitoring keeps working
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from api import clickhouse as clickhouse_module
from api import main as main_module
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterable[TestClient]:
    """Fresh TestClient per test. The CH client is cached via lru_cache so we
    clear it here; ``_dagster_counts`` is replaced whole by ``_stub`` in each
    test (via monkeypatch), so its cache state is irrelevant."""
    clickhouse_module.get_client.cache_clear()
    with TestClient(main_module.app) as c:
        yield c
    clickhouse_module.get_client.cache_clear()


@pytest.fixture(autouse=True)
def _fixed_git_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module.settings, "GIT_SHA", "test-sha-abc1234")


def _stub(monkeypatch: pytest.MonkeyPatch, *, ch_ok: bool, qdrant_ok: bool) -> None:
    monkeypatch.setattr(main_module, "_check_clickhouse", lambda: "ok" if ch_ok else "down")
    monkeypatch.setattr(main_module, "_check_qdrant", lambda: "ok" if qdrant_ok else "down")
    monkeypatch.setattr(main_module, "_dagster_counts", lambda: (8, 17))
    # Pin next_ingest_local so tests don't depend on real schedule introspection
    # (and so tests pass the same regardless of when they run vs. the cron).
    monkeypatch.setattr(main_module, "_next_ingest_local", lambda: "17:00 ET")


def test_health_ok_when_both_services_up(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, ch_ok=True, qdrant_ok=True)
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["services"] == {"clickhouse": "ok", "qdrant": "ok"}


def test_health_degraded_when_qdrant_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, ch_ok=True, qdrant_ok=False)
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["services"]["clickhouse"] == "ok"
    assert body["services"]["qdrant"] == "down"


def test_health_503_when_clickhouse_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, ch_ok=False, qdrant_ok=False)
    r = client.get("/api/v1/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "down"
    assert body["services"]["clickhouse"] == "down"


def test_health_payload_includes_deploy_identity(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, ch_ok=True, qdrant_ok=True)
    r = client.get("/api/v1/health")
    deploy = r.json()["deploy"]
    assert deploy["git_sha"] == "test-sha-abc1234"
    # Counts come from the actual dagster_pipelines definitions module;
    # assert they are sane integers (the CD hard gate (QNT-89) asserts minimums).
    assert isinstance(deploy["dagster_assets"], int) and deploy["dagster_assets"] >= 0
    assert isinstance(deploy["dagster_checks"], int) and deploy["dagster_checks"] >= 0


def test_git_sha_falls_back_to_unknown_without_env(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module.settings, "GIT_SHA", "")
    _stub(monkeypatch, ch_ok=True, qdrant_ok=True)
    r = client.get("/api/v1/health")
    assert r.json()["deploy"]["git_sha"] == "unknown"


def test_health_payload_includes_provenance_block(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QNT-132: bottom-strip subsystem provenance for the data-driven UI.

    Asserts the contract the frontend relies on. ``sources`` is the visible
    vendor list, ``jobs.next_ingest_local`` is a string the strip renders
    verbatim. ``runtime`` and ``schedule`` are static labels for the strip.
    """
    _stub(monkeypatch, ch_ok=True, qdrant_ok=True)
    r = client.get("/api/v1/health")
    body = r.json()
    prov = body["provenance"]
    assert prov["sources"] == ["yfinance", "Finnhub", "Qdrant"]
    jobs = prov["jobs"]
    assert jobs["runtime"] == "Dagster"
    assert jobs["schedule"] == "daily"
    assert isinstance(jobs["next_ingest_local"], str) and jobs["next_ingest_local"]


def test_provenance_sources_track_settings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vendor swap (e.g., yfinance → Polygon) updates settings; the strip
    re-renders without a frontend deploy. Single-source-of-truth check."""
    _stub(monkeypatch, ch_ok=True, qdrant_ok=True)
    monkeypatch.setattr(
        main_module.settings, "PROVENANCE_SOURCES", ["Polygon", "Finnhub", "Qdrant"]
    )
    r = client.get("/api/v1/health")
    assert r.json()["provenance"]["sources"] == ["Polygon", "Finnhub", "Qdrant"]


def test_provenance_next_ingest_local_falls_back_when_introspection_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Dagster schedule introspection fails (e.g., a future api-only image
    without dagster_pipelines), the field surfaces the static settings fallback
    instead of taking /health down."""
    # Don't use ``_stub`` here — it would shadow ``_next_ingest_local`` and we
    # specifically want to exercise the real fallback path through it.
    monkeypatch.setattr(main_module, "_check_clickhouse", lambda: "ok")
    monkeypatch.setattr(main_module, "_check_qdrant", lambda: "ok")
    monkeypatch.setattr(main_module, "_dagster_counts", lambda: (8, 17))
    main_module._ohlcv_schedule_cron_tz.cache_clear()
    monkeypatch.setattr(main_module, "_ohlcv_schedule_cron_tz", lambda: None)
    monkeypatch.setattr(main_module.settings, "PROVENANCE_NEXT_INGEST_FALLBACK", "FALLBACK")
    r = client.get("/api/v1/health")
    assert r.json()["provenance"]["jobs"]["next_ingest_local"] == "FALLBACK"


def test_provenance_next_ingest_local_real_schedule_format() -> None:
    """Real schedule introspection (no monkeypatch). The current
    ``ohlcv_daily_schedule`` cron is ``0 17 * * 1-5`` ET, so the output is
    pinned to ``17:00 ET`` — changing the schedule changes this assertion,
    which is the point: ONE place owns the value (QNT-132)."""
    main_module._ohlcv_schedule_cron_tz.cache_clear()
    assert main_module._next_ingest_local() == "17:00 ET"


def test_provenance_next_ingest_local_tracks_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching the schedule's ``execution_timezone`` must change the suffix.

    Guards the AC-1 invariant (ONE place owns the value) — the suffix is
    derived from the schedule's tz, not hardcoded. If a future change pins
    the suffix back to a string literal, this test fails."""
    main_module._ohlcv_schedule_cron_tz.cache_clear()
    monkeypatch.setattr(
        main_module, "_ohlcv_schedule_cron_tz", lambda: ("0 17 * * 1-5", "America/Los_Angeles")
    )
    assert main_module._next_ingest_local() == "17:00 PT"


def test_legacy_health_path_matches_v1_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, ch_ok=True, qdrant_ok=True)
    v1 = client.get("/api/v1/health")
    legacy = client.get("/health")
    assert v1.status_code == legacy.status_code == 200
    assert v1.json() == legacy.json()


def test_legacy_health_still_503_on_clickhouse_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Prod monitoring (scripts/health-monitor.sh, make check-prod) relies on the
    # legacy /health path returning 503 when CH is unreachable. Don't regress it.
    _stub(monkeypatch, ch_ok=False, qdrant_ok=False)
    r = client.get("/health")
    assert r.status_code == 503


def test_head_v1_health_200_and_empty_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HEAD must return the same status as GET with an empty body.

    UptimeRobot free tier only supports HEAD probes — see QNT-106. Keeping
    this test live guards against a regression that would silently break
    the prod uptime monitor.
    """
    _stub(monkeypatch, ch_ok=True, qdrant_ok=True)
    r = client.head("/api/v1/health")
    assert r.status_code == 200
    assert r.content == b""


def test_head_v1_health_503_when_clickhouse_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, ch_ok=False, qdrant_ok=False)
    r = client.head("/api/v1/health")
    assert r.status_code == 503
    assert r.content == b""


def test_head_legacy_health_matches_v1(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, ch_ok=True, qdrant_ok=True)
    v1 = client.head("/api/v1/health")
    legacy = client.head("/health")
    assert v1.status_code == legacy.status_code == 200
    assert v1.content == legacy.content == b""


def test_legacy_health_not_in_openapi_schema(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/v1/health" in paths
    assert "/health" not in paths  # intentionally hidden — legacy alias only


def test_openapi_metadata_populated(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    info = schema["info"]
    assert info["title"] == "Equity Data Agent API"
    assert info["version"] == "0.1.0"
    assert info["description"]  # non-empty


def test_cors_allows_localhost_3001(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, ch_ok=True, qdrant_ok=True)
    r = client.get("/api/v1/health", headers={"Origin": "http://localhost:3001"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:3001"


def test_cors_disallows_unrelated_vercel_project(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-161: the prior open ``*.vercel.app`` regex was tightened to a
    project-pinned origin set so a leaked preview URL for an unrelated
    Vercel project can't drive traffic to this API. Default test config
    has no regex set, so any vercel.app origin is rejected here. The
    project-pinned regex behaviour is covered by ``test_security.py``
    which constructs a fresh app with the regex set.
    """
    _stub(monkeypatch, ch_ok=True, qdrant_ok=True)
    r = client.get(
        "/api/v1/health",
        headers={"Origin": "https://equity-data-agent-git-feature.vercel.app"},
    )
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
