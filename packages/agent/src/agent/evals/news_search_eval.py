"""RAG news-search eval harness (QNT-231).

The QNT-222/225/226 semantic-news-search arc shipped with no eval coverage:
the unit tests verify the plumbing (flag propagation, hit folding, provenance
parsing) with mocked LLMs, but nothing measured whether the *firing decision*
and the *retrieval* are correct against the live system. This harness adds two
measured layers over a curated golden set (``goldens/news_search.yaml``):

* **Flag layer** -- the ``needs_news_search`` returned by
  ``classify_intent_with_source(question)`` must equal the fixture's
  ``expected_news_search``. The dangerous direction is a FALSE POSITIVE on a
  generic ask: the targeted-news path drops the focused card (QNT-226
  narrative-only shape), so firing RAG on "what's the news on AAPL?" degrades
  the whole answer. That is the hard gate here.

  IMPORTANT -- QNT-280 made the flag SEMANTIC. ``classify_intent_with_source``
  now returns the classify LLM's ``needs_news_search`` OR-ed with the
  ``_is_targeted_news`` keyword floor (the keyword decider was demoted from the
  sole gate to a recall floor). So "flag accuracy" measures the live small model
  on the heuristic-abstain cases -- it WILL move with the model, which is the
  point: topical/competitive phrasings ("the latest on Nvidia in the data center
  switching market") that no keyword token covers now fire via the LLM. A
  positive fixture that the keyword floor cannot reach is the load-bearing test
  of the new semantic path.

* **Retrieval layer** (positives only) -- ``search_news(ticker, question)``
  must return at least one hit whose headline or body contains one of the
  fixture's ``expected_terms``. This is the ticket's main design question: the
  7-day news window rolls, so a frozen-headline assertion would go stale daily.
  We assert STRUCTURAL relevance (a term match against the live corpus)
  instead, and treat retrieval as a REPORTED metric, never a gate -- a miss can
  mean a real recall gap OR simply that no such story is in this week's corpus.
  Improving recall is a follow-up (out of scope per the ticket).

Standalone, like ``dialogue_eval`` -- it needs the tunnel + live Qdrant +
LiteLLM, so it is not collected by pytest and does not run in the default unit
sweep (offline fixture validation lives in
``tests/agent/evals/test_news_search_yaml.py``). Respect the clean-rate-limit
window rule (Groq TPD for the classifier, Qdrant quota for search) before
publishing baseline numbers -- ``contamination`` is flagged via per-fixture
latency the same way the other live evals flag it.

Examples::

    uv run python -m agent.evals.news_search_eval
    uv run python -m agent.evals.news_search_eval --only nvda-litigation
    uv run python -m agent.evals.news_search_eval --flag-only
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml
from shared.config import settings
from shared.tickers import TICKERS

from agent.intent import classify_intent_with_source
from agent.tools import search_news

logger = logging.getLogger(__name__)

NEWS_SEARCH_GOLDENS_PATH = Path(__file__).parent / "goldens" / "news_search.yaml"

MIN_POSITIVES = 10
MIN_NEGATIVES = 5

# A live classify call (heuristic or one small-model structured call) plus one
# Qdrant search returns in a few seconds on a clean window. A fixture whose wall
# time clears this floor means a classifier LLM call ran to its timeout ceiling
# -- the signature of Groq throttling, not a slow fixture. Mirrors the
# CONTAMINATION_LATENCY_MS rationale in golden_set / dialogue_eval.
CONTAMINATION_LATENCY_MS = int(settings.LLM_REQUEST_TIMEOUT * 1000)


@dataclass(frozen=True)
class NewsSearchFixture:
    """One row from goldens/news_search.yaml."""

    id: str
    ticker: str
    question: str
    expected_news_search: bool
    expected_terms: tuple[str, ...]


@dataclass(frozen=True)
class FlagOutcome:
    """Flag-layer result for one fixture."""

    fixture: NewsSearchFixture
    actual_news_search: bool
    resolved_intent: str
    classifier_source: str
    elapsed_ms: int

    @property
    def ok(self) -> bool:
        return self.actual_news_search == self.fixture.expected_news_search

    @property
    def false_positive(self) -> bool:
        """A negative that wrongly fired the search -- the gated direction."""
        return not self.fixture.expected_news_search and self.actual_news_search


@dataclass(frozen=True)
class RetrievalOutcome:
    """Retrieval-layer result for one positive fixture."""

    fixture: NewsSearchFixture
    hit_count: int
    matched_terms: tuple[str, ...]
    elapsed_ms: int

    @property
    def status(self) -> str:
        if self.matched_terms:
            return "match"
        if self.hit_count == 0:
            return "empty"
        return "no_match"


def load_news_search_fixtures(
    path: Path = NEWS_SEARCH_GOLDENS_PATH,
) -> list[NewsSearchFixture]:
    """Parse + validate the YAML registry into typed fixtures.

    Validates here (unique ids, ticker in TICKERS, positive/negative floors,
    positives carry expected_terms) so every consumer reads from one authority.
    """
    raw = yaml.safe_load(path.read_text())
    fixtures = raw.get("fixtures") if isinstance(raw, dict) else None
    if not isinstance(fixtures, list):
        raise ValueError(f"{path}: missing top-level `fixtures` list")

    records: list[NewsSearchFixture] = []
    seen_ids: set[str] = set()
    for entry in fixtures:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each fixture must be a mapping, got {type(entry)}")
        try:
            rec_id = str(entry["id"])
            ticker = str(entry["ticker"])
            question = str(entry["question"])
            expected = bool(entry["expected_news_search"])
        except KeyError as exc:
            raise ValueError(f"{path}: fixture missing field {exc}") from exc
        if rec_id in seen_ids:
            raise ValueError(f"{path}: duplicate fixture id {rec_id!r}")
        if ticker not in TICKERS:
            raise ValueError(f"{path}: fixture {rec_id!r} references unknown ticker {ticker!r}")
        terms = tuple(str(t) for t in entry.get("expected_terms", []))
        if expected and not terms:
            raise ValueError(
                f"{path}: positive fixture {rec_id!r} must list expected_terms "
                "for the retrieval-relevance assertion"
            )
        if not expected and terms:
            raise ValueError(
                f"{path}: negative fixture {rec_id!r} must NOT carry expected_terms "
                "(negatives do not fire the search)"
            )
        seen_ids.add(rec_id)
        records.append(
            NewsSearchFixture(
                id=rec_id,
                ticker=ticker,
                question=question,
                expected_news_search=expected,
                expected_terms=terms,
            )
        )

    positives = sum(1 for r in records if r.expected_news_search)
    negatives = len(records) - positives
    if positives < MIN_POSITIVES:
        raise ValueError(f"{path}: {positives} positives, need at least {MIN_POSITIVES}")
    if negatives < MIN_NEGATIVES:
        raise ValueError(f"{path}: {negatives} negatives, need at least {MIN_NEGATIVES}")
    return records


def evaluate_flag(fixture: NewsSearchFixture) -> FlagOutcome:
    """Run the live classifier and capture the needs_news_search flag."""
    started = time.perf_counter()
    # QNT-327: trailing *_ absorbs the folded report_picks / plan_rationale.
    intent, source, needs_news_search, _needs_earnings_search, _search_query, *_ = (
        classify_intent_with_source(fixture.question)
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return FlagOutcome(
        fixture=fixture,
        actual_news_search=needs_news_search,
        resolved_intent=intent,
        classifier_source=source,
        elapsed_ms=elapsed_ms,
    )


def evaluate_retrieval(fixture: NewsSearchFixture) -> RetrievalOutcome:
    """Run live semantic search and match returned hits against expected_terms.

    A hit matches if any ``expected_term`` is a case-insensitive substring of
    its headline or body. ``search_news`` degrades to ``"[]"`` on any failure
    (Qdrant outage, HTTP error, no matches), so an unreachable corpus reads as
    an empty result rather than raising -- the run still completes and reports
    the miss.
    """
    started = time.perf_counter()
    raw = search_news(fixture.ticker, fixture.question)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    try:
        hits = json.loads(raw)
    except ValueError:
        hits = []
    if not isinstance(hits, list):
        hits = []

    terms = [t.lower() for t in fixture.expected_terms]
    matched: list[str] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        haystack = f"{hit.get('headline') or ''} {hit.get('body') or ''}".lower()
        for term in terms:
            if term and term in haystack and term not in matched:
                matched.append(term)
    return RetrievalOutcome(
        fixture=fixture,
        hit_count=len(hits),
        matched_terms=tuple(matched),
        elapsed_ms=elapsed_ms,
    )


def precheck_environment(*, timeout: float = 5.0) -> None:
    """Raise if the LiteLLM proxy or report API is unreachable.

    The flag layer needs LiteLLM (live classifier) and the retrieval layer
    needs the report API (which proxies Qdrant). A reachable HTTP response (any
    status -- even 404 proves the server is up) clears the check; a connection
    error fails it before a single token is spent. Qdrant being down behind a
    reachable API is NOT a precheck failure -- the API maps that to an empty
    200 (QNT-55) and the retrieval layer reports the empties.
    """
    targets = {
        "LiteLLM proxy": settings.LITELLM_BASE_URL,
        "report API": settings.API_BASE_URL,
    }
    unreachable: list[str] = []
    for name, base_url in targets.items():
        try:
            httpx.get(base_url, timeout=timeout)
        except httpx.HTTPError as exc:
            unreachable.append(f"{name} unreachable at {base_url} ({type(exc).__name__})")
    if unreachable:
        raise RuntimeError(
            "news-search eval precheck failed -- start the dev stack first "
            "(make dev-litellm / make dev-api / make tunnel):\n  " + "\n  ".join(unreachable)
        )


@dataclass(frozen=True)
class NewsSearchReport:
    """Aggregate of one run, returned to the caller and rendered by summarise."""

    flag_outcomes: tuple[FlagOutcome, ...]
    retrieval_outcomes: tuple[RetrievalOutcome, ...]

    @property
    def flag_accuracy(self) -> float:
        if not self.flag_outcomes:
            return 0.0
        return sum(1 for o in self.flag_outcomes if o.ok) / len(self.flag_outcomes)

    @property
    def false_positives(self) -> list[FlagOutcome]:
        return [o for o in self.flag_outcomes if o.false_positive]

    @property
    def positive_misses(self) -> list[FlagOutcome]:
        return [
            o
            for o in self.flag_outcomes
            if o.fixture.expected_news_search and not o.actual_news_search
        ]

    @property
    def retrieval_hit_rate(self) -> float:
        if not self.retrieval_outcomes:
            return 0.0
        return sum(1 for o in self.retrieval_outcomes if o.status == "match") / len(
            self.retrieval_outcomes
        )


def run_all(
    *,
    only: str | None = None,
    flag_only: bool = False,
    skip_precheck: bool = False,
) -> NewsSearchReport:
    """Run both layers over the fixture set and return the aggregate."""
    if not skip_precheck:
        precheck_environment()
    fixtures = load_news_search_fixtures()
    if only is not None:
        fixtures = [f for f in fixtures if f.id == only]
        if not fixtures:
            raise ValueError(f"no news-search fixture with id {only!r}")

    flag_outcomes = [evaluate_flag(f) for f in fixtures]
    retrieval_outcomes: list[RetrievalOutcome] = []
    if not flag_only:
        retrieval_outcomes = [evaluate_retrieval(f) for f in fixtures if f.expected_news_search]
    return NewsSearchReport(
        flag_outcomes=tuple(flag_outcomes),
        retrieval_outcomes=tuple(retrieval_outcomes),
    )


def contamination_warning(report: NewsSearchReport) -> str | None:
    """Flag a run contaminated by Groq/Qdrant throttling (latency signal).

    A fixture whose flag-layer wall time cleared one full LLM timeout ceiling
    means a classifier call ran to its timeout -- the throttle signature. When
    this fires, re-run on a clean rate-limit window before trusting the numbers
    (clean-window publishing rule).
    """
    slow = [o for o in report.flag_outcomes if o.elapsed_ms >= CONTAMINATION_LATENCY_MS]
    if not slow:
        return None
    return (
        f"CONTAMINATED RUN -- do not trust this aggregate. {len(slow)} fixture(s) "
        f"over the {CONTAMINATION_LATENCY_MS}ms timeout-ceiling floor "
        "(likely Groq throttling): "
        + ", ".join(f"{o.fixture.id}={o.elapsed_ms}ms" for o in slow)
        + ". Re-run on a clean rate-limit window before publishing baseline numbers."
    )


def is_failing(report: NewsSearchReport) -> bool:
    """Hard gate: a generic/off-domain ask that wrongly fired the search.

    Empty input fails too (a malformed stub that strips every fixture must not
    masquerade as a clean pass). Positive flag-misses and retrieval misses are
    reported but NOT gated -- positives are documented as known-misses per the
    AC, and retrieval recall is measured here, improved in a follow-up.
    """
    if not report.flag_outcomes:
        return True
    return bool(report.false_positives)


def summarise(report: NewsSearchReport) -> str:
    """Human-readable per-fixture + aggregate summary for stdout / the README."""
    lines: list[str] = []
    warning = contamination_warning(report)
    if warning is not None:
        lines += [warning, ""]

    flags = report.flag_outcomes
    total = len(flags)
    correct = sum(1 for o in flags if o.ok)
    negatives = [o for o in flags if not o.fixture.expected_news_search]
    abstained = sum(1 for o in negatives if not o.actual_news_search)
    lines += [
        "FLAG LAYER (semantic needs_news_search from the live classify LLM, OR keyword floor)",
        f"  flag_accuracy: {correct}/{total} ({report.flag_accuracy:.0%})  "
        f"negatives_abstained: {abstained}/{len(negatives)}  "
        f"false_positives: {len(report.false_positives)}  "
        f"positive_misses: {len(report.positive_misses)}",
    ]
    for o in flags:
        mark = "ok" if o.ok else ("FALSE-POSITIVE" if o.false_positive else "MISS")
        lines.append(
            f"    [{mark:14s}] {o.fixture.id:22s} "
            f"expected={str(o.fixture.expected_news_search):5s} "
            f"actual={str(o.actual_news_search):5s} "
            f"intent={o.resolved_intent}/{o.classifier_source} {o.elapsed_ms}ms"
        )

    if report.retrieval_outcomes:
        rs = report.retrieval_outcomes
        matched = sum(1 for o in rs if o.status == "match")
        lines += [
            "",
            "RETRIEVAL LAYER (structural relevance vs live Qdrant; report-only, "
            "rolling 7-day window)",
            f"  hit_rate: {matched}/{len(rs)} ({report.retrieval_hit_rate:.0%})",
        ]
        # search_news degrades to "[]" on any failure, so a Qdrant/tunnel outage
        # mid-run looks identical to a 0% hit-rate. If EVERY fixture returned
        # zero hits, warn that the substrate may be down rather than reading it
        # as a real recall collapse.
        if all(o.hit_count == 0 for o in rs):
            lines.append(
                "  WARNING: every retrieval returned 0 hits -- likely a Qdrant/tunnel "
                "outage, not a recall result. Verify the corpus is reachable before "
                "trusting this hit_rate."
            )
        for o in rs:
            term_tag = ",".join(o.matched_terms) if o.matched_terms else "-"
            lines.append(
                f"    [{o.status:8s}] {o.fixture.id:22s} "
                f"hits={o.hit_count} matched=[{term_tag}] {o.elapsed_ms}ms"
            )

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.evals.news_search_eval")
    parser.add_argument("--only", help="Run only one fixture id")
    parser.add_argument(
        "--flag-only",
        action="store_true",
        help="Run the flag layer only (skip live Qdrant retrieval).",
    )
    parser.add_argument(
        "--skip-precheck",
        action="store_true",
        help="Skip the LiteLLM/report-API reachability precheck (offline/testing only).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    try:
        report = run_all(
            only=args.only,
            flag_only=args.flag_only,
            skip_precheck=args.skip_precheck,
        )
    except RuntimeError as exc:
        # Precheck failure -- dev stack down. Skip gracefully (exit 2) rather
        # than report every fixture as a failure (AC4).
        print(f"SKIPPED: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        # Bad --only id (or a malformed fixture file the offline unit test would
        # already have caught). "Could not run a measurement" -- exit 2, not 1,
        # so a typo never masquerades as the false-positive hard-gate failure.
        print(f"SKIPPED: {exc}", file=sys.stderr)
        return 2
    except Exception:
        logger.exception("news-search eval run failed")
        return 1

    print(summarise(report))
    return 1 if is_failing(report) else 0


__all__ = [
    "CONTAMINATION_LATENCY_MS",
    "FlagOutcome",
    "MIN_NEGATIVES",
    "MIN_POSITIVES",
    "NEWS_SEARCH_GOLDENS_PATH",
    "NewsSearchFixture",
    "NewsSearchReport",
    "RetrievalOutcome",
    "contamination_warning",
    "evaluate_flag",
    "evaluate_retrieval",
    "is_failing",
    "load_news_search_fixtures",
    "precheck_environment",
    "run_all",
    "summarise",
]


if __name__ == "__main__":
    sys.exit(main())
