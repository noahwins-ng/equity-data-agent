"""Per-node + per-turn token/latency baseline from the Langfuse REST API (QNT-219).

Reproducible replacement for the throwaway script that produced the 14-day
baseline in ``docs/equity-analyst-improvement-v4.md``. Pulls GENERATION
observations over an arbitrary window and prints two tables:

* **Per node** (``metadata.langgraph_node``): count, latency mean/p50/max
  (seconds), mean input/output tokens.
* **Per turn** (grouped by ``traceId``): turn-type label, count, mean total
  tokens, mean end-to-end wall-clock, and the wall-minus-summed-LLM-latency gap
  (the gather/overhead estimate).

This is the before/after instrument for QNT-220. It is **read-only** — it issues
``GET`` requests against ``/api/public/observations`` and adds zero new Langfuse
spans or observations (ADR-019 holds).

Examples::

    uv run python -m agent.evals.langfuse_baseline                 # trailing 14 days
    uv run python -m agent.evals.langfuse_baseline --days 3
    uv run python -m agent.evals.langfuse_baseline --from 2026-05-19 --to 2026-06-02

Region trap (QNT-61): ``LANGFUSE_BASE_URL`` must be the US host
(``https://us.cloud.langfuse.com``) or the API returns an empty result set with
no auth error.
"""

from __future__ import annotations

import argparse
import base64
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from shared.config import settings

# langgraph_node values that are LLM calls (gather hits HTTP tools, not the LLM,
# so it never appears as a GENERATION observation). Ordering mirrors the v4 doc.
_NODE_ORDER = [
    "classify",
    "plan",
    "synthesize",
    "narrate",
    "explore_supervisor",
    "clarify",
]

# Turn-type label by node-set, highest precedence first. Mirrors the v4 doc:
# a turn that planned a thesis is "thesis", a turn that only answered from cache
# (synthesize/narrate, no plan) is "short-circuit", and a turn with neither an
# answer node nor a routing node is "other".
_TURN_TYPE_ORDER = ["exploration", "clarify", "thesis", "short-circuit", "other"]

_PAGE_LIMIT = 50
_REQUEST_TIMEOUT = 30.0


@dataclass
class _NodeStats:
    latencies: list[float] = field(default_factory=list)
    in_tokens: list[int] = field(default_factory=list)
    out_tokens: list[int] = field(default_factory=list)


@dataclass
class _Turn:
    nodes: set[str] = field(default_factory=set)
    total_tokens: int = 0
    sum_latency: float = 0.0
    start: datetime | None = None
    end: datetime | None = None


def _parse_ts(value: str) -> datetime:
    """Parse a Langfuse ISO timestamp (``...Z``) into an aware datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _tokens(obs: dict) -> tuple[int, int]:
    """Extract (input, output) tokens, tolerating the API's redundant fields."""
    details = obs.get("usageDetails") or obs.get("usage") or {}
    in_tok = details.get("input")
    out_tok = details.get("output")
    if in_tok is None:
        in_tok = obs.get("promptTokens")
    if out_tok is None:
        out_tok = obs.get("completionTokens")
    return int(in_tok or 0), int(out_tok or 0)


def _label_turn(nodes: set[str]) -> str:
    if "explore_supervisor" in nodes:
        return "exploration"
    if "clarify" in nodes:
        return "clarify"
    if "plan" in nodes:
        return "thesis"
    if "synthesize" in nodes or "narrate" in nodes:
        return "short-circuit"
    return "other"


