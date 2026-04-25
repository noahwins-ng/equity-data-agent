"""Coverage tests for goldens/questions.yaml (QNT-67 AC).

Locks two invariants the runtime can't catch:
    * Every ticker in shared.tickers.TICKERS has at least one golden record
      — guards against the Phase 2 retro lesson (sample broadly), where
      spot-checks skewing to the heavyweights left bugs in UNH / V / JPM
      invisible (feedback_sample_ac_broadly.md).
    * Every record's expected_tools is a subset of the planable tool surface
      — a typo like ``valuation`` would never trigger and would silently
      pass the tool-call check.
"""

from __future__ import annotations

from agent.evals.golden_set import load_goldens
from agent.prompts import REPORT_TOOLS
from shared.tickers import TICKERS


def test_every_ticker_has_at_least_one_question() -> None:
    records = load_goldens()
    covered = {r.ticker for r in records}
    missing = set(TICKERS) - covered
    assert not missing, f"goldens missing questions for tickers: {sorted(missing)}"


def test_every_record_has_at_least_one_expected_tool() -> None:
    # An empty expected_tools list trivially passes the tool-call check —
    # always a typo, never intentional.
    for r in load_goldens():
        assert r.expected_tools, f"{r.id}: expected_tools is empty"


def test_expected_tools_are_in_report_registry() -> None:
    valid = set(REPORT_TOOLS)
    for r in load_goldens():
        for tool in r.expected_tools:
            assert tool in valid, f"{r.id}: unknown expected_tool {tool!r} (valid: {sorted(valid)})"


def test_question_ids_are_unique() -> None:
    records = load_goldens()
    ids = [r.id for r in records]
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"


def test_record_count_within_target_band() -> None:
    # AC: "15-20 curated records". Lower bound is the substantive guarantee
    # (anything fewer is too thin); upper bound is a soft cap so the suite
    # stays fast enough to run on every prompt edit.
    records = load_goldens()
    assert 15 <= len(records) <= 20, f"record count {len(records)} outside 15-20 band"
