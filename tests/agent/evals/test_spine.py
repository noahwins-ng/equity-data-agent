"""Regression guards for the shared eval spine (QNT-293).

The spine exists to stop the history envelope / run-identity / CLI dispatch from
drifting apart across the eight suites (the prompt-version hash drifted once
before QNT-230 unified it). These lock the two invariants that guarantee it:

* every suite reads the SAME history envelope object (the re-export is live), and
* ``python -m agent.evals --suite retrieval`` is the same offline gate as the
  standalone retrieval runner (the AC2 parity target, as a standing detector).
"""

from __future__ import annotations

import pytest


def test_golden_reexports_spine_envelope() -> None:
    """golden_set must re-export the spine's envelope, not a divergent copy --
    the whole point of the spine is one authority for HISTORY_FIELDS/PATH."""
    from agent.evals import spine
    from agent.evals.golden_set import HISTORY_FIELDS, HISTORY_PATH

    assert HISTORY_FIELDS is spine.HISTORY_FIELDS
    assert HISTORY_PATH is spine.HISTORY_PATH


def test_suite_registry_covers_migrated_pair() -> None:
    """The two ci.yml-gating suites are dispatchable through the spine CLI."""
    from agent.evals.__main__ import _SUITES

    assert {"golden", "retrieval"} <= set(_SUITES)


def test_cli_prints_summary_to_stdout_and_warning_to_stderr(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI prints a suite's summary to stdout and its (optional) warning to
    stderr -- never the warning to stdout. Locks the golden threshold-fail
    ordering the QNT-293 review flagged: the sub-threshold diagnostic must not
    precede or contaminate the stdout summary."""
    from agent.evals import __main__ as cli
    from agent.evals.spine import SuiteResult

    monkeypatch.setitem(
        cli._SUITES,
        "golden",
        lambda args: SuiteResult(summary="SUMMARY", failed=True, warning="\n[fail] WARN"),
    )
    rc = cli.main([])
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.out == "SUMMARY\n"
    assert captured.err.strip() == "[fail] WARN"


@pytest.mark.eval
def test_suite_retrieval_matches_standalone_gate() -> None:
    """`--suite retrieval` returns the same exit code as the standalone offline
    gate -- the spine dispatch must not change the retrieval pass/fail verdict.
    Marked eval: needs ir_measures + the frozen retrieval artifacts."""
    from agent.evals.__main__ import main
    from agent.evals.retrieval_eval import score_offline

    assert main(["--suite", "retrieval"]) == (1 if score_offline() else 0)
