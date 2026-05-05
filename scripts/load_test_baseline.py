"""Load-test baseline for the 5 read endpoints (QNT-65).

Hits 5 endpoints x 10 tickers at modest concurrency, computes p50/p95/p99
per endpoint, prints JSON to stdout and a Markdown summary table to stderr.

Demo-protection paths (rate-limit, per-IP token budget, global Groq TPD
breaker, LiteLLM fail-closed) are intentionally NOT exercised here -- they
are covered by tests/api/test_security.py. See docs/guides/load-test-baseline.md
for the rationale.

Usage (against the M4 dev API):
    uv run python scripts/load_test_baseline.py http://localhost:8000

Usage (against the prod api container, eliminates SSH + Caddy hops):
    scp scripts/load_test_baseline.py hetzner:/tmp/
    ssh hetzner "docker cp /tmp/load_test_baseline.py equity-data-agent-api-1:/tmp/ \\
        && docker exec equity-data-agent-api-1 /app/.venv/bin/python \\
        /tmp/load_test_baseline.py http://localhost:8000"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

import httpx

# Hardcoded rather than imported from shared.tickers: this script is invoked
# both inside the prod api container (where `shared` is on the venv) and
# standalone via `uv run`, but the typical invocation is
# `docker exec ... /app/.venv/bin/python /tmp/load_test_baseline.py` where
# the file is /tmp-staged outside the package, so `shared` may not import.
# The meta JSON emits the ticker set, so a re-run diff will surface drift.
TICKERS: list[str] = [
    "NVDA",
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "META",
    "TSLA",
    "JPM",
    "V",
    "UNH",
]
ENDPOINTS: list[str] = ["quote", "ohlcv", "indicators", "fundamentals", "news"]


async def _hit(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    base_url: str,
    endpoint: str,
    ticker: str,
    sink: list[dict[str, Any]],
) -> None:
    async with sem:
        url = f"{base_url}/api/v1/{endpoint}/{ticker}"
        start = time.perf_counter()
        try:
            response = await client.get(url, timeout=30.0)
            elapsed_ms = (time.perf_counter() - start) * 1000
            sink.append(
                {
                    "elapsed_ms": elapsed_ms,
                    "status": response.status_code,
                    "ticker": ticker,
                }
            )
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = (time.perf_counter() - start) * 1000
            sink.append(
                {
                    "elapsed_ms": elapsed_ms,
                    "status": -1,
                    "ticker": ticker,
                    "error": repr(exc),
                }
            )


async def _run(base_url: str, reps: int, concurrency: int) -> dict[str, list[dict[str, Any]]]:
    real: dict[str, list[dict[str, Any]]] = {ep: [] for ep in ENDPOINTS}
    discard: dict[str, list[dict[str, Any]]] = {ep: [] for ep in ENDPOINTS}
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient() as client:
        # One warm-up pass through every (endpoint, ticker) pair -- 50
        # requests, results discarded. Primes ClickHouse query plan cache +
        # Qdrant client + any module-level state so the first measured
        # request isn't a cold-start outlier. Not a full warm of the 500-req
        # measured run; just enough to bypass first-touch costs.
        warm = [
            _hit(client, sem, base_url, ep, t, discard[ep]) for ep in ENDPOINTS for t in TICKERS
        ]
        await asyncio.gather(*warm)

        tasks = [
            _hit(client, sem, base_url, ep, t, real[ep])
            for _ in range(reps)
            for ep in ENDPOINTS
            for t in TICKERS
        ]
        await asyncio.gather(*tasks)

    return real


def _quantile(data: list[float], q: float) -> float:
    """Linear-interpolated quantile. Matches numpy default."""
    sorted_data = sorted(data)
    pos = q * (len(sorted_data) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_data) - 1)
    frac = pos - lo
    return sorted_data[lo] * (1 - frac) + sorted_data[hi] * frac


def _summarise(
    results: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for ep, rows in results.items():
        ok = [r for r in rows if r["status"] == 200]
        latencies = [r["elapsed_ms"] for r in ok]
        summary[ep] = {
            "n": len(rows),
            "ok": len(ok),
            "errors": len(rows) - len(ok),
            "p50_ms": round(_quantile(latencies, 0.50), 1) if latencies else None,
            "p95_ms": round(_quantile(latencies, 0.95), 1) if latencies else None,
            "p99_ms": round(_quantile(latencies, 0.99), 1) if latencies else None,
            "min_ms": round(min(latencies), 1) if latencies else None,
            "max_ms": round(max(latencies), 1) if latencies else None,
        }
    return summary


def _markdown_table(summary: dict[str, dict[str, Any]]) -> str:
    header = (
        "| Endpoint | n | ok | err | p50 ms | p95 ms | p99 ms | min ms | max ms |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    rows = []
    for ep in ENDPOINTS:
        s = summary[ep]
        rows.append(
            f"| `/api/v1/{ep}/{{ticker}}` | {s['n']} | {s['ok']} | {s['errors']} | "
            f"{s['p50_ms']} | {s['p95_ms']} | {s['p99_ms']} | "
            f"{s['min_ms']} | {s['max_ms']} |"
        )
    return header + "\n" + "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="API base URL (e.g., http://localhost:8000)")
    parser.add_argument(
        "--reps",
        type=int,
        default=10,
        help="Repetitions per (endpoint, ticker) pair (default: 10)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=20,
        help="Max concurrent in-flight requests (default: 20)",
    )
    args = parser.parse_args()

    total = len(ENDPOINTS) * len(TICKERS) * args.reps
    print(
        f"# Load test -> {args.base_url}\n"
        f"# {len(ENDPOINTS)} endpoints x {len(TICKERS)} tickers x "
        f"{args.reps} reps = {total} requests, concurrency {args.concurrency}\n"
        f"# Warm-up pass first (results discarded)",
        file=sys.stderr,
    )

    start = time.perf_counter()
    results = asyncio.run(_run(args.base_url, args.reps, args.concurrency))
    duration_s = time.perf_counter() - start

    summary = _summarise(results)
    payload = {
        "summary": summary,
        "meta": {
            "base_url": args.base_url,
            "reps_per_pair": args.reps,
            "concurrency": args.concurrency,
            "tickers": TICKERS,
            "endpoints": ENDPOINTS,
            "total_requests": total,
            "total_duration_s": round(duration_s, 1),
        },
    }

    print(json.dumps(payload, indent=2))
    print("\n" + _markdown_table(summary), file=sys.stderr)

    # Exit non-zero if any endpoint had >5% errors -- catches a wholly broken
    # endpoint without flagging a single transient failure.
    for ep in ENDPOINTS:
        s = summary[ep]
        if s["n"] and s["errors"] / s["n"] > 0.05:
            print(
                f"\nERROR: {ep} had {s['errors']}/{s['n']} failures (>5%)",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
