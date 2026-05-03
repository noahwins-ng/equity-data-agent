"""Tests for Sentry init shape + the cors_aware_exception_handler capture
forward + the /api/v1/_debug/sentry verification endpoint (QNT-86).

The init itself is module-level (runs at import), so we re-run the exact
init block under a controlled fake ``sentry_sdk`` to assert the kwargs
without booting an actual Sentry client. The debug endpoint exercises the
full request → exception → capture path through the FastAPI app.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from unittest.mock import MagicMock, patch

import pytest
from api import main as main_module
from api import security as security_module
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterable[TestClient]:
    # ``raise_server_exceptions=False`` lets the cors_aware_exception_handler
    # actually run and produce its 500 — by default, Starlette's TestClient
    # re-raises any unhandled exception and bypasses the registered handler,
    # which would defeat the AC under test (we're verifying the handler
    # forwards to Sentry).
    with TestClient(main_module.app, raise_server_exceptions=False) as c:
        yield c


# ─── Init kwarg shape ───────────────────────────────────────────────────────


@pytest.fixture
def _restore_main(monkeypatch: pytest.MonkeyPatch) -> Iterable[None]:
    """Reload api.main back to its original state after a test that itself
    reloaded the module (so the fake sentry_sdk + monkeypatched settings
    don't bleed into later tests via the cached module).
    """
    yield
    monkeypatch.undo()
    importlib.reload(main_module)


def _reload_main_with_fake_sdk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    git_sha: str,
    env: str = "prod",
    dsn: str = "https://test@example/1",
) -> MagicMock:
    """Re-import api.main with a fake sentry_sdk so the module-level init
    block runs against the mock. Returns the fake SDK for assertions.

    All config (DSN, ENV, GIT_SHA) is monkeypatched on ``settings`` rather
    than on ``os.environ`` — pydantic-settings parses env vars at module
    import time, so direct attribute monkeypatching is the only reliable
    way to exercise different combinations within a single test session.
    """
    fake_sdk = MagicMock()
    monkeypatch.setattr(security_module.settings, "SENTRY_DSN", dsn)
    monkeypatch.setattr(security_module.settings, "ENV", env)
    monkeypatch.setattr(security_module.settings, "GIT_SHA", git_sha)
    with patch.dict("sys.modules", {"sentry_sdk": fake_sdk}):
        importlib.reload(main_module)
    return fake_sdk


def test_sentry_init_passes_release_environment_and_sample_rate(
    monkeypatch: pytest.MonkeyPatch,
    _restore_main: None,
) -> None:
    """The init kwargs must carry: dsn, environment=settings.ENV, release=GIT_SHA,
    traces_sample_rate=0.1, auto_session_tracking=True, send_default_pii=False.
    Each of these is an AC datapoint — drift here breaks the dashboard or
    leaks PII."""
    fake_sdk = _reload_main_with_fake_sdk(
        monkeypatch,
        git_sha="abc1234deadbeef",
        env="prod",
    )
    fake_sdk.init.assert_called_once()
    kwargs = fake_sdk.init.call_args.kwargs
    assert kwargs["dsn"] == "https://test@example/1"
    assert kwargs["environment"] == "prod"
    assert kwargs["release"] == "abc1234deadbeef"
    assert kwargs["traces_sample_rate"] == 0.1
    assert kwargs["auto_session_tracking"] is True
    # Default PII scrubbing — no IPs, cookies, or auth headers in events.
    assert kwargs["send_default_pii"] is False


def test_sentry_init_release_is_none_when_git_sha_unset(
    monkeypatch: pytest.MonkeyPatch,
    _restore_main: None,
) -> None:
    """Local dev runs with ``GIT_SHA`` unset must not pass an empty-string
    release — Sentry rejects empty release values. ``None`` lets the SDK
    fall back to its release-detection or omit the tag entirely."""
    fake_sdk = _reload_main_with_fake_sdk(
        monkeypatch,
        git_sha="",
        env="dev",
    )
    fake_sdk.init.assert_called_once()
    assert fake_sdk.init.call_args.kwargs["release"] is None


def test_sentry_init_skipped_when_dsn_unset(
    monkeypatch: pytest.MonkeyPatch,
    _restore_main: None,
) -> None:
    """No DSN → no init call at all. Dev runs must not ship anything anywhere."""
    fake_sdk = _reload_main_with_fake_sdk(monkeypatch, git_sha="x", env="dev", dsn="")
    fake_sdk.init.assert_not_called()


# ─── /api/v1/_debug/sentry verification endpoint ────────────────────────────


def test_debug_sentry_raises_in_dev(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In dev (``ENV != "prod"``) the endpoint always raises so a developer
    can verify Sentry wiring locally. The cors_aware_exception_handler
    converts the RuntimeError to a 500 — the test asserts the 500 lands
    AND the exception is forwarded to Sentry."""
    monkeypatch.setattr(security_module.settings, "ENV", "dev")
    monkeypatch.setattr(security_module.settings, "SENTRY_DSN", "https://test@example/1")
    fake_sdk = MagicMock()
    with patch.dict("sys.modules", {"sentry_sdk": fake_sdk}):
        r = client.get("/api/v1/_debug/sentry")
    assert r.status_code == 500
    assert r.json() == {"detail": "Internal Server Error"}
    # The exception forwarded to Sentry is our synthetic RuntimeError.
    fake_sdk.capture_exception.assert_called_once()
    captured = fake_sdk.capture_exception.call_args.args[0]
    assert isinstance(captured, RuntimeError)
    assert "QNT-86" in str(captured)


def test_debug_sentry_returns_404_in_prod_without_override(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In prod the endpoint must NOT be reachable by default — a scraper
    looping on ``/api/v1/_debug/sentry`` would burn the Sentry monthly
    quota. Gating returns 404 so the path looks unregistered."""
    monkeypatch.setattr(security_module.settings, "ENV", "prod")
    monkeypatch.setattr(security_module.settings, "ENABLE_SENTRY_TEST", False)
    r = client.get("/api/v1/_debug/sentry")
    assert r.status_code == 404


def test_debug_sentry_raises_in_prod_with_override(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One-shot prod verification: setting ``ENABLE_SENTRY_TEST=1`` for a
    single deploy lets the operator force a Sentry event, then the env var
    is unset to relock the endpoint."""
    monkeypatch.setattr(security_module.settings, "ENV", "prod")
    monkeypatch.setattr(security_module.settings, "ENABLE_SENTRY_TEST", True)
    monkeypatch.setattr(security_module.settings, "SENTRY_DSN", "https://test@example/1")
    fake_sdk = MagicMock()
    with patch.dict("sys.modules", {"sentry_sdk": fake_sdk}):
        r = client.get("/api/v1/_debug/sentry")
    assert r.status_code == 500
    fake_sdk.capture_exception.assert_called_once()


# Note: the cors_aware_exception_handler forward is exercised end-to-end by
# ``test_debug_sentry_raises_in_dev`` above — the synthetic RuntimeError from
# /api/v1/_debug/sentry is the same code path. Adding a second test that
# registered an extra route on the shared ``main_module.app`` would leak a
# permanent /_test/* route into every later test in the session, so that
# scenario was deliberately collapsed into the debug-endpoint test.
