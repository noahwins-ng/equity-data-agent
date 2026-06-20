"""Multi-corpus routing eval (QNT-263).

The QNT-261 retrieval eval scores retrieval quality *within* a corpus (recall@k
/ MRR / nDCG). It does not measure whether a question is routed to the RIGHT
corpus in the first place -- the senior multi-corpus signal this ticket adds.
This harness scores that routing decision against a curated golden set
(``goldens/routing.yaml``): for each question, ``route_search_corpora`` must
return the expected set of corpora (news and/or earnings, or neither).

The router is DETERMINISTIC and LLM-free (``agent.intent.route_search_corpora``
composes ``_is_targeted_news`` + ``_is_earnings_search``), so this is a
keyword-routing contract that does not drift with the model -- it runs offline
with no Qdrant, no LiteLLM, and is safe in the default pytest sweep
(``tests/agent/evals/test_routing_yaml.py``) as well as standalone for the PR
scorecard.

Example::

    uv run python -m agent.evals.routing_eval
    uv run python -m agent.evals.routing_eval --only nvda-ceo-guidance
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml
from shared.tickers import TICKERS

from agent.intent import route_search_corpora

logger = logging.getLogger(__name__)

ROUTING_GOLDENS_PATH = Path(__file__).parent / "goldens" / "routing.yaml"

# Known corpora a question can route to. An empty expected set is valid (the
# canned digests carry the answer); these are the routing negatives.
CORPORA = ("news", "earnings")

# Coverage floors so the set keeps exercising every routing class -- a single
# class collapsing (e.g. no "both" fixtures) would hide the multi-corpus signal
# the ticket is about.
MIN_NEWS_ONLY = 6
MIN_EARNINGS_ONLY = 6
MIN_BOTH = 3
MIN_NEITHER = 5


@dataclass(frozen=True)
class RoutingFixture:
    """One row from goldens/routing.yaml."""

    id: str
    ticker: str
    question: str
    expected_corpora: frozenset[str]

    @property
    def routing_class(self) -> str:
        has_news = "news" in self.expected_corpora
        has_earn = "earnings" in self.expected_corpora
        if has_news and has_earn:
            return "both"
        if has_news:
            return "news_only"
        if has_earn:
            return "earnings_only"
        return "neither"


@dataclass(frozen=True)
class RoutingOutcome:
    """Router result for one fixture."""

    fixture: RoutingFixture
    actual_corpora: frozenset[str]

    @property
    def ok(self) -> bool:
        return self.actual_corpora == self.fixture.expected_corpora


def load_routing_fixtures(path: Path = ROUTING_GOLDENS_PATH) -> list[RoutingFixture]:
    """Parse + validate the YAML registry into typed fixtures.

    Validates here (unique ids, ticker in TICKERS, known corpora vocabulary,
    per-class coverage floors) so every consumer reads from one authority.
    """
    raw = yaml.safe_load(path.read_text())
    fixtures = raw.get("fixtures") if isinstance(raw, dict) else None
    if not isinstance(fixtures, list):
        raise ValueError(f"{path}: missing top-level `fixtures` list")

    records: list[RoutingFixture] = []
    seen_ids: set[str] = set()
    for entry in fixtures:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each fixture must be a mapping, got {type(entry)}")
        try:
            rec_id = str(entry["id"])
            ticker = str(entry["ticker"])
            question = str(entry["question"])
            corpora_raw = entry["expected_corpora"]
        except KeyError as exc:
            raise ValueError(f"{path}: fixture missing field {exc}") from exc
        if rec_id in seen_ids:
            raise ValueError(f"{path}: duplicate fixture id {rec_id!r}")
        if ticker not in TICKERS:
            raise ValueError(f"{path}: fixture {rec_id!r} references unknown ticker {ticker!r}")
        if not isinstance(corpora_raw, list):
            raise ValueError(f"{path}: fixture {rec_id!r} expected_corpora must be a list")
        corpora = frozenset(str(c) for c in corpora_raw)
        unknown = corpora - set(CORPORA)
        if unknown:
            raise ValueError(f"{path}: fixture {rec_id!r} has unknown corpora {sorted(unknown)}")
        seen_ids.add(rec_id)
        records.append(
            RoutingFixture(id=rec_id, ticker=ticker, question=question, expected_corpora=corpora)
        )

    _check_coverage(path, records)
    return records


def _check_coverage(path: Path, records: list[RoutingFixture]) -> None:
    counts = {"news_only": 0, "earnings_only": 0, "both": 0, "neither": 0}
    for r in records:
        counts[r.routing_class] += 1
    floors = {
        "news_only": MIN_NEWS_ONLY,
        "earnings_only": MIN_EARNINGS_ONLY,
        "both": MIN_BOTH,
        "neither": MIN_NEITHER,
    }
    for cls, floor in floors.items():
        if counts[cls] < floor:
            raise ValueError(f"{path}: {counts[cls]} {cls} fixtures, need at least {floor}")


def evaluate(fixture: RoutingFixture) -> RoutingOutcome:
    """Run the deterministic router and capture the routed corpora."""
    return RoutingOutcome(
        fixture=fixture,
        actual_corpora=frozenset(route_search_corpora(fixture.question)),
    )


@dataclass(frozen=True)
class RoutingReport:
    """Aggregate of one run, returned to the caller and rendered by summarise."""

    outcomes: tuple[RoutingOutcome, ...]

    @property
    def accuracy(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(1 for o in self.outcomes if o.ok) / len(self.outcomes)

    @property
    def misses(self) -> list[RoutingOutcome]:
        return [o for o in self.outcomes if not o.ok]


def run_all(*, only: str | None = None) -> RoutingReport:
    """Run the router over the fixture set and return the aggregate."""
    fixtures = load_routing_fixtures()
    if only is not None:
        fixtures = [f for f in fixtures if f.id == only]
        if not fixtures:
            raise ValueError(f"no routing fixture with id {only!r}")
    return RoutingReport(outcomes=tuple(evaluate(f) for f in fixtures))


def is_failing(report: RoutingReport) -> bool:
    """Hard gate: any misrouted fixture fails (deterministic, so a miss is a bug).

    Empty input fails too -- a malformed stub that strips every fixture must not
    masquerade as a clean pass.
    """
    if not report.outcomes:
        return True
    return bool(report.misses)


def _fmt_corpora(corpora: frozenset[str]) -> str:
    return ",".join(sorted(corpora)) if corpora else "(none)"


def summarise(report: RoutingReport) -> str:
    """Human-readable per-fixture + aggregate scorecard for stdout / the PR."""
    total = len(report.outcomes)
    correct = sum(1 for o in report.outcomes if o.ok)
    lines = [
        "ROUTING EVAL (deterministic route_search_corpora; offline, LLM-free)",
        f"  accuracy: {correct}/{total} ({report.accuracy:.0%})  misses: {len(report.misses)}",
    ]
    for o in report.outcomes:
        mark = "ok" if o.ok else "MISROUTED"
        lines.append(
            f"    [{mark:9s}] {o.fixture.id:22s} {o.fixture.routing_class:13s} "
            f"expected={_fmt_corpora(o.fixture.expected_corpora):16s} "
            f"actual={_fmt_corpora(o.actual_corpora)}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.evals.routing_eval")
    parser.add_argument("--only", help="Run only one fixture id")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    try:
        report = run_all(only=args.only)
    except ValueError as exc:
        print(f"SKIPPED: {exc}", file=sys.stderr)
        return 2

    print(summarise(report))
    return 1 if is_failing(report) else 0


__all__ = [
    "CORPORA",
    "MIN_BOTH",
    "MIN_EARNINGS_ONLY",
    "MIN_NEITHER",
    "MIN_NEWS_ONLY",
    "ROUTING_GOLDENS_PATH",
    "RoutingFixture",
    "RoutingOutcome",
    "RoutingReport",
    "evaluate",
    "is_failing",
    "load_routing_fixtures",
    "run_all",
    "summarise",
]


if __name__ == "__main__":
    sys.exit(main())
