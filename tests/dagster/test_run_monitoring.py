"""Tests for the QNT-114 run_monitoring + tag-concurrency configuration in
``dagster.yaml``.

These are config-shape tests: they prove that ``dagster.yaml`` parses with the
exact settings the runbook and ops docs reference. The *behavioural* chaos
test — SIGKILL a run-worker, confirm the run auto-fails in ~5 min — runs
against live Dagster (local ``make dev-dagster`` or prod) per the commands
documented in ``docs/guides/ops-runbook.md`` under "CANCELING ghost after
run-worker OOM". Config-shape alone can't prove run_monitoring runs; the
runbook test proves the end-to-end path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from dagster._core.instance.config import dagster_instance_config

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def parsed_config() -> dict[str, Any]:
    config, _ = dagster_instance_config(base_dir=str(REPO_ROOT), config_filename="dagster.yaml")
    return cast(dict[str, Any], config)


def test_run_monitoring_enabled_with_expected_timeouts(parsed_config: dict[str, Any]) -> None:
    rm = parsed_config["run_monitoring"]
    assert rm["enabled"] is True
    # These numbers shape the runbook's stated recovery window. If they change,
    # the runbook entry under "CANCELING ghost after run-worker OOM" must change
    # too. max_runtime_seconds is the timeout fallback — on DefaultRunLauncher
    # it's the only real orphan-detection signal (the per-worker health check
    # path is not supported for our launcher). See dagster.yaml for the full
    # rationale and the launcher-switch follow-up.
    assert rm["poll_interval_seconds"] == 120
    assert rm["start_timeout_seconds"] == 180
    assert rm["cancel_timeout_seconds"] == 180
    assert rm["max_runtime_seconds"] == 1800


def test_run_monitoring_fails_orphans_instead_of_resuming(
    parsed_config: dict[str, Any],
) -> None:
    """``max_resume_run_attempts: 0`` is load-bearing — resuming a run whose
    worker was OOM-killed would just re-OOM the same cgroup. Orphans must
    fail loudly so the alerting path fires."""
    assert parsed_config["run_monitoring"]["max_resume_run_attempts"] == 0


def test_tag_concurrency_reserves_slot_for_non_backfill_work(
    parsed_config: dict[str, Any],
) -> None:
    """With ``max_concurrent_runs=3`` and ``dagster/backfill`` capped at 2,
    at least one slot is always available for sensor-triggered runs."""
    coord = parsed_config["run_coordinator"]["config"]
    assert coord["max_concurrent_runs"] == 3
    limits = coord["tag_concurrency_limits"]
    backfill_limits = [x for x in limits if x["key"] == "dagster/backfill"]
    assert backfill_limits == [{"key": "dagster/backfill", "limit": 2}]
