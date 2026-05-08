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
    # always a typo for the equity-research shapes (thesis / quick_fact /
    # comparison). The conversational path is the explicit exception: it
    # skips the gather node entirely so empty IS the right answer there
    # (QNT-156). Records opt in via ``expected_intent: conversational``.
    for r in load_goldens():
        if r.expected_intent == "conversational":
            assert not r.expected_tools, (
                f"{r.id}: conversational records must have empty expected_tools"
            )
            continue
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
    # AC: originally "15-20 curated records" (QNT-67). Bumped to 15-25 in
    # QNT-176 to make room for one record per supported intent shape — the
    # focused trio (fundamental / technical / news_sentiment) added 3 rows.
    # Lower bound is the substantive guarantee (anything fewer is too thin);
    # upper bound is a soft cap so the suite stays fast enough to run on
    # every prompt edit.
    records = load_goldens()
    assert 15 <= len(records) <= 25, f"record count {len(records)} outside 15-25 band"


def test_every_intent_has_at_least_one_golden() -> None:
    """QNT-176: regression guard so a future intent addition lands with a
    golden record alongside it, not as an afterthought. The 'auto' default
    is allowed for records that don't pin an explicit intent (the original
    QNT-67 thesis records use it); but every NAMED intent in
    ``agent.intent.Intent`` must appear in at least one record's
    ``expected_intent`` field."""
    from typing import get_args

    from agent.intent import Intent

    pinned = {r.expected_intent for r in load_goldens() if r.expected_intent != "auto"}
    named = set(get_args(Intent))
    # ``thesis`` is implied by every ``auto`` record (the heuristic default),
    # so it doesn't need to be pinned. Every other intent does.
    required = named - {"thesis"}
    missing = required - pinned
    assert not missing, (
        f"goldens missing pinned records for intents: {sorted(missing)} (found: {sorted(pinned)})"
    )
