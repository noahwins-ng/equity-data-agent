"""Multi-corpus routing eval (QNT-263; made semantic in QNT-280).

The QNT-261 retrieval eval scores retrieval quality *within* a corpus (recall@k
/ MRR / nDCG). It does not measure whether a question is routed to the RIGHT
corpus in the first place -- the senior multi-corpus signal. This harness scores
that routing decision against a curated golden set (``goldens/routing.yaml``):
for each question, the composed corpus set must equal the fixture's
``expected_corpora`` (news and/or earnings, or neither).

QNT-280 moved the trigger from deterministic keyword gates to a SEMANTIC flag
carried by the classify LLM, so this eval is now LIVE: it calls the real
``classify_intent_with_source`` (the same entrypoint the agent's classify_node
uses) to resolve ``needs_news_search`` / ``needs_earnings_search``, then composes
them with ``route_search_corpora`` -- exactly the runtime path. That is what
keeps "what the eval scores == what the agent does" true now that the decision
depends on the model. The accuracy floor (``ACCURACY_FLOOR``) is the asserted
gate; ``resolved_intent`` is captured per fixture so an intent-label drift shows
up alongside the routing miss.

Because it fires the live classifier, it needs LiteLLM (not Qdrant -- no
retrieval here) and is NOT collected by pytest; the offline structural +
keyword-soundness invariants live in ``tests/agent/evals/test_routing_yaml.py``.
Respect the clean-rate-limit-window rule (Groq TPD for the classifier) before
publishing baseline numbers -- contamination is flagged via per-fixture latency.

Example::

    uv run python -m agent.evals.routing_eval
    uv run python -m agent.evals.routing_eval --only nvda-ceo-guidance
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml
from shared.config import settings
from shared.tickers import TICKERS

from agent.intent import classify_intent_with_source, route_search_corpora

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

# QNT-280 AC2: the asserted accuracy floor. The decision is now the live small
# model OR-ed with the keyword floor; exact composite-set match across all
# routing classes must clear this, or the run fails. Tuned to leave headroom for
# small-model variance while still catching a real regression (e.g. the semantic
# flag silently reverting to the keyword floor, which would miss every topical
# positive).
ACCURACY_FLOOR = 0.80

# A live classify call (one small structured call, or a heuristic short-circuit)
# returns in a few seconds on a clean window. A fixture clearing this floor means
# a classifier call ran to its timeout ceiling -- the Groq-throttle signature.
# Mirrors news_search_eval / golden_set.
CONTAMINATION_LATENCY_MS = int(settings.LLM_REQUEST_TIMEOUT * 1000)


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
    resolved_intent: str
    classifier_source: str
    elapsed_ms: int

    @property
    def ok(self) -> bool:
        return self.actual_corpora == self.fixture.expected_corpora

    @property
    def false_positive(self) -> bool:
        """A 'neither' fixture that wrongly fired a corpus -- the gated direction.

        Firing RAG on a generic / single-metric / off-domain ask drops the canned
        digest and degrades the answer (the same failure news_search_eval gates).
        """
        return not self.fixture.expected_corpora and bool(self.actual_corpora)


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
    """Resolve the live search flags and compose them into the routed corpora.

    Calls the same ``classify_intent_with_source`` entrypoint the agent's
    classify_node uses, then composes the two flags with ``route_search_corpora``
    -- the runtime path, so the score reflects what the agent actually fires.
    """
    started = time.perf_counter()
    intent, source, needs_news_search, needs_earnings_search = classify_intent_with_source(
        fixture.question
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return RoutingOutcome(
        fixture=fixture,
        actual_corpora=frozenset(route_search_corpora(needs_news_search, needs_earnings_search)),
        resolved_intent=intent,
        classifier_source=source,
        elapsed_ms=elapsed_ms,
    )


def precheck_environment(*, timeout: float = 5.0) -> None:
    """Raise if the LiteLLM proxy is unreachable.

    The eval now fires the live classifier, so it needs LiteLLM. A reachable
    HTTP response (any status -- even 404 proves the server is up) clears the
    check; a connection error fails it before a single token is spent.
    """
    base_url = settings.LITELLM_BASE_URL
    try:
        httpx.get(base_url, timeout=timeout)
    except httpx.HTTPError as exc:
        raise RuntimeError(
            "routing eval precheck failed -- start the LiteLLM proxy first "
            f"(make dev-litellm): LiteLLM proxy unreachable at {base_url} "
            f"({type(exc).__name__})"
        ) from exc


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

    @property
    def false_positives(self) -> list[RoutingOutcome]:
        return [o for o in self.outcomes if o.false_positive]


def run_all(*, only: str | None = None, skip_precheck: bool = False) -> RoutingReport:
    """Run the live router over the fixture set and return the aggregate."""
    if not skip_precheck:
        precheck_environment()
    fixtures = load_routing_fixtures()
    if only is not None:
        fixtures = [f for f in fixtures if f.id == only]
        if not fixtures:
            raise ValueError(f"no routing fixture with id {only!r}")
    return RoutingReport(outcomes=tuple(evaluate(f) for f in fixtures))


def contamination_warning(report: RoutingReport) -> str | None:
    """Flag a run contaminated by Groq throttling (latency signal).

    A fixture whose wall time cleared one full LLM timeout ceiling means a
    classifier call ran to its timeout -- the throttle signature. Re-run on a
    clean rate-limit window before trusting the numbers.
    """
    slow = [o for o in report.outcomes if o.elapsed_ms >= CONTAMINATION_LATENCY_MS]
    if not slow:
        return None
    return (
        f"CONTAMINATED RUN -- do not trust this aggregate. {len(slow)} fixture(s) "
        f"over the {CONTAMINATION_LATENCY_MS}ms timeout-ceiling floor "
        "(likely Groq throttling): "
        + ", ".join(f"{o.fixture.id}={o.elapsed_ms}ms" for o in slow)
        + ". Re-run on a clean rate-limit window before publishing baseline numbers."
    )


def is_failing(report: RoutingReport) -> bool:
    """Hard gate (QNT-280 AC2): accuracy below the floor, OR any generic ask that
    wrongly fired a corpus (the gated false-positive direction).

    Empty input fails too -- a malformed stub that strips every fixture must not
    masquerade as a clean pass. Individual misses on targeted fixtures are NOT
    gated one-by-one (the small model has some variance), but the aggregate must
    clear ``ACCURACY_FLOOR`` and no generic fixture may fire RAG.
    """
    if not report.outcomes:
        return True
    if report.false_positives:
        return True
    return report.accuracy < ACCURACY_FLOOR


def _fmt_corpora(corpora: frozenset[str]) -> str:
    return ",".join(sorted(corpora)) if corpora else "(none)"


def summarise(report: RoutingReport) -> str:
    """Human-readable per-fixture + aggregate scorecard for stdout / the PR."""
    lines: list[str] = []
    warning = contamination_warning(report)
    if warning is not None:
        lines += [warning, ""]

    total = len(report.outcomes)
    correct = sum(1 for o in report.outcomes if o.ok)
    floor_mark = "PASS" if report.accuracy >= ACCURACY_FLOOR else "BELOW FLOOR"
    lines += [
        "ROUTING EVAL (live route_search_corpora over semantic classify flags + keyword floor)",
        f"  accuracy: {correct}/{total} ({report.accuracy:.0%})  "
        f"floor: {ACCURACY_FLOOR:.0%} [{floor_mark}]  "
        f"misses: {len(report.misses)}  false_positives: {len(report.false_positives)}",
    ]
    for o in report.outcomes:
        mark = "ok" if o.ok else ("FALSE-POSITIVE" if o.false_positive else "MISROUTED")
        lines.append(
            f"    [{mark:14s}] {o.fixture.id:22s} {o.fixture.routing_class:13s} "
            f"expected={_fmt_corpora(o.fixture.expected_corpora):16s} "
            f"actual={_fmt_corpora(o.actual_corpora):16s} "
            f"intent={o.resolved_intent}/{o.classifier_source} {o.elapsed_ms}ms"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.evals.routing_eval")
    parser.add_argument("--only", help="Run only one fixture id")
    parser.add_argument(
        "--skip-precheck",
        action="store_true",
        help="Skip the LiteLLM reachability precheck (offline/testing only).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    try:
        report = run_all(only=args.only, skip_precheck=args.skip_precheck)
    except RuntimeError as exc:
        # Precheck failure -- LiteLLM down. Skip gracefully (exit 2) rather than
        # report every fixture as a routing failure.
        print(f"SKIPPED: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"SKIPPED: {exc}", file=sys.stderr)
        return 2

    print(summarise(report))
    return 1 if is_failing(report) else 0


__all__ = [
    "ACCURACY_FLOOR",
    "CONTAMINATION_LATENCY_MS",
    "CORPORA",
    "MIN_BOTH",
    "MIN_EARNINGS_ONLY",
    "MIN_NEITHER",
    "MIN_NEWS_ONLY",
    "ROUTING_GOLDENS_PATH",
    "RoutingFixture",
    "RoutingOutcome",
    "RoutingReport",
    "contamination_warning",
    "evaluate",
    "is_failing",
    "load_routing_fixtures",
    "precheck_environment",
    "run_all",
    "summarise",
]


if __name__ == "__main__":
    sys.exit(main())
