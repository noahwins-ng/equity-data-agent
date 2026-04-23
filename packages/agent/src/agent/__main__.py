"""Minimal CLI proof-of-life for QNT-59. QNT-60 replaces this with the full
LangGraph plan → gather → synthesize flow; until then `analyze <TICKER>`
exercises the LiteLLM routing (Groq default, Gemini override via env)."""

import argparse
import sys

from shared.tickers import TICKERS

from agent.llm import get_llm


def analyze(ticker: str) -> int:
    ticker = ticker.upper()
    if ticker not in TICKERS:
        print(
            f"Unknown ticker: {ticker}. Known: {', '.join(sorted(TICKERS))}",
            file=sys.stderr,
        )
        return 2

    llm = get_llm()
    response = llm.invoke(
        f"Write a one-paragraph investment hypothesis for {ticker}. "
        "Do not fabricate numbers - speak in qualitative terms only."
    )
    print(response.content)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_analyze = sub.add_parser("analyze", help="Analyze a single ticker")
    p_analyze.add_argument("ticker")
    args = parser.parse_args(argv)
    if args.cmd == "analyze":
        return analyze(args.ticker)
    return 1


if __name__ == "__main__":
    sys.exit(main())
