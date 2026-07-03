"""Shared eval-harness spine (QNT-293).

``packages/agent/src/agent/evals/`` grew to eight harnesses -- golden_set,
dialogue_eval, deepeval_eval, retrieval_eval, rag_impact_eval, rag_smoke_eval,
news_search_eval, routing_eval -- each with its own runner, history format,
threshold convention, and env-gating. "Did anything regress" became eight
commands with eight output shapes, and every cross-cutting change (a new model
alias, a new history column, a new gating env var) touched up to eight files.
That class of drift already bit once: the prompt-version hash was hand-synced
between ``graph.py`` and ``golden_set.py`` and silently diverged until QNT-230
unified it in ``agent/prompt_version.py``.

This module is the shared SPINE, not a rewrite. It owns the pieces that must be
identical across suites so they can never drift again:

* The **history envelope + writer** -- :data:`ENVELOPE_FIELDS`
  (``run_id`` / ``git_sha`` / ``prompt_version`` / ``suite``) and
  :func:`append_suite_history`, which writes one ``{suite}_history.csv`` per
  suite: the shared envelope columns followed by that suite's own metric columns.
  Per-suite metrics stay suite-defined; only the envelope is shared. Each suite
  owns its file, so there is no sparsity (no row blanks another suite's columns)
  and no shared column order to keep in lockstep -- the QNT-264 / QNT-277
  header-misalignment class of bug can't recur. (This replaced a single wide
  ``history.csv`` keyed by an ``eval_type`` column; a ``metrics``-as-a-JSON blob
  was considered and dropped -- a plain per-suite CSV stays spreadsheet- and
  ``git log -p``-readable, and cross-suite queries keyed on ``run_id`` are rare
  for eval history.)
* The **run-identity helpers** -- :func:`git_sha` and :func:`prompt_version`,
  stamped on every row by the writer so a run is reproducible from its file alone.
* The **gating primitives** -- :func:`threshold_from_env` (threshold-from-env
  with a single warn-on-garbage convention) and :class:`SuiteResult` (the
  shared summary+pass/fail contract the CLI dispatches on).

Provider-error classification (``evals/provider_errors.py``) is already a shared
module; suites import it directly.

Adding a suite to the spine
---------------------------
1. Write a runner that returns a :class:`SuiteResult` (``summary`` for stdout,
   ``failed`` for the exit code). Do the suite's own printing to *stderr* only;
   the CLI prints ``summary`` to stdout so every suite's stdout shape is uniform.
2. Declare a ``{SUITE}_FIELDS`` tuple of the suite's own metric columns next to
   its code, and write history via
   ``append_suite_history(suite, SUITE_FIELDS, rows, run_id=...)`` -- it stamps
   the shared envelope and writes ``{suite}_history.csv``. Don't reach for a
   shared column list; each suite owns its schema.
3. Gate off env via :func:`threshold_from_env` rather than a bespoke parser.
4. Register it in the ``_SUITES`` dispatch table in ``evals/__main__.py`` so
   ``python -m agent.evals --suite <name>`` reaches it.

All eight suites' write paths flow through this spine; the golden + retrieval
pair also dispatch through the ``--suite`` CLI.
"""

from __future__ import annotations

import csv
import logging
import os
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

EVALS_DIR = Path(__file__).parent

# The shared row envelope written on every per-suite history file (QNT-293
# follow-up). ``suite`` names the harness (== the file stem); each suite adds its
# own metric columns after these. Written by append_suite_history from the shared
# git_sha() / prompt_version() so run identity is byte-identical across files.
ENVELOPE_FIELDS = ("run_id", "git_sha", "prompt_version", "suite")


def suite_history_path(suite: str, history_dir: Path | None = None) -> Path:
    """Path to a suite's own ``{suite}_history.csv`` (QNT-293 follow-up)."""
    return (history_dir or EVALS_DIR) / f"{suite}_history.csv"


