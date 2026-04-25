"""Eval-suite entry point (QNT-67).

Runs the golden-set against the live agent and exits non-zero if any record
fails the hallucination or tool-call contracts (see
``golden_set.is_failing``). Judge score is treated as a soft signal — set
``EVAL_MIN_JUDGE`` to gate on it once history.csv shows a stable baseline.

Examples::

    uv run python -m agent.evals
    uv run python -m agent.evals --only NVDA
    uv run python -m agent.evals --history-path /tmp/history.csv
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from agent.evals.golden_set import (
    HISTORY_PATH,
    fail_threshold_from_env,
    is_failing,
    run_all,
    summarise,
)
from agent.tracing import langfuse

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.evals")
    parser.add_argument(
        "--only",
        help="Run only records for this ticker (case-insensitive)",
    )
    parser.add_argument(
        "--history-path",
        type=Path,
        default=HISTORY_PATH,
        help=f"Where to append history rows (default: {HISTORY_PATH})",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    try:
        run_id, outcomes = run_all(history_path=args.history_path, only=args.only)
    except Exception:
        logger.exception("eval run failed")
        return 1
    finally:
        langfuse.flush()

    print(f"run_id: {run_id}")
    print(summarise(outcomes))

    if is_failing(outcomes):
        return 1

    threshold = fail_threshold_from_env()
    if threshold is not None:
        judged = [o.judge_score for o in outcomes if o.judge_score is not None]
        if judged:
            avg = sum(judged) / len(judged)
            if avg < threshold:
                print(
                    f"\n[fail] avg_judge {avg:.2f} < EVAL_MIN_JUDGE {threshold}",
                    file=sys.stderr,
                )
                return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
