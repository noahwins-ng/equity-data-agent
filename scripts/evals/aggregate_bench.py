"""Per-bench-alias aggregation from history.csv (QNT-129, QNT-138).

Reads ``packages/agent/src/agent/evals/history.csv``, filters to bench
sweeps (run_id contains ``-bench-``), and emits one row per bench alias
with hallucination_ok, tool_call_ok, avg judge / cosine, p50 elapsed_ms,
ready to paste into ``docs/model-bench-2026-04.md``.

When an alias has multiple sweeps in history (e.g. QNT-138 re-ran
bench-llama3-70b on a fresh TPD bucket), only the **latest run_id** is
aggregated. Older sweeps remain in history.csv as audit trail but do
not contaminate the doc-publishable row. Pass ``--all-sweeps`` to keep
the legacy "all rows for this alias" behaviour.

Run::

    uv run python scripts/evals/aggregate_bench.py
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median

HISTORY = Path("packages/agent/src/agent/evals/history.csv")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aggregate_bench")
    parser.add_argument(
        "--all-sweeps",
        action="store_true",
        help=(
            "Aggregate every sweep per alias (legacy). Default: latest only. "
            "Warning: partial-sweep probe runs (e.g. 2-record NVDA probes) will "
            "distort per-alias averages."
        ),
    )
    parser.add_argument(
        "--history-path",
        type=Path,
        default=HISTORY,
        help=f"Path to history.csv (default: {HISTORY})",
    )
    args = parser.parse_args(argv)

    by_alias_run: dict[tuple[str, str], list[dict]] = defaultdict(list)
    with args.history_path.open() as fh:
        for row in csv.DictReader(fh):
            rid = row["run_id"]
            if "-bench-" not in rid:
                continue
            # 20260426T143926Z-e82b2e-bench-gptoss120b -> bench-gptoss120b
            suffix = "bench-" + rid.split("-bench-", 1)[1]
            by_alias_run[(suffix, rid)].append(row)

    by_alias: dict[str, list[dict]] = defaultdict(list)
    if args.all_sweeps:
        for (alias, _rid), rows in by_alias_run.items():
            by_alias[alias].extend(rows)
    else:
        latest_run: dict[str, str] = {}
        for alias, rid in by_alias_run:
            # Lexicographic sort on the ISO-8601 timestamp prefix is
            # chronological (e.g. 20260427T122411Z > 20260426T151730Z).
            if alias not in latest_run or rid > latest_run[alias]:
                latest_run[alias] = rid
        for alias, rid in latest_run.items():
            by_alias[alias] = by_alias_run[(alias, rid)]

    writer = csv.writer(sys.stdout)
    writer.writerow(
        [
            "alias_suffix",
            "n",
            "hallucination_ok",
            "tool_call_ok",
            "avg_judge",
            "avg_cosine",
            "p50_elapsed_ms",
        ]
    )
    for alias in sorted(by_alias):
        rows = by_alias[alias]
        n = len(rows)
        h_ok = sum(1 for r in rows if r["hallucination_ok"] == "1")
        t_ok = sum(1 for r in rows if r["tool_call_ok"] == "1")
        judges = [int(r["judge_score"]) for r in rows if r["judge_score"]]
        avg_judge = round(sum(judges) / len(judges), 2) if judges else None
        cosines = [float(r["cosine"]) for r in rows]
        avg_cos = round(sum(cosines) / len(cosines), 3) if cosines else None
        elapsed = sorted(int(r["elapsed_ms"]) for r in rows)
        p50 = int(median(elapsed)) if elapsed else 0
        writer.writerow(
            [
                alias,
                n,
                f"{h_ok}/{n}",
                f"{t_ok}/{n}",
                avg_judge,
                avg_cos,
                p50,
            ]
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
