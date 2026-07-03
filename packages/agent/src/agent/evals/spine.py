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

* The **history envelope** -- ``HISTORY_PATH`` and ``HISTORY_FIELDS``, the single
  wide ``history.csv`` schema every suite appends to, discriminated by the
  ``eval_type`` column. Per-suite metrics stay suite-defined (each suite fills
  the columns it owns and blanks the rest); only the envelope columns
  (``run_id`` / ``git_sha`` / ``prompt_version`` / ``eval_type``) are shared.
* The **run-identity helpers** -- :func:`git_sha` and :func:`prompt_version`,
  stamped on every row so a run is reproducible from the CSV alone.
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
2. When it writes history, fill the envelope columns from :func:`git_sha` /
   :func:`prompt_version`, tag ``eval_type`` with the suite name, and add any
   new metric columns to :data:`HISTORY_FIELDS` **at the end** -- this is a
   ragged append-only CSV; older rows are positionally shorter, so a mid-list
   insert misaligns every column after it on read.
3. Gate off env via :func:`threshold_from_env` rather than a bespoke parser.
4. Register it in the ``_SUITES`` dispatch table in ``evals/__main__.py`` so
   ``python -m agent.evals --suite <name>`` reaches it.

Migration status: golden + retrieval flow through the spine (the ci.yml gating
pair). The remaining six fold in opportunistically as each is next touched --
they still import the envelope from ``golden_set`` (which re-exports it from
here), so nothing broke; repoint each at ``agent.evals.spine`` when you touch it.

Eventual target -- per-suite history files, not one shared table (decided as a
QNT-293 follow-up). Each suite gets its own ``{suite}_history.csv`` whose columns
are exactly that suite's metrics: no sparsity (today a retrieval row blanks ~40
dialogue/deepeval columns) and no shared column order to keep in lockstep, so the
QNT-264 / QNT-277 header-misalignment class of bug can't recur. The spine still
owns the shared ENVELOPE (``run_id`` / ``git_sha`` / ``prompt_version`` /
``suite``) written by one append helper, so run identity stays consistent across
every file -- this maps the "per-suite metrics stay suite-defined; only the
envelope is shared" split directly onto the filesystem. (A single
``metrics``-as-a-JSON-column table was considered and dropped: it buries each
suite's metric schema in an untyped blob and needs ``json_normalize`` to analyse,
whereas a plain per-suite CSV stays spreadsheet- and ``git log -p``-readable. The
one thing a single file buys -- a cross-suite query keyed on ``run_id`` -- is rare
for eval history, which is almost always one suite's trend at a time.) Converting
now would be the maximal cross-cutting change (all eight read/write the shared
``history.csv``) and would need the committed trend split out, so the wide CSV is
preserved until each suite folds in and its rows split into ``{suite}_history.csv``
along the way.
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
HISTORY_PATH = EVALS_DIR / "history.csv"

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


HISTORY_FIELDS = (
    "run_id",
    "git_sha",
    "prompt_version",
    "ticker",
    "question_id",
    "question",
    "faithfulness",
    "structure",
    "correctness",
    "analyst_logic",
    "composite",
    "cosine",
    "tool_call_ok",
    "hallucination_ok",
    "elapsed_ms",
    "eval_type",
    "dialogue_fixture_id",
    "dialogue_turns",
    "analyst_likeness",
    "analyst_likeness_rationale",
    "helpfulness",
    "helpfulness_rationale",
    "non_hallucination",
    "non_hallucination_rationale",
    "exploration_quality",
    "exploration_quality_rationale",
    "voice_match",
    "voice_match_rationale",
    "dialogue_composite",
    "judge_model",
    "agent_model",
    # QNT-218: per-run aggregate band, written on a single eval_type="dialogue_summary"
    # row per run. Axis columns above carry the mean; these carry its standard error
    # across fixtures, and dialogue_n the fixture count. Blank on per-fixture rows.
    "analyst_likeness_se",
    "helpfulness_se",
    "non_hallucination_se",
    "exploration_quality_se",
    "voice_match_se",
    "dialogue_composite_se",
    "dialogue_n",
    # QNT-261: retrieval-eval aggregate, written on a single eval_type="retrieval"
    # row per run (recall@k / MRR / nDCG from ir_measures). Blank on every other
    # eval_type's rows. retrieval_n is the labeled-query count.
    "recall_at_5",
    "recall_at_20",
    "mrr",
    "ndcg_at_10",
    "retrieval_n",
    # QNT-264: LLM-judged DeepEval generation metrics (RAGAS set + custom G-Eval),
    # written on a single eval_type="deepeval" row per run. 0.0-1.0 floats, blank
    # on every other eval_type's rows. Distinct from the integer `faithfulness`
    # judge axis above (0-10, golden-set) -- the deepeval_* prefix avoids the
    # collision. deepeval_n is the sampled-record count (sample-gated, AC2).
    "deepeval_faithfulness",
    "deepeval_answer_relevancy",
    "deepeval_context_precision",
    "deepeval_context_recall",
    "deepeval_geval",
    "deepeval_n",
    # QNT-277: RAG-impact behavioral eval aggregate, written on a single
    # eval_type="rag_impact" row per run. rag_impact_pass_rate is the fraction of
    # gated fixtures (positives + negative-controls that actually fired) whose
    # retrieved-only fact reached (positive) / stayed out of (negative) the answer
    # text; rag_impact_n is that gated-fixture count. Blank on every other
    # eval_type's rows.
    "rag_impact_pass_rate",
    "rag_impact_n",
    # QNT-302: advisory verdict-vs-labels tripwire, per structured-thesis row
    # ("1"/"0"; blank on non-thesis rows and every run-level aggregate row).
    # Appended at the END to preserve the append-only column order this ragged
    # history.csv depends on -- older rows are positionally shorter, so a
    # mid-list insert would misalign every column after it on read. The mean
    # over structured rows is the observed mismatch rate that gates promoting
    # the tripwire from advisory to normalization; it never gates the exit code.
    "verdict_label_consistent",
)


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
    "HISTORY_FIELDS",
    "HISTORY_PATH",
    "SuiteResult",
    "append_suite_history",
    "git_sha",
    "prompt_version",
    "suite_history_path",
    "threshold_from_env",
]
