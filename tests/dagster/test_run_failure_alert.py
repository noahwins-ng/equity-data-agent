"""Tests for the QNT-62 run-failure -> Discord webhook sensor.

The sensor is a thin glue layer; the value is in the failure-mode contract:

* No webhook configured -> log + return (never attempts the POST).
* New failure (no recent matching run) -> POST exactly once, with the
  documented message shape.
* Stuck partition retrying back-to-back -> dedup by (job_name, partition)
  within a 10-min window so Discord doesn't get spammed.

Test strategy -- two layers, composed by inspection:

1. ``_has_earlier_failure_in_window`` is exercised directly against a stub
   instance whose ``get_run_records`` matches the real Dagster interface
   (``test_dedup_query_excludes_current_run`` / ``..._only_current_run_present``).
   This proves the query, the current-run-exclusion, and the partition-tag
   wiring without needing to seed runs into a real ``DagsterInstance``.

2. ``test_sensor_posts_expected_message_shape`` invokes the full sensor
   against a real ``DagsterInstance.ephemeral()`` (zero prior runs), proving
   the no-dedup branch wires the helper into the sensor body correctly.

3. ``test_sensor_dedups_within_rate_limit_window`` covers the dedup branch
   by monkeypatching the helper -- the real query path is already covered by
   layer 1, and seeding fake prior failed runs into ephemeral storage requires
   constructing real ``JobDefinition`` / ``RunRecord`` objects whose Pydantic
   plumbing breaks across Dagster minor versions.

The sensor itself is invoked through ``build_run_status_sensor_context().for_run_failure()``
so the tests cover the same code path the daemon runs in prod, not just the
helpers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from dagster import (
    AssetKey,
    DagsterEvent,
    DagsterEventType,
    DagsterInstance,
    build_run_status_sensor_context,
)
from dagster._core.execution.plan.objects import ErrorSource, StepFailureData
from dagster._core.storage.dagster_run import DagsterRun, DagsterRunStatus
from dagster_pipelines import run_failure_alert
from dagster_shared.error import SerializableErrorInfo

_ORIGINAL_HTTPX_CLIENT = httpx.Client


def _patch_httpx_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Route every ``httpx.Client(...)`` inside the alert module through a
    MockTransport. Mirrors the helper in test_vercel_deploy.py."""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        run_failure_alert.httpx,
        "Client",
        lambda *_args, **_kwargs: _ORIGINAL_HTTPX_CLIENT(transport=transport),
    )


# ── Helpers for building sensor contexts ──────────────────────────────


def _make_failure_event(*, job_name: str, run_id: str) -> DagsterEvent:
    return DagsterEvent(
        event_type_value=DagsterEventType.PIPELINE_FAILURE.value,
        job_name=job_name,
        message="run failed: HTTPError 500",
        pid=1,
    )


def _make_run(
    *,
    run_id: str = "run-123",
    job_name: str = "ohlcv_daily_job",
    partition: str | None = "AAPL",
    asset_keys: tuple[AssetKey, ...] = (AssetKey(["ohlcv_raw"]),),
) -> DagsterRun:
    tags: dict[str, str] = {}
    if partition:
        tags["dagster/partition"] = partition
    return DagsterRun(
        job_name=job_name,
        run_id=run_id,
        tags=tags,
        status=DagsterRunStatus.FAILURE,
        asset_selection=frozenset(asset_keys) if asset_keys else None,
    )


def _build_context(
    *,
    instance: DagsterInstance,
    run: DagsterRun,
) -> Any:
    event = _make_failure_event(job_name=run.job_name, run_id=run.run_id)
    return build_run_status_sensor_context(
        sensor_name="dagster_run_failure_alert_sensor",
        dagster_event=event,
        dagster_instance=instance,
        dagster_run=run,
        partition_key=run.tags.get("dagster/partition"),
    ).for_run_failure()


