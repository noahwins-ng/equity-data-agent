"""Unit tests for agent.post_checks (QNT-193).

Tests the check_verdict_direction function and the rolling-window
mismatch escalation logic. No Langfuse client, no Discord POST.
"""

from __future__ import annotations

import time
from unittest.mock import patch

from agent.post_checks import (
    _ESCALATE_THRESHOLD,
    _mismatch_timestamps,
    check_verdict_direction,
    record_mismatch,
)
from agent.thesis import Thesis

# ── Fixtures ──────────────────────────────────────────────────────────────────

_TECHNICAL_REPORT_225 = """\
# TECHNICAL REPORT — NVDA
As of 2026-05-19 (Semiconductors, Information Technology)

## PRICE ACTION
Close: 225.32 (+1.2% daily)
Trend: above SMA-20 (210.30) and SMA-50 (193.07)

## MOMENTUM
RSI-14: 71.6 -- overbought (threshold: 70 overbought / 30 oversold)
"""


def _thesis(verdict_action: str) -> Thesis:
    return Thesis(
        setup="NVDA setup paragraph.",
        bull_case=["Momentum intact (source: technical)"],
        bear_case=["Overbought RSI pulling back (source: technical)"],
        verdict_stance="cautious",
        verdict_action=verdict_action,
    )


# ── check_verdict_direction ────────────────────────────────────────────────────


class TestCheckVerdictDirection:
    def test_bad_target_below_close_scores_zero(self) -> None:
        """AC unit test 1: target/resistance level below close flags direction mismatch.

        The comma-space split separates "Close above SMA-50 at 193.07" (current-position
        clause, no target keyword) from "with a potential target of SMA-20 at 210.30"
        (target clause). Only 210.30 should be flagged; 193.07 must NOT be a false positive.
        """
        thesis = _thesis(
            "Close above SMA-50 at 193.07, with a potential target of SMA-20 at 210.30"
        )
        ok, comment = check_verdict_direction(thesis, _TECHNICAL_REPORT_225)
        assert not ok
        assert "210.30" in comment
        # 193.07 is a current-position reference (above SMA-50), not a target — must not be flagged
        assert "193.07" not in comment

    def test_sane_action_scores_one(self) -> None:
        """AC unit test 2: levels correctly oriented relative to close pass."""
        thesis = _thesis("Trim above 250; defend SMA-50 at 193.07")
        ok, comment = check_verdict_direction(thesis, _TECHNICAL_REPORT_225)
        assert ok, comment

    def test_no_levels_in_verdict_passes(self) -> None:
        thesis = _thesis("Hold current position and reassess after next earnings.")
        ok, comment = check_verdict_direction(thesis, _TECHNICAL_REPORT_225)
        assert ok, comment

    def test_missing_close_line_skips_check(self) -> None:
        """When the technical report has no Close: line, check is a safe no-op."""
        report_no_close = "## PRICE ACTION\nTrend: bullish\n"
        thesis = _thesis("Close above SMA-50 at 193.07, target 210.30")
        ok, comment = check_verdict_direction(thesis, report_no_close)
        assert ok
        assert "skipped" in comment

    def test_support_level_below_close_passes(self) -> None:
        """Support correctly below current close is not a mismatch."""
        thesis = _thesis("Defend key support at 193.07; consider trim above 240")
        ok, comment = check_verdict_direction(thesis, _TECHNICAL_REPORT_225)
        assert ok, comment

    def test_support_level_above_close_fails(self) -> None:
        """Support cited above current close is already broken — flag it."""
        # close=225.32, support cited at 250 (above close — already broken)
        report_low_close = _TECHNICAL_REPORT_225.replace("Close: 225.32", "Close: 180.00")
        thesis = _thesis("Hold support at 193.07 as floor")
        ok, comment = check_verdict_direction(thesis, report_low_close)
        assert not ok
        assert "193.07" in comment

    def test_sma_period_not_extracted_as_level(self) -> None:
        """SMA-50 period (50) must not be mistaken for a price level."""
        thesis = _thesis("Trim above SMA-50 and SMA-20 once price confirms")
        ok, _ = check_verdict_direction(thesis, _TECHNICAL_REPORT_225)
        # 50 and 20 are SMA periods, close is 225.32 — neither should trigger
        assert ok

    def test_resistance_level_below_close_fails(self) -> None:
        """Resistance cited below current close is already cleared — flag it."""
        thesis = _thesis("Break above resistance at 210.30 is the key trigger")
        ok, comment = check_verdict_direction(thesis, _TECHNICAL_REPORT_225)
        assert not ok
        assert "210.30" in comment


# ── record_mismatch + Discord escalation ─────────────────────────────────────


class TestRecordMismatch:
    def setup_method(self) -> None:
        _mismatch_timestamps.clear()

    def test_below_threshold_does_not_fire(self) -> None:
        with patch("agent.post_checks._fire_discord_alert") as mock_alert:
            for _ in range(_ESCALATE_THRESHOLD - 1):
                record_mismatch()
            mock_alert.assert_not_called()

    def test_at_threshold_fires_discord(self) -> None:
        with patch("agent.post_checks._fire_discord_alert") as mock_alert:
            for _ in range(_ESCALATE_THRESHOLD):
                record_mismatch()
            mock_alert.assert_called_once()

    def test_clears_window_after_alert(self) -> None:
        """After escalation the window resets so a new burst can re-trigger."""
        with patch("agent.post_checks._fire_discord_alert"):
            for _ in range(_ESCALATE_THRESHOLD):
                record_mismatch()
        assert len(_mismatch_timestamps) == 0

    def test_expired_events_pruned_before_count(self) -> None:
        """Events older than 1h do not count toward the threshold."""
        past = time.monotonic() - 7200  # 2h ago
        for _ in range(_ESCALATE_THRESHOLD):
            _mismatch_timestamps.append(past)
        with patch("agent.post_checks._fire_discord_alert") as mock_alert:
            record_mismatch()  # one fresh event, threshold not hit
            mock_alert.assert_not_called()
