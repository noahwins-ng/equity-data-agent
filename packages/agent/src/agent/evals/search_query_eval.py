"""Contextual RAG query-rewrite eval harness (QNT-289).

Both RAG fire points (``agent.graph`` gather node) used to pass the RAW user
question as the retrieval query. On a warm thread a targeted follow-up is
often elliptical ("what about the buyback?" after an NVDA thesis turn) --
the classify LLM correctly sets ``needs_news_search``/``needs_earnings_search``
from history (QNT-216/QNT-280), but the bare ellipsis carries no ticker or
topic noun for hybrid + rerank retrieval to match against. QNT-289 adds a
``search_query`` field to the SAME classify structured-output call
(``IntentDecision``), guardrailed by ``agent.intent.sanitize_search_query``.

This harness scores the rewrite over a curated golden set
(``goldens/search_query.yaml``) with three fixture kinds:

* **elliptical** -- warm-thread follow-up with seeded ``history``, no ticker
  named in the question itself. The rewrite must resolve the ticker + topic
  FROM history -- this is the load-bearing case the ticket exists for.
* **cold_targeted** -- no history; the question already names its own
  ticker/topic. The rewrite must not degrade it (the AC's "cold targeted
  asks are not degraded" requirement).
* **generic** -- no targeted event / no search-worthy intent. needs_search
  must stay False and no rewrite should fire.

Two layers per fixture:

* **Flag layer** -- ``needs_news_search OR needs_earnings_search`` must equal
  the fixture's ``expected_needs_search``. Mirrors ``news_search_eval``'s hard
  gate: a false positive on a generic ask is the dangerous direction (it
  drops the canned digest for a narrower RAG-only read).
* **Query layer** -- for a positive fixture, the sanitized ``search_query``
  must contain at least one alias from BOTH ``ticker_terms`` and
  ``topic_terms`` (any-of, case-insensitive substring -- mirrors
  ``retrieval.yaml``'s ``anchor_terms``). For a negative fixture, the query
  must be empty (no hallucinated rewrite on a question that needs none).

Standalone, like ``news_search_eval`` / ``routing_eval`` -- needs the live
LiteLLM proxy (the classify LLM call), so it is not collected by pytest and
does not run in the default unit sweep (offline fixture validation lives in
``tests/agent/evals/test_search_query_yaml.py``).

Examples::

    uv run python -m agent.evals.search_query_eval
    uv run python -m agent.evals.search_query_eval --only nvda-buyback-followup
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
import yaml
from shared.config import settings
from shared.tickers import TICKERS

from agent.intent import classify_intent_with_source
from agent.prompts import ConversationMessage
from agent.tools import search_earnings, search_news

logger = logging.getLogger(__name__)

SEARCH_QUERY_GOLDENS_PATH = Path(__file__).parent / "goldens" / "search_query.yaml"

_KINDS = ("elliptical", "cold_targeted", "generic")

MIN_ELLIPTICAL = 6
MIN_COLD_TARGETED = 5
MIN_GENERIC = 5

# Aggregate gate (mirrors routing_eval.ACCURACY_FLOOR): the elliptical kind is
# the load-bearing metric this ticket exists for (resolving ticker+topic from
# history), so it gets a hard floor rather than relying on a human reading
# summarise()'s per-kind percentages. Measured baseline on a clean window is
# 7/8 (88%); set below that so the gate catches a real regression (e.g. the
# small model losing the history-resolution instruction) without being flaky
# on the single documented small-model miss (meta-layoffs-followup).
ELLIPTICAL_QUERY_ACCURACY_FLOOR = 0.70

# Mirrors news_search_eval's CONTAMINATION_LATENCY_MS -- a fixture whose wall
# time clears one full LLM timeout ceiling means the classifier call ran to
# its timeout, the signature of Groq throttling rather than a slow fixture.
CONTAMINATION_LATENCY_MS = int(settings.LLM_REQUEST_TIMEOUT * 1000)


@dataclass(frozen=True)
class SearchQueryFixture:
    """One row from goldens/search_query.yaml."""

    id: str
    ticker: str
    kind: Literal["elliptical", "cold_targeted", "generic"]
    history: tuple[ConversationMessage, ...]
    question: str
    expected_needs_search: bool
    ticker_terms: tuple[str, ...]
    topic_terms: tuple[str, ...]
    corpus: Literal["news", "earnings"] | None


@dataclass(frozen=True)
class SearchQueryOutcome:
    """Result of running one fixture through the live classifier."""

    fixture: SearchQueryFixture
    resolved_intent: str
    classifier_source: str
    actual_needs_search: bool
    search_query: str
    elapsed_ms: int

    @property
    def flag_ok(self) -> bool:
        return self.actual_needs_search == self.fixture.expected_needs_search

    @property
    def false_positive(self) -> bool:
        """A generic fixture that wrongly fired search -- the gated direction."""
        return not self.fixture.expected_needs_search and self.actual_needs_search

    @property
    def query_ok(self) -> bool:
        """Positive: query names the resolved ticker AND the topic.
        Negative: no rewrite was produced."""
        if not self.fixture.expected_needs_search:
            return self.search_query == ""
        haystack = self.search_query.lower()
        ticker_hit = any(term.lower() in haystack for term in self.fixture.ticker_terms)
        topic_hit = any(term.lower() in haystack for term in self.fixture.topic_terms)
        return ticker_hit and topic_hit


def load_search_query_fixtures(
    path: Path = SEARCH_QUERY_GOLDENS_PATH,
) -> list[SearchQueryFixture]:
    """Parse + validate the YAML registry into typed fixtures.

    Validates here (unique ids, ticker in TICKERS, known kind, history/kind
    consistency, positive fixtures carry both term lists, negatives carry
    neither, per-kind floors) so every consumer reads from one authority.
    """
    raw = yaml.safe_load(path.read_text())
    fixtures = raw.get("fixtures") if isinstance(raw, dict) else None
    if not isinstance(fixtures, list):
        raise ValueError(f"{path}: missing top-level `fixtures` list")

    records: list[SearchQueryFixture] = []
    seen_ids: set[str] = set()
    for entry in fixtures:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each fixture must be a mapping, got {type(entry)}")
        try:
            rec_id = str(entry["id"])
            ticker = str(entry["ticker"])
            kind = str(entry["kind"])
            question = str(entry["question"])
            expected = bool(entry["expected_needs_search"])
        except KeyError as exc:
            raise ValueError(f"{path}: fixture missing field {exc}") from exc
        if rec_id in seen_ids:
            raise ValueError(f"{path}: duplicate fixture id {rec_id!r}")
        if ticker not in TICKERS:
            raise ValueError(f"{path}: fixture {rec_id!r} references unknown ticker {ticker!r}")
        if kind not in _KINDS:
            raise ValueError(f"{path}: fixture {rec_id!r} has unknown kind {kind!r}")

        history_raw = entry.get("history", [])
        if not isinstance(history_raw, list):
            raise ValueError(f"{path}: fixture {rec_id!r} history must be a list")
        history = tuple(
            ConversationMessage(role=h["role"], content=h["content"]) for h in history_raw
        )
        if kind == "elliptical" and not history:
            raise ValueError(f"{path}: elliptical fixture {rec_id!r} must carry history")
        if kind in ("cold_targeted", "generic") and history:
            raise ValueError(f"{path}: {kind} fixture {rec_id!r} must NOT carry history")

        ticker_terms = tuple(str(t) for t in entry.get("ticker_terms", []))
        topic_terms = tuple(str(t) for t in entry.get("topic_terms", []))
        if expected and not (ticker_terms and topic_terms):
            raise ValueError(
                f"{path}: positive fixture {rec_id!r} must list both ticker_terms "
                "and topic_terms for the query-relevance assertion"
            )
        if not expected and (ticker_terms or topic_terms):
            raise ValueError(
                f"{path}: negative fixture {rec_id!r} must NOT carry ticker_terms/"
                "topic_terms (negatives do not fire a rewrite)"
            )

        corpus = entry.get("corpus")
        if expected and corpus not in ("news", "earnings"):
            raise ValueError(
                f"{path}: positive fixture {rec_id!r} must set corpus to 'news' "
                "or 'earnings' for the retrieval-comparison layer"
            )
        if not expected and corpus is not None:
            raise ValueError(f"{path}: negative fixture {rec_id!r} must NOT carry corpus")

        seen_ids.add(rec_id)
        records.append(
            SearchQueryFixture(
                id=rec_id,
                ticker=ticker,
                kind=kind,  # type: ignore[arg-type] — validated against _KINDS above
                history=history,
                question=question,
                expected_needs_search=expected,
                ticker_terms=ticker_terms,
                topic_terms=topic_terms,
                corpus=corpus,
            )
        )

    counts = {kind: sum(1 for r in records if r.kind == kind) for kind in _KINDS}
    floors = {
        "elliptical": MIN_ELLIPTICAL,
        "cold_targeted": MIN_COLD_TARGETED,
        "generic": MIN_GENERIC,
    }
    for kind, floor in floors.items():
        if counts[kind] < floor:
            raise ValueError(f"{path}: {counts[kind]} {kind} fixtures, need at least {floor}")
    return records


def evaluate(fixture: SearchQueryFixture) -> SearchQueryOutcome:
    """Run the live classifier (with the fixture's seeded history) and score it."""
    started = time.perf_counter()
    intent, source, needs_news_search, needs_earnings_search, search_query = (
        classify_intent_with_source(
            fixture.question,
            has_prior_turn=bool(fixture.history),
            history=fixture.history or None,
        )
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return SearchQueryOutcome(
        fixture=fixture,
        resolved_intent=intent,
        classifier_source=source,
        actual_needs_search=needs_news_search or needs_earnings_search,
        search_query=search_query,
        elapsed_ms=elapsed_ms,
    )


@dataclass(frozen=True)
class RetrievalComparisonOutcome:
    """AC3: live retrieval hit-rate for a positive fixture, raw question vs the
    resolved (guardrailed) rewrite. Only computed for fixtures where the
    rewrite actually differs from the raw question -- otherwise the two calls
    would be identical and the comparison is a no-op."""

    fixture: SearchQueryFixture
    raw_hit: bool
    raw_hit_count: int
    rewritten_hit: bool
    rewritten_hit_count: int
    elapsed_ms: int

    @property
    def improved(self) -> bool:
        """The exact recall-gain scenario the ticket exists for: the raw
        question missed, the rewrite hit."""
        return (not self.raw_hit) and self.rewritten_hit

    @property
    def regressed(self) -> bool:
        """The dangerous direction: the raw question would have hit, but the
        rewrite lost it. Should never happen -- gather only uses the rewrite
        when it is non-empty, so a regression here is a real bug."""
        return self.raw_hit and not self.rewritten_hit


def _search_hit(
    ticker: str, query: str, *, corpus: Literal["news", "earnings"]
) -> tuple[bool, int]:
    """Run the live corpus search and report (any_hit, hit_count)."""
    tool = search_news if corpus == "news" else search_earnings
    raw = tool(ticker, query)
    try:
        hits = json.loads(raw)
    except ValueError:
        hits = []
    if not isinstance(hits, list):
        hits = []
    return bool(hits), len(hits)


def evaluate_retrieval_comparison(
    outcome: SearchQueryOutcome,
) -> RetrievalComparisonOutcome | None:
    """AC3: compare live retrieval hit-rate for the raw question vs the
    resolved rewrite. Returns None for a negative fixture or when the rewrite
    is empty/identical to the raw question (nothing to compare)."""
    fixture = outcome.fixture
    if not fixture.expected_needs_search or fixture.corpus is None:
        return None
    if not outcome.search_query or outcome.search_query == fixture.question:
        return None

    started = time.perf_counter()
    raw_hit, raw_count = _search_hit(fixture.ticker, fixture.question, corpus=fixture.corpus)
    rewritten_hit, rewritten_count = _search_hit(
        fixture.ticker, outcome.search_query, corpus=fixture.corpus
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return RetrievalComparisonOutcome(
        fixture=fixture,
        raw_hit=raw_hit,
        raw_hit_count=raw_count,
        rewritten_hit=rewritten_hit,
        rewritten_hit_count=rewritten_count,
        elapsed_ms=elapsed_ms,
    )


def precheck_environment(*, timeout: float = 5.0, with_retrieval: bool = False) -> None:
    """Raise if the LiteLLM proxy (and, with ``with_retrieval``, the report API
    that proxies Qdrant) is unreachable."""
    targets = {"LiteLLM proxy": settings.LITELLM_BASE_URL}
    if with_retrieval:
        targets["report API"] = settings.API_BASE_URL
    unreachable: list[str] = []
    for name, base_url in targets.items():
        try:
            httpx.get(base_url, timeout=timeout)
        except httpx.HTTPError as exc:
            unreachable.append(f"{name} unreachable at {base_url} ({type(exc).__name__})")
    if unreachable:
        raise RuntimeError(
            "search-query eval precheck failed -- start the dev stack first "
            "(make dev-litellm / make dev-api / make tunnel):\n  " + "\n  ".join(unreachable)
        )


@dataclass(frozen=True)
class SearchQueryReport:
    """Aggregate of one run, returned to the caller and rendered by summarise."""

    outcomes: tuple[SearchQueryOutcome, ...]
    retrieval_outcomes: tuple[RetrievalComparisonOutcome, ...] = ()

    def _by_kind(self, kind: str) -> list[SearchQueryOutcome]:
        return [o for o in self.outcomes if o.fixture.kind == kind]

    @property
    def flag_accuracy(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(1 for o in self.outcomes if o.flag_ok) / len(self.outcomes)

    @property
    def false_positives(self) -> list[SearchQueryOutcome]:
        return [o for o in self.outcomes if o.false_positive]

    def query_accuracy(self, kind: str) -> float:
        rows = self._by_kind(kind)
        if not rows:
            return 0.0
        return sum(1 for o in rows if o.query_ok) / len(rows)

    @property
    def elliptical_miss_to_hit(self) -> list[SearchQueryOutcome]:
        """Elliptical fixtures where the raw question lacks the ticker term
        (a genuine ellipsis) AND the rewrite recovered it -- the exact
        recall-gain scenario the ticket exists for."""
        hits = []
        for o in self._by_kind("elliptical"):
            raw_had_ticker = any(
                term.lower() in o.fixture.question.lower() for term in o.fixture.ticker_terms
            )
            if not raw_had_ticker and o.query_ok:
                hits.append(o)
        return hits

    @property
    def raw_hit_rate(self) -> float:
        if not self.retrieval_outcomes:
            return 0.0
        return sum(1 for o in self.retrieval_outcomes if o.raw_hit) / len(self.retrieval_outcomes)

    @property
    def rewritten_hit_rate(self) -> float:
        if not self.retrieval_outcomes:
            return 0.0
        return sum(1 for o in self.retrieval_outcomes if o.rewritten_hit) / len(
            self.retrieval_outcomes
        )

    @property
    def retrieval_regressions(self) -> list[RetrievalComparisonOutcome]:
        return [o for o in self.retrieval_outcomes if o.regressed]


def run_all(
    *,
    only: str | None = None,
    skip_precheck: bool = False,
    with_retrieval: bool = False,
) -> SearchQueryReport:
    """Run the flag/query layers (and, with ``with_retrieval``, the AC3
    retrieval-comparison layer) over the fixture set."""
    if not skip_precheck:
        precheck_environment(with_retrieval=with_retrieval)
    fixtures = load_search_query_fixtures()
    if only is not None:
        fixtures = [f for f in fixtures if f.id == only]
        if not fixtures:
            raise ValueError(f"no search-query fixture with id {only!r}")

    outcomes = [evaluate(f) for f in fixtures]
    retrieval_outcomes: list[RetrievalComparisonOutcome] = []
    if with_retrieval:
        for outcome in outcomes:
            comparison = evaluate_retrieval_comparison(outcome)
            if comparison is not None:
                retrieval_outcomes.append(comparison)
    return SearchQueryReport(outcomes=tuple(outcomes), retrieval_outcomes=tuple(retrieval_outcomes))


def contamination_warning(report: SearchQueryReport) -> str | None:
    """Flag a run contaminated by Groq throttling (latency signal)."""
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


def is_failing(report: SearchQueryReport) -> bool:
    """Hard gate: a generic ask that wrongly fired the search (the gated
    direction, mirroring news_search_eval), a retrieval REGRESSION (the
    rewrite lost a hit the raw question would have had -- should never happen
    since gather only swaps in the rewrite when it is non-empty), or the
    elliptical kind's query accuracy falling below ELLIPTICAL_QUERY_ACCURACY_FLOOR
    (mirrors routing_eval.ACCURACY_FLOOR -- the load-bearing AC2/AC3 metric gets
    an aggregate floor rather than relying on a human reading summarise()'s
    per-kind percentages). Individual cold_targeted/generic query misses and a
    single elliptical miss above the floor are reported but NOT gated -- an
    empty/rejected rewrite falls back to the raw question (the QNT-280
    recall-floor pattern), so it can only forgo an upgrade, never regress
    cold-turn retrieval."""
    if not report.outcomes:
        return True
    if report.false_positives or report.retrieval_regressions:
        return True
    elliptical = report._by_kind("elliptical")
    if elliptical and report.query_accuracy("elliptical") < ELLIPTICAL_QUERY_ACCURACY_FLOOR:
        return True
    return False


def summarise(report: SearchQueryReport) -> str:
    """Human-readable per-fixture + aggregate summary for stdout / the PR."""
    lines: list[str] = []
    warning = contamination_warning(report)
    if warning is not None:
        lines += [warning, ""]

    lines.append(
        f"FLAG LAYER: accuracy {sum(1 for o in report.outcomes if o.flag_ok)}/"
        f"{len(report.outcomes)} ({report.flag_accuracy:.0%})  "
        f"false_positives: {len(report.false_positives)}"
    )
    lines.append("")
    lines.append(
        "QUERY LAYER (per kind: ticker_terms AND topic_terms present, or empty for negatives)"
    )
    for kind in _KINDS:
        rows = report._by_kind(kind)
        if not rows:
            continue
        ok = sum(1 for o in rows if o.query_ok)
        floor_tag = ""
        if kind == "elliptical":
            mark = (
                "PASS"
                if report.query_accuracy(kind) >= ELLIPTICAL_QUERY_ACCURACY_FLOOR
                else ("BELOW FLOOR")
            )
            floor_tag = f"  floor: {ELLIPTICAL_QUERY_ACCURACY_FLOOR:.0%} [{mark}]"
        lines.append(
            f"  {kind:15s}: {ok}/{len(rows)} ({report.query_accuracy(kind):.0%}){floor_tag}"
        )
    miss_to_hit = report.elliptical_miss_to_hit
    lines.append(
        f"  elliptical miss->hit (raw question lacked ticker, rewrite recovered it): "
        f"{len(miss_to_hit)}/{len(report._by_kind('elliptical'))}"
    )
    lines.append("")
    for o in report.outcomes:
        flag_mark = "ok" if o.flag_ok else ("FALSE-POSITIVE" if o.false_positive else "MISS")
        query_mark = "ok" if o.query_ok else "MISS"
        lines.append(
            f"    [{o.fixture.kind:14s}] {o.fixture.id:24s} "
            f"flag={flag_mark:14s} query={query_mark:4s} "
            f"search_query={o.search_query!r} "
            f"intent={o.resolved_intent}/{o.classifier_source} {o.elapsed_ms}ms"
        )

    if report.retrieval_outcomes:
        lines += [
            "",
            "RETRIEVAL LAYER (AC3: live hit-rate, raw question vs resolved rewrite; "
            "report-only, rolling news/earnings window)",
            f"  raw_hit_rate:       {sum(1 for o in report.retrieval_outcomes if o.raw_hit)}/"
            f"{len(report.retrieval_outcomes)} ({report.raw_hit_rate:.0%})",
            f"  rewritten_hit_rate: "
            f"{sum(1 for o in report.retrieval_outcomes if o.rewritten_hit)}/"
            f"{len(report.retrieval_outcomes)} ({report.rewritten_hit_rate:.0%})",
            f"  regressions (raw hit, rewrite missed -- should be 0): "
            f"{len(report.retrieval_regressions)}",
        ]
        for o in report.retrieval_outcomes:
            tag = "IMPROVED" if o.improved else ("REGRESSED" if o.regressed else "-")
            lines.append(
                f"    [{tag:9s}] {o.fixture.id:24s} "
                f"raw_hits={o.raw_hit_count} rewritten_hits={o.rewritten_hit_count} "
                f"{o.elapsed_ms}ms"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.evals.search_query_eval")
    parser.add_argument("--only", help="Run only one fixture id")
    parser.add_argument(
        "--retrieval",
        action="store_true",
        help="Also run the AC3 live retrieval hit-rate comparison (needs the report API/Qdrant).",
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
            skip_precheck=args.skip_precheck,
            with_retrieval=args.retrieval,
        )
    except RuntimeError as exc:
        print(f"SKIPPED: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"SKIPPED: {exc}", file=sys.stderr)
        return 2
    except Exception:
        logger.exception("search-query eval run failed")
        return 1

    print(summarise(report))
    return 1 if is_failing(report) else 0


__all__ = [
    "CONTAMINATION_LATENCY_MS",
    "ELLIPTICAL_QUERY_ACCURACY_FLOOR",
    "MIN_COLD_TARGETED",
    "MIN_ELLIPTICAL",
    "MIN_GENERIC",
    "SEARCH_QUERY_GOLDENS_PATH",
    "RetrievalComparisonOutcome",
    "SearchQueryFixture",
    "SearchQueryOutcome",
    "SearchQueryReport",
    "contamination_warning",
    "evaluate",
    "evaluate_retrieval_comparison",
    "is_failing",
    "load_search_query_fixtures",
    "precheck_environment",
    "run_all",
    "summarise",
]


if __name__ == "__main__":
    sys.exit(main())