def fetch_generations(
    *,
    from_time: datetime,
    to_time: datetime,
    public_key: str,
    secret_key: str,
    base_url: str,
) -> list[dict]:
    """Page through all GENERATION observations in the window (read-only)."""
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}
    url = f"{base_url.rstrip('/')}/api/public/observations"
    rows: list[dict] = []
    page = 1
    with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
        while True:
            resp = client.get(
                url,
                headers=headers,
                params={
                    "type": "GENERATION",
                    "fromStartTime": from_time.isoformat(),
                    "toStartTime": to_time.isoformat(),
                    "page": page,
                    "limit": _PAGE_LIMIT,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            batch = body.get("data") or []
            rows.extend(batch)
            total_pages = (body.get("meta") or {}).get("totalPages") or 0
            if page >= total_pages or not batch:
                break
            page += 1
    return rows


def aggregate(rows: list[dict]) -> tuple[dict[str, _NodeStats], dict[str, _Turn]]:
    """Fold observations into per-node and per-turn accumulators."""
    nodes: dict[str, _NodeStats] = {}
    turns: dict[str, _Turn] = {}
    for obs in rows:
        node = (obs.get("metadata") or {}).get("langgraph_node") or "unknown"
        in_tok, out_tok = _tokens(obs)
        latency = float(obs.get("latency") or 0.0)

        ns = nodes.setdefault(node, _NodeStats())
        ns.latencies.append(latency)
        ns.in_tokens.append(in_tok)
        ns.out_tokens.append(out_tok)

        trace_id = obs.get("traceId")
        if trace_id is None:
            continue
        turn = turns.setdefault(trace_id, _Turn())
        turn.nodes.add(node)
        turn.total_tokens += in_tok + out_tok
        turn.sum_latency += latency
        start = _parse_ts(obs["startTime"]) if obs.get("startTime") else None
        end = _parse_ts(obs["endTime"]) if obs.get("endTime") else None
        if start is not None and (turn.start is None or start < turn.start):
            turn.start = start
        if end is not None and (turn.end is None or end > turn.end):
            turn.end = end
    return nodes, turns


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _print_node_table(nodes: dict[str, _NodeStats]) -> None:
    print("\nPer-node (latency seconds, tokens mean per call):")
    header = (
        f"{'node':<20}{'n':>6}{'lat mean':>10}{'lat p50':>10}"
        f"{'lat max':>10}{'in tok':>9}{'out tok':>9}"
    )
    print(header)
    print("-" * len(header))
    ordered = _NODE_ORDER + sorted(k for k in nodes if k not in _NODE_ORDER)
    for node in ordered:
        ns = nodes.get(node)
        if ns is None or not ns.latencies:
            continue
        print(
            f"{node:<20}{len(ns.latencies):>6}"
            f"{_mean(ns.latencies):>10.1f}"
            f"{statistics.median(ns.latencies):>10.1f}"
            f"{max(ns.latencies):>10.1f}"
            f"{round(_mean(ns.in_tokens)):>9}"
            f"{round(_mean(ns.out_tokens)):>9}"
        )


def _print_turn_table(turns: dict[str, _Turn]) -> None:
    print("\nPer-turn (grouped by traceId):")
    header = f"{'turn type':<16}{'n':>6}{'tok mean':>10}{'wall mean':>11}{'gap mean':>10}"
    print(header)
    print("-" * len(header))
    by_type: dict[str, list[_Turn]] = {}
    for turn in turns.values():
        by_type.setdefault(_label_turn(turn.nodes), []).append(turn)
    for label in _TURN_TYPE_ORDER:
        bucket = by_type.get(label)
        if not bucket:
            continue
        walls = [
            (t.end - t.start).total_seconds()
            for t in bucket
            if t.start is not None and t.end is not None
        ]
        gaps = [
            (t.end - t.start).total_seconds() - t.sum_latency
            for t in bucket
            if t.start is not None and t.end is not None
        ]
        toks = [t.total_tokens for t in bucket]
        print(
            f"{label:<16}{len(bucket):>6}"
            f"{round(_mean(toks)):>10}"
            f"{_mean(walls):>10.1f}s"
            f"{_mean(gaps):>9.1f}s"
        )
    print(f"\nTotal turns: {len(turns)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.evals.langfuse_baseline")
    parser.add_argument("--days", type=int, default=14, help="Trailing window size (default: 14)")
    parser.add_argument("--from", dest="from_date", help="Window start (YYYY-MM-DD, UTC)")
    parser.add_argument("--to", dest="to_date", help="Window end (YYYY-MM-DD, UTC)")
    args = parser.parse_args(argv)

    if not (settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY):
        print(
            "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set — skipping baseline.",
            file=sys.stderr,
        )
        return 0

    if args.to_date:
        to_time = datetime.strptime(args.to_date, "%Y-%m-%d").replace(tzinfo=UTC)
    else:
        to_time = datetime.now(UTC)
    if args.from_date:
        from_time = datetime.strptime(args.from_date, "%Y-%m-%d").replace(tzinfo=UTC)
    else:
        from_time = to_time - timedelta(days=args.days)

    print(f"Window: {from_time.date()} -> {to_time.date()}  ({settings.LANGFUSE_BASE_URL})")
    rows = fetch_generations(
        from_time=from_time,
        to_time=to_time,
        public_key=settings.LANGFUSE_PUBLIC_KEY,
        secret_key=settings.LANGFUSE_SECRET_KEY,
        base_url=settings.LANGFUSE_BASE_URL,
    )
    if not rows:
        print("No GENERATION observations in window (check the region/host trap).")
        return 0

    nodes, turns = aggregate(rows)
    print(f"\n{len(rows)} generations / {len(turns)} turns")
    _print_node_table(nodes)
    _print_turn_table(turns)
    return 0


if __name__ == "__main__":
    sys.exit(main())