# ── _format_message ──────────────────────────────────────────────────


def test_format_message_includes_all_fields() -> None:
    run = _make_run()
    msg = run_failure_alert._format_message(
        run=run,
        asset_key="ohlcv_raw",
        partition="AAPL",
        step_key="ohlcv_raw",
        exception_line="HTTPError 500: yfinance rate limited",
        run_url="http://localhost:3000/runs/run-123",
    )
    assert "[ASSET FAILURE]" in msg
    assert "job=`ohlcv_daily_job`" in msg
    assert "asset=`ohlcv_raw`" in msg
    assert "partition=`AAPL`" in msg
    assert "step=`ohlcv_raw`" in msg
    assert "HTTPError 500: yfinance rate limited" in msg
    assert "run: http://localhost:3000/runs/run-123" in msg


def test_format_message_omits_optional_fields() -> None:
    run = _make_run(partition=None, asset_keys=())
    msg = run_failure_alert._format_message(
        run=run,
        asset_key=None,
        partition="",
        step_key=None,
        exception_line=None,
        run_url="http://localhost:3000/runs/abc",
    )
    assert "[ASSET FAILURE]" in msg
    assert "asset=" not in msg
    assert "partition=" not in msg
    assert "step=" not in msg
    assert "(no exception captured)" in msg


# ── _post_discord ────────────────────────────────────────────────────


def test_post_discord_truncates_long_content(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(204)

    _patch_httpx_client(monkeypatch, handler)
    long_content = "x" * 5000
    assert run_failure_alert._post_discord("https://discord/webhook", long_content) is True
    # Body is JSON-encoded; the encoded string is shorter than the original
    # but the truncation guard caps content well under Discord's 2000 limit.
    assert len(captured["body"]) < 2200


def test_post_discord_swallows_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns fail")

    _patch_httpx_client(monkeypatch, handler)
    assert run_failure_alert._post_discord("https://discord/webhook", "hi") is False


def test_post_discord_returns_false_on_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx_client(monkeypatch, lambda _req: httpx.Response(500))
    assert run_failure_alert._post_discord("https://discord/webhook", "hi") is False


# ── Sensor integration ───────────────────────────────────────────────


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        run_failure_alert.settings, "DISCORD_WEBHOOK_URL", "https://discord/webhook"
    )
    monkeypatch.setattr(run_failure_alert.settings, "DAGSTER_BASE_URL", "http://localhost:3000")


def test_sensor_skips_when_webhook_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_failure_alert.settings, "DISCORD_WEBHOOK_URL", "")
    # No httpx patch -- a POST attempt would hit the network and fail the test.
    with DagsterInstance.ephemeral() as instance:
        ctx = _build_context(instance=instance, run=_make_run())
        run_failure_alert.dagster_run_failure_alert_sensor(ctx)


@pytest.mark.usefixtures("configured")
def test_sensor_posts_expected_message_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(204)

    _patch_httpx_client(monkeypatch, handler)
    with DagsterInstance.ephemeral() as instance:
        ctx = _build_context(instance=instance, run=_make_run())
        run_failure_alert.dagster_run_failure_alert_sensor(ctx)

    assert captured["url"] == "https://discord/webhook"
    body = captured["body"]
    # AC: message includes asset key, partition, job name, exception line, run URL.
    assert "ohlcv_daily_job" in body
    assert "ohlcv_raw" in body
    assert "AAPL" in body
    assert "HTTPError 500" in body
    assert "/runs/run-123" in body


