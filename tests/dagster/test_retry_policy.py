"""Unit tests for the QNT-110 deploy-window retry configuration.

Two layers to verify:
1. Op-level RetryPolicy attached to sensor-triggered jobs (in-run retry)
2. Run-level tags attached to all auto-triggered jobs (re-launch on failure)
"""

from __future__ import annotations

from dagster_pipelines.retry import DEPLOY_WINDOW_RETRY, DEPLOY_WINDOW_RUN_RETRY_TAGS
from dagster_pipelines.schedules import fundamentals_weekly_job, ohlcv_daily_job
from dagster_pipelines.sensors import fundamentals_downstream_job, ohlcv_downstream_job


def test_deploy_window_retry_shape() -> None:
    """RetryPolicy parameters match the ticket spec: 3 retries, 30s exp backoff."""
    assert DEPLOY_WINDOW_RETRY.max_retries == 3
    assert DEPLOY_WINDOW_RETRY.delay == 30
    assert DEPLOY_WINDOW_RETRY.backoff is not None  # Backoff.EXPONENTIAL
    assert DEPLOY_WINDOW_RETRY.jitter is not None  # Jitter.PLUS_MINUS


def test_sensor_jobs_have_op_retry_policy() -> None:
    """Op-level retry — scoped to sensor jobs per QNT-110 ticket."""
    for job in (ohlcv_downstream_job, fundamentals_downstream_job):
        policy = job.op_retry_policy
        assert policy is not None, f"{job.name} missing op_retry_policy"
        assert policy.max_retries == 3
        assert policy.delay == 30


def test_sensor_jobs_have_run_retry_tags() -> None:
    """Run-level retry — sensor jobs re-launch on launch-time failures."""
    for job in (ohlcv_downstream_job, fundamentals_downstream_job):
        tags = job.tags or {}
        assert tags.get("dagster/max_retries") == "3", f"{job.name} missing run-retry tag"


def test_schedule_jobs_have_run_retry_tags() -> None:
    """Run-level retry — schedule jobs ALSO re-launch on launch-time failures
    (same deploy-window gRPC UNAVAILABLE class as sensor jobs)."""
    for job in (ohlcv_daily_job, fundamentals_weekly_job):
        tags = job.tags or {}
        assert tags.get("dagster/max_retries") == "3", f"{job.name} missing run-retry tag"


def test_deploy_window_run_retry_tags_constant_shape() -> None:
    """Tag dict is well-formed so jobs picking up the constant stay consistent."""
    assert DEPLOY_WINDOW_RUN_RETRY_TAGS == {"dagster/max_retries": "3"}
