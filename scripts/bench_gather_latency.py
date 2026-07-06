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


def _gather_compare(ticker: str, partner: str, iters: int) -> None:
    """QNT-321 (G-3, AC4): before/after on the rich 2-ticker comparison gather.

    Times the exact orchestration change against the SAME live report tools
    (QNT-300's method): the OLD serial per-ticker loop (``_gather_reports`` once
    per ticker = two parallel batches) versus the NEW shared-pool
    ``_gather_reports_multi`` (all 8 ``(ticker, tool)`` pairs on one pool capped
    at 4). Both hold the same max-4-in-flight bound; the delta is purely the
    cross-ticker overlap the shared pool unlocks."""
    from agent.support import _gather_reports, _gather_reports_multi

    plan = list(REPORT_TOOLS)
    tickers = [ticker, partner]

    def _serial() -> None:
        for t in tickers:
            _gather_reports(t, plan, REPORT_TOOLS)  # type: ignore[arg-type]

    def _shared() -> None:
        _gather_reports_multi(tickers, plan, REPORT_TOOLS)  # type: ignore[arg-type]

    # Warm once so first-call TLS/connect cost is excluded from both.
    _serial()
    _shared()

    before: list[float] = []
    after: list[float] = []
    for _ in range(iters):
        s = time.perf_counter()
        _serial()
        before.append(time.perf_counter() - s)
        s = time.perf_counter()
        _shared()
        after.append(time.perf_counter() - s)

    print(f"{'gather (2-ticker)':<22}{'p50 (ms)':>12}{'p95 (ms)':>12}")
    print("-" * 46)
    print(f"{'BEFORE serial loop':<22}{_p(before, 0.50):>12.1f}{_p(before, 0.95):>12.1f}")
    print(f"{'AFTER shared pool':<22}{_p(after, 0.50):>12.1f}{_p(after, 0.95):>12.1f}")
    speedup = _p(before, 0.50) / _p(after, 0.50) if _p(after, 0.50) else 0.0
    print(f"\np50 speedup: {speedup:.2f}x")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iters", type=int, default=30, help="timed iterations per tool")
    parser.add_argument("--ticker", default="NVDA", help="primary ticker")
    parser.add_argument("--partner", default="AAPL", help="second comparison ticker")
    parser.add_argument(
        "--gather-compare",
        action="store_true",
        help="QNT-321 G-3: before/after the shared-pool comparison gather (skips per-tool table)",
    )
    args = parser.parse_args()

    print(f"API_BASE_URL = {settings.API_BASE_URL}")
    print(f"ticker={args.ticker} partner={args.partner} iters={args.iters}\n")

    if args.gather_compare:
        # Warm up once per tool/ticker so first-call TLS/connect cost is excluded.
        for fn in REPORT_TOOLS.values():
            for t in (args.ticker, args.partner):
                fn(t)
        _gather_compare(args.ticker, args.partner, args.iters)
        return

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
