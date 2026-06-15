"""Unit coverage for the integration-suite reachability gate (QNT-239).

The integration step in ``ci.yml`` is bare ``pytest -m integration``, and
pytest exits 0 when every test is *skipped*. The conftest auto-skips the whole
integration suite when ClickHouse is unreachable, so a CH service that silently
fails to come up would let the real-engine SQL gate pass green with nothing
actually executed — the "aggregate green hides invariants" failure mode.

``_integration_gate_action`` is the pure decision the gate makes; pinning its
three branches here guarantees the CI-hard-fail behaviour can't be quietly
reverted to an unconditional skip.
"""

from __future__ import annotations

from tests.integration.conftest import _integration_gate_action


def test_reachable_runs_regardless_of_ci() -> None:
    assert _integration_gate_action(reachable=True, is_ci=False) is None
    assert _integration_gate_action(reachable=True, is_ci=True) is None


def test_unreachable_locally_skips() -> None:
    assert _integration_gate_action(reachable=False, is_ci=False) == "skip"


def test_unreachable_in_ci_fails_loud() -> None:
    # The whole point of QNT-239: a dead CH service in CI must break the build,
    # not silently skip the SQL gate.
    assert _integration_gate_action(reachable=False, is_ci=True) == "fail"
