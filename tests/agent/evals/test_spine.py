"""Regression guards for the shared eval spine (QNT-293).

The spine exists to stop the history envelope / run-identity / CLI dispatch from
drifting apart across the eight suites (the prompt-version hash drifted once
before QNT-230 unified it). These lock the two invariants that guarantee it:

* every suite reads the SAME history envelope object (the re-export is live), and
* ``python -m agent.evals --suite retrieval`` is the same offline gate as the
  standalone retrieval runner (the AC2 parity target, as a standing detector).
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest


def test_append_suite_history_writes_envelope_plus_metric_columns(tmp_path: Path) -> None:
    """The per-suite writer (QNT-293 follow-up) lays down ENVELOPE_FIELDS + the
    suite's own metric columns, stamps the shared envelope, and names the file
    ``{suite}_history.csv``."""
    from agent.evals.spine import (
        ENVELOPE_FIELDS,
        append_suite_history,
        suite_history_path,
    )

    path = suite_history_path("demo", tmp_path)
    assert path.name == "demo_history.csv"
    append_suite_history("demo", ("score", "n"), [{"score": 0.5, "n": 3}], run_id="rX", path=path)

    rows = list(csv.DictReader(path.open()))
    assert len(rows) == 1
    assert list(rows[0].keys()) == [*ENVELOPE_FIELDS, "score", "n"]
    assert rows[0]["run_id"] == "rX"
    assert rows[0]["suite"] == "demo"
    assert rows[0]["score"] == "0.5" and rows[0]["n"] == "3"
    # envelope identity columns are stamped from the shared helpers, not blank
    assert rows[0]["git_sha"] and rows[0]["prompt_version"]


def test_committed_suite_files_match_their_code_schema() -> None:
    """Each committed {suite}_history.csv header must equal ENVELOPE_FIELDS + that
    suite's own *_FIELDS -- the per-suite replacement for the old wide-CSV
    append-only guard. Catches a suite's code schema drifting from the file it
    writes (the QNT-264/QNT-277 header-misalignment class, per suite now)."""
    from agent.evals.deepeval_eval import DEEPEVAL_FIELDS, DEEPEVAL_HISTORY_PATH
    from agent.evals.dialogue_eval import DIALOGUE_FIELDS, DIALOGUE_HISTORY_PATH
    from agent.evals.golden_set import GOLDEN_FIELDS, GOLDEN_HISTORY_PATH
    from agent.evals.rag_impact_eval import RAG_IMPACT_FIELDS, RAG_IMPACT_HISTORY_PATH
    from agent.evals.retrieval_eval import RETRIEVAL_FIELDS, RETRIEVAL_HISTORY_PATH
    from agent.evals.spine import ENVELOPE_FIELDS

    for path, fields in (
        (GOLDEN_HISTORY_PATH, GOLDEN_FIELDS),
        (RETRIEVAL_HISTORY_PATH, RETRIEVAL_FIELDS),
        (DIALOGUE_HISTORY_PATH, DIALOGUE_FIELDS),
        (DEEPEVAL_HISTORY_PATH, DEEPEVAL_FIELDS),
        (RAG_IMPACT_HISTORY_PATH, RAG_IMPACT_FIELDS),
    ):
        header = next(csv.reader(path.open()))
        assert header == [*ENVELOPE_FIELDS, *fields], (
            f"{path.name} header drifted from its *_FIELDS"
        )


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
