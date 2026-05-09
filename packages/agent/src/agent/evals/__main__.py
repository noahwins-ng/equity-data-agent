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
    HISTORY_PATH,
    fail_threshold_from_env,
    is_failing,
    run_all,
    summarise,
)
from agent.llm import set_model_override  # noqa: E402
from agent.tracing import flush as flush_langfuse  # noqa: E402

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
    parser.add_argument(
        "--model",
        help=(
            "LiteLLM alias to route every plan / synthesize / judge call through "
            "(e.g. 'equity-agent/bench-gptoss120b'). Bypasses EQUITY_AGENT_PROVIDER "
            "and tags the run_id with the alias suffix so history.csv filters per "
            "model. QNT-129."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

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
    except Exception:
        logger.exception("eval run failed")
        return 1
    finally:
        flush_langfuse()

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
