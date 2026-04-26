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
    confidence = final_state.get("confidence", 0.0)
    errors = final_state.get("errors") or {}

    if errors:
        for name, err in errors.items():
            print(f"[warn] {name}: {err}", file=sys.stderr)

    # ``thesis`` is a structured ``Thesis`` since QNT-133 — re-render to
    # markdown for stdout / ``--output`` so the CLI keeps the legacy contract
    # (a plain markdown file). The structured form remains accessible via the
    # graph's state for callers that want JSON (API, frontend).
    thesis_md = thesis_obj.to_markdown().strip() if isinstance(thesis_obj, Thesis) else ""

    if not thesis_md:
        print(f"No thesis produced for {ticker} (no reports gathered).", file=sys.stderr)
        return 1

    print(thesis_md)
    print(f"\n[confidence={confidence}]", file=sys.stderr)

    if output is not None:
        try:
            output.write_text(thesis_md + "\n")
        except OSError as exc:
            print(f"[error] cannot write {output}: {exc}", file=sys.stderr)
            return 1
        print(f"Wrote thesis to {output}", file=sys.stderr)

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
