"""Unit tests for the rag_impact contamination latency classification (QNT-278).

The live behavioral run needs the LiteLLM proxy; these pin the pure latency
classification offline so the fast-degraded floor can't silently regress. The
fast floor is the QNT-278 fix: Groq throttling shows up not only as a call that
ran to its timeout (slow) but as a truncated, suspiciously FAST completion that
drops the planted entity -- which used to report a "trustworthy" 7/8 instead of
being flagged.
"""

from __future__ import annotations

from agent.evals.rag_impact_eval import (
    CONTAMINATION_FAST_LATENCY_MS,
    CONTAMINATION_LATENCY_MS,
    RagImpactFixture,
    RagImpactOutcome,
    RagImpactReport,
    contamination_warning,
)


def _fixture(fid: str, kind: str = "positive") -> RagImpactFixture:
    return RagImpactFixture(
        id=fid,
        ticker="MSFT",
        question="q",
        corpus="earnings",
        kind=kind,  # type: ignore[arg-type]
        planted_entity="Project Quillon",
        planted_figure="$2.9 billion",
    )


def _outcome(
    fid: str,
    *,
    status: str,
    elapsed_ms: int,
    kind: str = "positive",
) -> RagImpactOutcome:
    return RagImpactOutcome(
        fixture=_fixture(fid, kind=kind),
        status=status,  # type: ignore[arg-type]
        entity_present=status == "pass",
        search_fired=True,
        answer_chars=400,
        elapsed_ms=elapsed_ms,
    )


def _report(*outcomes: RagImpactOutcome) -> RagImpactReport:
    return RagImpactReport(outcomes=tuple(outcomes))


def test_healthy_run_is_not_flagged() -> None:
    """Positives that ran several seconds (clean window) raise no warning."""
    report = _report(
        _outcome("a", status="pass", elapsed_ms=6600),
        _outcome("b", status="fail", elapsed_ms=5200),
    )
    assert contamination_warning(report) is None


def test_fast_degraded_positive_is_flagged() -> None:
    """A positive under the fast floor (~1.4s) is the truncated-completion
    signature -- the run must be flagged, not trusted as a clean 7/8."""
    report = _report(
        _outcome("ok", status="pass", elapsed_ms=6600),
        _outcome("msft-guidance-earnings", status="fail", elapsed_ms=1400),
    )
    warning = contamination_warning(report)
    assert warning is not None
    assert "fast-degraded" in warning
    assert "msft-guidance-earnings" in warning


def test_slow_throttle_is_flagged() -> None:
    """A fixture over the timeout ceiling keeps firing the slow-throttle signal."""
    report = _report(
        _outcome("slowpoke", status="pass", elapsed_ms=CONTAMINATION_LATENCY_MS + 1),
    )
    warning = contamination_warning(report)
    assert warning is not None
    assert "slow-throttle" in warning


def test_fast_negative_control_is_not_flagged() -> None:
    """A negative control's no-fabrication answer is legitimately short and quick
    -- the fast floor is scoped to positives, so a fast negative is not an alarm."""
    report = _report(
        _outcome("ok", status="pass", elapsed_ms=6600),
        _outcome("neg-empty", status="pass", kind="negative_control", elapsed_ms=900),
    )
    assert contamination_warning(report) is None


def test_fast_floor_excludes_ungated_rows() -> None:
    """A provider_error / infra_error / misrouted row short-circuited before a real
    generation -- its elapsed time is not a throttle signal and must not flag."""
    report = _report(
        _outcome("ok", status="pass", elapsed_ms=6600),
        _outcome("provider", status="provider_error", elapsed_ms=120),
        _outcome("infra", status="infra_error", elapsed_ms=80),
        _outcome("misrouted", status="misrouted", elapsed_ms=500),
    )
    assert contamination_warning(report) is None


def test_fast_floor_boundary_is_inclusive() -> None:
    """A positive exactly at the floor is flagged (<= boundary)."""
    report = _report(
        _outcome("edge", status="fail", elapsed_ms=CONTAMINATION_FAST_LATENCY_MS),
    )
    assert contamination_warning(report) is not None
