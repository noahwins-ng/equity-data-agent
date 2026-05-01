"""Agent CLI: drive the LangGraph plan -> gather -> synthesize flow against
a single ticker and print the resulting thesis.

    uv run python -m agent analyze NVDA
    uv run python -m agent analyze NVDA --output thesis.md

Replaces the QNT-59 proof-of-life stub. Exit codes:
    0  thesis produced
    1  unknown ticker, graph short-circuit (no reports gathered), or unhandled error
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from shared.tickers import TICKERS

from agent.graph import build_graph
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from agent.tools import default_report_tools
from agent.tracing import langfuse, observe

logger = logging.getLogger(__name__)


@observe()
def analyze(ticker: str, output: Path | None = None) -> int:
    ticker = ticker.upper()
    if ticker not in TICKERS:
        print(
            f"Unknown ticker: {ticker}. Known: {', '.join(sorted(TICKERS))}",
            file=sys.stderr,
        )
        return 1

    graph = build_graph(default_report_tools())
    final_state = graph.invoke({"ticker": ticker})

    thesis_obj = final_state.get("thesis")
    quick_fact_obj = final_state.get("quick_fact")
    intent = final_state.get("intent", "thesis")
    confidence = final_state.get("confidence", 0.0)
    errors = final_state.get("errors") or {}

    if errors:
        for name, err in errors.items():
            print(f"[warn] {name}: {err}", file=sys.stderr)

    # QNT-149: render whichever shape the synthesize node populated. The
    # CLI keeps its plain-markdown stdout contract — quick-fact path renders
    # short, thesis path renders the four sections — so callers piping to
    # files don't have to branch on intent.
    if intent == "quick_fact" and isinstance(quick_fact_obj, QuickFactAnswer):
        rendered = quick_fact_obj.to_markdown().strip()
    elif isinstance(thesis_obj, Thesis):
        rendered = thesis_obj.to_markdown().strip()
    else:
        rendered = ""

    if not rendered:
        print(f"No answer produced for {ticker} (no reports gathered).", file=sys.stderr)
        return 1

    print(rendered)
    print(f"\n[intent={intent} confidence={confidence}]", file=sys.stderr)

    if output is not None:
        try:
            output.write_text(rendered + "\n")
        except OSError as exc:
            print(f"[error] cannot write {output}: {exc}", file=sys.stderr)
            return 1
        print(f"Wrote answer to {output}", file=sys.stderr)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_analyze = sub.add_parser("analyze", help="Analyze a single ticker")
    p_analyze.add_argument("ticker")
    p_analyze.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Write the thesis to this file in addition to stdout.",
    )
    args = parser.parse_args(argv)
    try:
        if args.cmd == "analyze":
            return analyze(args.ticker, output=args.output)
        return 1
    except Exception:
        logger.exception("agent analyze failed")
        return 1
    finally:
        langfuse.flush()


if __name__ == "__main__":
    sys.exit(main())
