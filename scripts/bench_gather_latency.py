"""Measure per-report gather latency from the agent's side (QNT-300, item B-6).

Times the report tools the graph's ``_gather_reports`` drives -- exactly the
callables ``agent.graph.gather_node`` invokes -- against a live API so we can
decide whether the serial gather loop is worth parallelising (ADR-007 sketch:
"in parallel where possible"). Prints p50/p95 per report kind plus the two
serial totals the ticket calls out:

* thesis gather = company + technical + fundamental + news, run serially
* rich 2-ticker comparison = the same four tools, per ticker (8 serial calls)

Run against a live API (co-located with ClickHouse for prod-representative
numbers -- a tunnelled ClickHouse inflates every call by the SSH round trip):

    API_BASE_URL=http://localhost:8000 uv run python scripts/bench_gather_latency.py
    uv run python scripts/bench_gather_latency.py --iters 30 --ticker NVDA --partner AAPL

Read-only: every tool is an HTTP GET against a report endpoint. No writes.
"""

from __future__ import annotations

import argparse
import time
from statistics import median

from agent.tools import (
    get_company_report,
    get_fundamental_report,
    get_news_report,
    get_technical_report,
)
from shared.config import settings

# The four report kinds the thesis path gathers, keyed by the graph's tool name.
REPORT_TOOLS = {
    "company": get_company_report,
    "technical": get_technical_report,
    "fundamental": get_fundamental_report,
    "news": get_news_report,
}


def _p(values: list[float], q: float) -> float:
    """Nearest-rank percentile in milliseconds."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx] * 1000


def _time_call(fn: object, ticker: str) -> float:
    start = time.perf_counter()
    fn(ticker)  # type: ignore[operator]
    return time.perf_counter() - start


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iters", type=int, default=30, help="timed iterations per tool")
    parser.add_argument("--ticker", default="NVDA", help="primary ticker")
    parser.add_argument("--partner", default="AAPL", help="second comparison ticker")
    args = parser.parse_args()

    print(f"API_BASE_URL = {settings.API_BASE_URL}")
    print(f"ticker={args.ticker} partner={args.partner} iters={args.iters}\n")

    # Warm up once per tool/ticker so first-call TLS/connect cost is excluded.
    for fn in REPORT_TOOLS.values():
        for t in (args.ticker, args.partner):
            fn(t)

    per_tool: dict[str, list[float]] = {name: [] for name in REPORT_TOOLS}
    thesis_totals: list[float] = []
    comparison_totals: list[float] = []

    for _ in range(args.iters):
        thesis_total = 0.0
        for name, fn in REPORT_TOOLS.items():
            dt = _time_call(fn, args.ticker)
            per_tool[name].append(dt)
            thesis_total += dt
        thesis_totals.append(thesis_total)

        # Rich 2-ticker comparison: same four tools serially per ticker.
        comparison_total = 0.0
        for t in (args.ticker, args.partner):
            for fn in REPORT_TOOLS.values():
                comparison_total += _time_call(fn, t)
        comparison_totals.append(comparison_total)

    print(f"{'report kind':<16}{'p50 (ms)':>12}{'p95 (ms)':>12}")
    print("-" * 40)
    for name, values in per_tool.items():
        print(f"{name:<16}{_p(values, 0.50):>12.1f}{_p(values, 0.95):>12.1f}")
    print("-" * 40)
    print(
        f"{'thesis (4 serial)':<16}{_p(thesis_totals, 0.50):>12.1f}{_p(thesis_totals, 0.95):>12.1f}"
    )
    print(
        f"{'compare (8 serial)':<16}"
        f"{_p(comparison_totals, 0.50):>12.1f}{_p(comparison_totals, 0.95):>12.1f}"
    )
    floor = median([v for vs in per_tool.values() for v in vs]) * 1000
    print(f"\nmedian per-call floor: {floor:.1f} ms")


if __name__ == "__main__":
    main()
