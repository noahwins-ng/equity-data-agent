"""Eval-suite entry point (QNT-67, unified spine QNT-293).

One CLI over the eval suites. With no flag it runs the golden set exactly as it
always has -- same stdout shape, same exit-code semantics -- so CI and muscle
memory are untouched. ``--suite`` dispatches a named suite through the shared
spine (:mod:`agent.evals.spine`): each suite returns a
:class:`~agent.evals.spine.SuiteResult` and this module prints its summary and
maps ``failed`` to the process exit code, uniformly.

Runs the golden-set against the live agent and exits non-zero if any record
fails the hallucination or tool-call contracts (see ``golden_set.is_failing``).
Judge score is a soft signal -- set ``EVAL_MIN_JUDGE`` to gate on it once
history.csv shows a stable baseline.

Examples::

    uv run python -m agent.evals                       # golden set (default)
    uv run python -m agent.evals --only NVDA
    uv run python -m agent.evals --history-path /tmp/history.csv
    uv run python -m agent.evals --suite retrieval     # offline retrieval gate
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Eval / bench runs are fully reproducible from history.csv (run_id +
# git_sha + prompt_version + judge + cosine + token cost), so Langfuse
# traces are pure duplicates that burn the free-tier event budget — a
# 22-record bench emits ~200 observations. Disable tracing before
# `agent.tracing` reads settings at import time. Override by setting the
# keys explicitly in the calling env if a single run does need a trace.
os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""

from agent.evals.golden_set import (  # noqa: E402
    GOLDEN_HISTORY_PATH,
    fail_threshold_from_env,
    is_failing,
    run_all,
    summarise,
)
from agent.evals.spine import SuiteResult  # noqa: E402
from agent.llm import set_model_override  # noqa: E402
from agent.tracing import flush as flush_langfuse  # noqa: E402

logger = logging.getLogger(__name__)


def _run_golden(args: argparse.Namespace) -> SuiteResult:
    """Golden set against the live agent — the default suite (QNT-67).

    Preserves the historical stdout (``run_id:`` line then the summary) and the
    exit-code contract: hard hallucination / tool-call failures gate, the judge
    score gates only when ``EVAL_MIN_JUDGE`` is set.
    """
    run_id_suffix: str | None = None
    if args.model:
        set_model_override(args.model)
        # 'equity-agent/bench-gptoss120b' → 'bench-gptoss120b'
        run_id_suffix = args.model.split("/", 1)[-1]

    try:
        run_id, outcomes = run_all(
            history_path=args.history_path,
            only=args.only,
            run_id_suffix=run_id_suffix,
        )
    finally:
        flush_langfuse()

    summary = f"run_id: {run_id}\n{summarise(outcomes)}"
    failed = is_failing(outcomes)
    warning = ""

    if not failed:
        threshold = fail_threshold_from_env()
        if threshold is not None:
            judged = [o.judge_score for o in outcomes if o.judge_score is not None]
            if judged:
                avg = sum(js.composite for js in judged) / len(judged)
                if avg < threshold:
                    # Returned (not printed here) so main() emits it to stderr
                    # strictly AFTER the stdout summary -- preserving the
                    # historical stdout-then-stderr order (QNT-293 review).
                    warning = f"\n[fail] avg_judge {avg:.2f} < EVAL_MIN_JUDGE {threshold}"
                    failed = True

    return SuiteResult(summary=summary, failed=failed, run_id=run_id, warning=warning)


def _run_retrieval(args: argparse.Namespace) -> SuiteResult:
    """Deterministic retrieval gate over the frozen served-path artifacts.

    Same code path as ``python -m agent.evals.retrieval_eval`` with no args and
    as the ci.yml ``-m eval`` pytest gate: score the committed qrels + hybrid
    run, print the scorecard, gate on the metric floors. Offline and LLM-free
    (``ir_measures`` is imported lazily here so the golden path never needs it).
    ``score_offline`` prints its own scorecard, so the returned summary is empty.
    """
    from agent.evals.retrieval_eval import score_offline

    return SuiteResult(summary="", failed=bool(score_offline()))


_SUITES = {
    "golden": _run_golden,
    "retrieval": _run_retrieval,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.evals")
    parser.add_argument(
        "--suite",
        choices=sorted(_SUITES),
        default="golden",
        help="Which eval suite to run through the shared spine (default: golden).",
    )
    parser.add_argument(
        "--only",
        help="Run only records for this ticker (case-insensitive). Golden suite only.",
    )
    parser.add_argument(
        "--history-path",
        type=Path,
        default=GOLDEN_HISTORY_PATH,
        help=f"Where to append history rows (default: {GOLDEN_HISTORY_PATH}). Golden suite only.",
    )
    parser.add_argument(
        "--model",
        help=(
            "LiteLLM alias to route every plan / synthesize / judge call through "
            "(e.g. 'equity-agent/bench-gptoss120b'). Bypasses EQUITY_AGENT_PROVIDER "
            "and tags the run_id with the alias suffix so history.csv filters per "
            "model. Golden suite only. QNT-129."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    try:
        result = _SUITES[args.suite](args)
    except Exception:
        logger.exception("eval run failed")
        return 1

    if result.summary:
        print(result.summary)
    if result.warning:
        print(result.warning, file=sys.stderr)

    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