def append_suite_history(
    suite: str,
    metric_fields: Sequence[str],
    rows: Iterable[Mapping[str, object]],
    *,
    run_id: str,
    path: Path | None = None,
) -> str:
    """Append rows to ``{suite}_history.csv``; returns the ``run_id`` used.

    The file's header is :data:`ENVELOPE_FIELDS` + ``metric_fields`` (the suite's
    own columns). Each item in ``rows`` is a mapping of that suite's columns; the
    envelope (``run_id`` / ``git_sha`` / ``prompt_version`` / ``suite``) is stamped
    here from the shared helpers so every suite's identity columns match. Creates
    the file with a header when absent. Callers generate their own ``run_id`` (the
    per-suite id formats predate this) and pass it in. ``path`` overrides the
    default ``{suite}_history.csv`` (CI / experimentation / tests).

    Concurrency: the exists-check + write is not atomic (same single-developer
    caveat as the legacy shared writer); wrap with a lock if this ever runs
    concurrently.
    """
    target = path or suite_history_path(suite)
    fields = (*ENVELOPE_FIELDS, *metric_fields)
    sha = git_sha()
    pv = prompt_version()
    new_file = not target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)

    with target.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        if new_file:
            writer.writeheader()
        for metrics in rows:
            row: dict[str, object] = {field: "" for field in fields}
            row["run_id"] = run_id
            row["git_sha"] = sha
            row["prompt_version"] = pv
            row["suite"] = suite
            row.update(metrics)
            writer.writerow(row)
    return run_id


def git_sha() -> str:
    """Short SHA of HEAD, or ``unknown`` if git isn't reachable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def prompt_version() -> str:
    """Stable hash of every agent prompt + report-tool registry (QNT-187, QNT-230).

    Delegates to the shared :func:`agent.prompt_version.compute_prompt_version`
    so this harness and ``agent.graph`` produce the SAME version for the same
    prompts. Previously these were hand-synced copies that had drifted (the
    eval copy hashed only five system prompts to graph's nine), so the same
    ``prompt_version`` column meant different things depending on the writer.
    """
    from agent.graph import _build_plan_prompt, _build_thesis_plan_prompt
    from agent.prompt_version import compute_prompt_version

    return compute_prompt_version(_build_plan_prompt, _build_thesis_plan_prompt)


def threshold_from_env(name: str) -> float | None:
    """Optional numeric gate threshold, read from environment variable ``name``.

    Off by default (returns ``None`` when unset) so a suite's hard contracts
    stay the primary gate; the env threshold is an opt-in soft-signal floor
    (e.g. ``EVAL_MIN_JUDGE=7``). One warn-on-garbage convention shared by every
    suite so a typo'd value degrades to "off", never to a crash.

    CLAUDE.md routes config through ``shared.Settings``; these are a deliberate
    exception -- developer-time eval knobs, off by default and never read in
    prod. Adding them to the global Settings object would pollute the runtime
    contract. Promote if an eval knob ever needs to be set the way
    ``CLICKHOUSE_HOST`` is.
    """
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r not a number; ignoring", name, raw)
        return None


@dataclass(frozen=True)
class SuiteResult:
    """The shared runner contract every spine suite returns.

    ``summary`` is printed verbatim to stdout by the CLI (empty string = the
    suite already printed its own report; the CLI prints nothing). ``failed``
    drives the process exit code (non-zero on failure). ``run_id`` is the
    history run identifier when the suite wrote one, else ``None``.

    ``warning`` is an optional diagnostic the CLI prints to stderr *after* the
    stdout ``summary`` -- suites return it rather than printing it themselves so
    the stdout-summary-then-stderr-diagnostic order stays fixed across suites
    (e.g. the golden set's sub-threshold ``[fail] avg_judge`` line, which
    historically followed the summary on stdout).
    """

    summary: str
    failed: bool
    run_id: str | None = None
    warning: str = ""


__all__ = [
    "ENVELOPE_FIELDS",
    "EVALS_DIR",
    "SuiteResult",
    "append_suite_history",
    "git_sha",
    "prompt_version",
    "suite_history_path",
    "threshold_from_env",
]