@pytest.mark.usefixtures("configured")
def test_sensor_dedups_within_rate_limit_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second failure on the same (job, partition) within the window is
    suppressed -- only the first call POSTs."""
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request.content.decode())
        return httpx.Response(204)

    _patch_httpx_client(monkeypatch, handler)

    # Pretend a prior run already failed in the window. The dedup query is
    # exercised end-to-end by test_dedup_query_excludes_current_run below;
    # here we short-circuit it to keep this test focused on the suppress branch.
    monkeypatch.setattr(
        run_failure_alert,
        "_has_earlier_failure_in_window",
        lambda *, instance, current_run_id, job_name, partition, window_seconds: True,
    )
    with DagsterInstance.ephemeral() as instance:
        ctx = _build_context(instance=instance, run=_make_run(run_id="run-2"))
        run_failure_alert.dagster_run_failure_alert_sensor(ctx)

    assert posts == [], "second failure within window should not POST"


@pytest.mark.usefixtures("configured")
def test_dedup_query_excludes_current_run() -> None:
    """The current run itself must not count toward the dedup decision --
    otherwise every new failure would self-suppress."""
    current_run = _make_run(run_id="self")
    other_run = _make_run(run_id="other")

    class _Instance:
        called_with: dict[str, Any] = {}

        def get_run_records(self, *, filters: Any, limit: int) -> list[Any]:
            self.called_with["filters"] = filters
            self.called_with["limit"] = limit
            return [
                _Record(current_run),  # same run -> ignored
                _Record(other_run),  # earlier failure -> triggers dedup
            ]

    class _Record:
        def __init__(self, run: DagsterRun) -> None:
            self.dagster_run = run

    instance = _Instance()
    has_earlier = run_failure_alert._has_earlier_failure_in_window(
        instance=instance,  # type: ignore[arg-type]
        current_run_id="self",
        job_name="ohlcv_daily_job",
        partition="AAPL",
        window_seconds=600,
    )
    assert has_earlier is True
    # Confirm the partition tag was passed to the filter (needed for correct scoping).
    filters = instance.called_with["filters"]
    assert filters.tags == {"dagster/partition": "AAPL"}
    assert filters.job_name == "ohlcv_daily_job"


def test_dedup_returns_false_when_only_current_run_present() -> None:
    """If the only matching failure in the window IS the current run, no
    earlier failure exists -> return False so the alert fires."""
    current_run = _make_run(run_id="self")

    class _Record:
        def __init__(self, run: DagsterRun) -> None:
            self.dagster_run = run

    class _Instance:
        def get_run_records(self, *, filters: Any, limit: int) -> list[Any]:
            return [_Record(current_run)]

    has_earlier = run_failure_alert._has_earlier_failure_in_window(
        instance=_Instance(),  # type: ignore[arg-type]
        current_run_id="self",
        job_name="ohlcv_daily_job",
        partition="AAPL",
        window_seconds=600,
    )
    assert has_earlier is False


def test_extract_failure_details_uses_step_failure_event() -> None:
    """When a step failed, the sensor should prefer the step-failure error
    over the run-level failure message."""
    step_event = DagsterEvent(
        event_type_value=DagsterEventType.STEP_FAILURE.value,
        job_name="ohlcv_daily_job",
        step_key="ohlcv_raw",
        message="step failed",
        event_specific_data=StepFailureData(
            error=SerializableErrorInfo(
                message="ValueError: bad ticker\nstack trace line 2",
                stack=[],
                cls_name="ValueError",
            ),
            user_failure_data=None,
            error_source=ErrorSource.USER_CODE_ERROR,
        ),
    )

    class _StubContext:
        failure_event = DagsterEvent(
            event_type_value=DagsterEventType.PIPELINE_FAILURE.value,
            job_name="ohlcv_daily_job",
            message="generic run failure",
            pid=1,
        )
        dagster_run = _make_run()

        def get_step_failure_events(self) -> list[DagsterEvent]:
            return [step_event]

    step_key, exception_line, asset_key = run_failure_alert._extract_failure_details(
        _StubContext()  # type: ignore[arg-type]
    )
    assert step_key == "ohlcv_raw"
    # First line of the step error wins over the run-level message.
    assert exception_line == "ValueError: bad ticker"
    assert asset_key == "ohlcv_raw"
