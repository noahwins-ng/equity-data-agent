"""Unit tests for the asset_checks module.

Validates registration (visible in Definitions) and blocking semantics
(AC: "Failed check blocks downstream computation (configurable severity)").
Behavioural tests that query ClickHouse belong in integration tests.
"""

from __future__ import annotations

from dagster_pipelines.asset_checks import ALL_ASSET_CHECKS
from dagster_pipelines.definitions import defs


def test_all_asset_checks_registered_in_definitions() -> None:
    """Every check in ALL_ASSET_CHECKS should appear in the Definitions object
    so it's visible in the Dagster UI alongside its asset."""
    defs_check_keys = {
        spec.key for check_def in defs.asset_checks or [] for spec in check_def.check_specs
    }
    for check_def in ALL_ASSET_CHECKS:
        for spec in check_def.check_specs:
            assert spec.key in defs_check_keys, f"{spec.key} missing from Definitions"


def test_blocking_checks_are_marked_blocking() -> None:
    """Integrity checks (row-count, NULL close, future dates, period_type) should
    be blocking so they halt downstream materialization if violated."""
    expected_blocking = {
        "ohlcv_raw_has_rows",
        "ohlcv_raw_no_null_close",
        "ohlcv_raw_no_future_dates",
        "fundamentals_has_rows",
        "fundamentals_period_type_valid",
    }
    actual_blocking = {
        spec.name
        for check_def in ALL_ASSET_CHECKS
        for spec in check_def.check_specs
        if spec.blocking
    }
    assert actual_blocking == expected_blocking


def test_expected_asset_check_count() -> None:
    """All 4 target assets have at least one check registered."""
    asset_keys_with_checks = {
        spec.asset_key.to_user_string()
        for check_def in ALL_ASSET_CHECKS
        for spec in check_def.check_specs
    }
    assert "ohlcv_raw" in asset_keys_with_checks
    assert "fundamentals" in asset_keys_with_checks
    assert "technical_indicators_daily" in asset_keys_with_checks
    assert "technical_indicators_weekly" in asset_keys_with_checks
    assert "technical_indicators_monthly" in asset_keys_with_checks
    assert "fundamental_summary" in asset_keys_with_checks
    assert "news_raw" in asset_keys_with_checks
    assert "news_embeddings" in asset_keys_with_checks
