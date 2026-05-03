"""Tests for the QNT-168 Vercel Deploy Hook trigger op.

The op is a thin wrapper around an HTTP POST -- the value is in the
failure-mode contract:

* Missing config (VERCEL_DEPLOY_HOOK_URL unset) -> log info, return.
* Non-2xx response -> log warning at run level, op completes successfully.
* HTTP error (timeout, connect fail) -> log warning, op completes successfully.

These tests pin those branches because a future ``raise`` from the op
would surface a missed deploy as a failed Dagster run, which adds noise
without being more actionable than a warning (the next scheduled tick
will retry, the prior deploy is still serving).
"""

from collections.abc import Callable

import httpx
import pytest
from dagster import build_op_context
from dagster_pipelines import vercel_deploy

_ORIGINAL_HTTPX_CLIENT = httpx.Client


def _patch_httpx_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Route every ``httpx.Client(...)`` inside the ``vercel_deploy`` module
    through a MockTransport. Captures the original constructor at import
    time so the patched name does not recurse into itself."""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        vercel_deploy.httpx,
        "Client",
        lambda *_args, **_kwargs: _ORIGINAL_HTTPX_CLIENT(transport=transport),
    )


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the hook URL so the op actually attempts the POST."""
    monkeypatch.setattr(
        vercel_deploy.settings,
        "VERCEL_DEPLOY_HOOK_URL",
        "https://api.vercel.com/v1/deploy-hook/test-token",
    )


def test_skips_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vercel_deploy.settings, "VERCEL_DEPLOY_HOOK_URL", "")
    # No httpx patch -- if the op tried to POST, it would hit the network
    # and the test would fail. Successful return == nothing was attempted.
    vercel_deploy.trigger_vercel_deploy(build_op_context())


@pytest.mark.usefixtures("configured")
def test_success_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        return httpx.Response(201, json={"job": {"id": "deploy-abc"}})

    _patch_httpx_client(monkeypatch, handler)
    vercel_deploy.trigger_vercel_deploy(build_op_context())

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/test-token")


@pytest.mark.usefixtures("configured")
def test_swallows_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx_client(monkeypatch, lambda _req: httpx.Response(500, text="vercel oopsie"))
    # Op must NOT raise; the run completes and the next schedule retries.
    vercel_deploy.trigger_vercel_deploy(build_op_context())


@pytest.mark.usefixtures("configured")
def test_swallows_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns fail")

    _patch_httpx_client(monkeypatch, handler)
    vercel_deploy.trigger_vercel_deploy(build_op_context())
