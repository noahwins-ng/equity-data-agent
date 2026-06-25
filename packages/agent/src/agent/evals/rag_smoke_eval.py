"""Live end-to-end RAG smoke harness (QNT-278).

Every other eval scores ONE layer in isolation: the IR eval (QNT-261) scores
retrieval, DeepEval (QNT-264/275) scores generation, and the rag_impact eval
(QNT-277) STUBS the search tool so it tests fold->prompt->answer but by
construction cannot see retrieve->rerank. That blind spot is exactly where the
QNT-276 demotion (retrieved fact dropped from the answer) and the QNT-279
boilerplate leak (best-of-weak 8-K "About <co>" surfaced as a source) both
lived: every component eval was green while the end-to-end contribution was
wrong. The first real end-to-end check was a human clicking through the prod UI.

This harness closes that seam. It runs a small set of hand-picked queries
through the WHOLE chain against the REAL Qdrant + Cohere -- nothing stubbed --
and asserts two things the component evals cannot:

* **surfaced-source relevance** (the QNT-279 axis) -- a reranked hit must clear
  the per-corpus rerank floor this eval ASSERTS against (it does not implement
  it; the floor lives in ``api/routers/search.py``). Two fixture kinds split the
  assertion:
  - ``relevant`` (narrow ask): the top hit must clear the floor (and an optional
    expected section), proving a genuinely relevant chunk surfaced.
  - ``boilerplate_guard`` (broad ask): NO surfaced hit may sit below the floor.
    An empty result is the CORRECT outcome for a broad ask (the caller answers
    from its canned report); a sub-floor hit surfaced as a source is the bug.
* **the retrieved fact reaches the answer** (the QNT-276 axis) -- for a
  ``relevant`` fixture, a distinctive term DERIVED from the live top hit (a coined
  figure / proper noun, not a generic word the canned report also carries) must
  appear in the final answer text. Deriving the grounding term from the actual
  retrieval (rather than freezing it in YAML) keeps the assertion sound against
  the rolling news window -- the same behavioral, refactor-proof discipline as
  rag_impact.

Off the per-PR hot path by design -- it spends Cohere rerank quota and needs the
live stack, so like ``news_search_eval`` / ``deepeval_eval`` it is run on demand
(``uv run python -m agent.evals.rag_smoke_eval``), never collected by pytest.
The offline fixture validation (``tests/agent/evals/test_rag_smoke_yaml.py``)
runs in the default unit sweep. Respect the clean-rate-limit-window rule before
trusting numbers; ``contamination_warning`` flags a throttled window.

Examples::

    uv run python -m agent.evals.rag_smoke_eval
    uv run python -m agent.evals.rag_smoke_eval --only nvda-datacenter-news
    uv run python -m agent.evals.rag_smoke_eval --retrieval-only
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import yaml
from shared.config import settings
from shared.tickers import TICKERS

from agent.evals.provider_errors import is_provider_pressure_error, provider_error_label
from agent.evals.rag_impact_eval import _answer_text
from agent.graph import build_graph
from agent.tools import (
    default_report_tools,
    get_company_report_compact,
    get_comparison_metrics,
    search_earnings,
    search_news,
)

logger = logging.getLogger(__name__)

RAG_SMOKE_GOLDENS_PATH = Path(__file__).parent / "goldens" / "rag_smoke.yaml"

# Per-corpus rerank floors this eval ASSERTS against (QNT-279). These MUST track
# ``_NEWS_RERANK_FLOOR`` / ``_EARNINGS_RERANK_FLOOR`` in api/routers/search.py --
# the floor is implemented there; here we only check the surfaced scores honour
# it. The whole point of the harness is that on a pre-QNT-279 build (no floor)
# sub-floor boilerplate surfaces and these assertions fail.
RERANK_FLOORS: dict[str, float] = {"news": 0.30, "earnings": 0.50}

_CORPORA = ("news", "earnings")
_KINDS = ("relevant", "boilerplate_guard")

# Coverage floors. The harness is a smoke set, not a labeled IR set -- a handful
# of broad AND narrow phrasings per corpus is enough to exercise the seam. AC4
# calibration on the live corpus may expand it.
MIN_RELEVANT_PER_CORPUS = 3
MIN_GUARD_PER_CORPUS = 1

# A live retrieve + full graph (classify + synthesize/narrate) returns in several
# seconds on a clean window. Mirrors CONTAMINATION_LATENCY_MS / the fast floor in
# rag_impact_eval: a slow row ran a call to its timeout, a fast ``relevant`` row
# got a truncated completion that may have dropped the retrieved fact.
CONTAMINATION_LATENCY_MS = int(settings.LLM_REQUEST_TIMEOUT * 1000)
CONTAMINATION_FAST_LATENCY_MS = 2500


@dataclass(frozen=True)
class RagSmokeFixture:
    """One row from goldens/rag_smoke.yaml."""

    id: str
    ticker: str
    query: str
    corpus: Literal["news", "earnings"]
    kind: Literal["relevant", "boilerplate_guard"]
    expected_section: str = ""

    @property
    def floor(self) -> float:
        return RERANK_FLOORS[self.corpus]


@dataclass(frozen=True)
class Hit:
    """A surfaced search hit, normalised across the news / earnings shapes."""

    text: str
    section: str
    score: float | None


@dataclass(frozen=True)
class RagSmokeOutcome:
    """Result of running one fixture through the live chain."""

    fixture: RagSmokeFixture
    status: Literal["pass", "fail", "empty", "ungradable", "provider_error", "infra_error"]
    hit_count: int
    top_score: float | None
    elapsed_ms: int
    detail: str = ""
    grounding_term: str = ""
    grounded: bool = False
    # Did this row actually invoke the graph (classify + synthesize/narrate)?
    # Only graph rows carry a generation-latency signal -- a retrieval-only row
    # (guard, --retrieval-only, or a relevant fixture that failed/empty before the
    # graph ran) elapsed in milliseconds of search alone and must NOT be latency-
    # contamination-classified (it would false-flag every fast search as throttled).
    graph_ran: bool = False

    @property
    def gated(self) -> bool:
        """Counts toward the pass-rate. ``empty`` (broad window had no narrow hit)
        and ``ungradable`` (no distinctive grounding term in the hit) are reported
        but excluded -- an inconclusive run must never masquerade as a seam bug."""
        return self.status in ("pass", "fail")


def load_rag_smoke_fixtures(path: Path = RAG_SMOKE_GOLDENS_PATH) -> list[RagSmokeFixture]:
    """Parse + validate the YAML registry into typed fixtures.

    Validates here (unique ids, ticker in TICKERS, enum fields, coverage floors)
    so every consumer reads from one authority.
    """
    raw = yaml.safe_load(path.read_text())
    fixtures = raw.get("fixtures") if isinstance(raw, dict) else None
    if not isinstance(fixtures, list):
        raise ValueError(f"{path}: missing top-level `fixtures` list")

    records: list[RagSmokeFixture] = []
    seen_ids: set[str] = set()
    for entry in fixtures:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each fixture must be a mapping, got {type(entry)}")
        try:
            rec_id = str(entry["id"])
            ticker = str(entry["ticker"])
            query = str(entry["query"])
            corpus = str(entry["corpus"])
            kind = str(entry["kind"])
        except KeyError as exc:
            raise ValueError(f"{path}: fixture missing field {exc}") from exc
        expected_section = str(entry.get("expected_section", "")).strip()
        if rec_id in seen_ids:
            raise ValueError(f"{path}: duplicate fixture id {rec_id!r}")
        if ticker not in TICKERS:
            raise ValueError(f"{path}: fixture {rec_id!r} references unknown ticker {ticker!r}")
        if corpus not in _CORPORA:
            raise ValueError(f"{path}: fixture {rec_id!r} corpus {corpus!r} not in {_CORPORA}")
        if kind not in _KINDS:
            raise ValueError(f"{path}: fixture {rec_id!r} kind {kind!r} not in {_KINDS}")
        if expected_section and kind != "relevant":
            raise ValueError(
                f"{path}: fixture {rec_id!r} sets expected_section but is a {kind} "
                "(only `relevant` fixtures assert a section)"
            )
        seen_ids.add(rec_id)
        records.append(
            RagSmokeFixture(
                id=rec_id,
                ticker=ticker,
                query=query,
                corpus=cast(Any, corpus),
                kind=cast(Any, kind),
                expected_section=expected_section,
            )
        )

    _check_coverage(records, path)
    return records


def _check_coverage(records: list[RagSmokeFixture], path: Path) -> None:
    """Enforce the per-corpus relevant / boilerplate-guard floors."""
    for corpus in _CORPORA:
        relevant = sum(1 for r in records if r.corpus == corpus and r.kind == "relevant")
        guard = sum(1 for r in records if r.corpus == corpus and r.kind == "boilerplate_guard")
        if relevant < MIN_RELEVANT_PER_CORPUS:
            raise ValueError(
                f"{path}: {relevant} {corpus} relevant fixtures, "
                f"need at least {MIN_RELEVANT_PER_CORPUS}"
            )
        if guard < MIN_GUARD_PER_CORPUS:
            raise ValueError(
                f"{path}: {guard} {corpus} boilerplate_guard fixtures, "
                f"need at least {MIN_GUARD_PER_CORPUS}"
            )


def _parse_hits(raw: str, corpus: str) -> list[Hit]:
    """Normalise a search tool's JSON into ``Hit`` rows (text, section, score).

    ``search_news`` rows are ``{headline, source, date, score, url, body}``;
    ``search_earnings`` rows are ``{title, section, date, score, url, text}``.
    Both tools degrade to ``"[]"`` on any failure, so a bad/empty payload yields
    ``[]`` and the caller reads it as "nothing surfaced".
    """
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    hits: list[Hit] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        score = row.get("score")
        score_f = float(score) if isinstance(score, int | float) else None
        if corpus == "news":
            text = f"{row.get('headline') or ''} {row.get('body') or ''}".strip()
            section = str(row.get("source") or "").strip()
        else:
            text = f"{row.get('title') or ''} {row.get('text') or ''}".strip()
            section = str(row.get("section") or "").strip()
        hits.append(Hit(text=text, section=section, score=score_f))
    return hits


_FIGURE_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s?(?:billion|million|trillion|bn|mn|b|m|k)?", re.I
)
_PERCENT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s?%")
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")

# Multi-word proper nouns that are NOT distinctive grounding evidence -- the
# answer carries the company name regardless of whether retrieval contributed,
# so matching on it would false-pass the grounding check.
_GENERIC_PROPER_NOUNS = frozenset(
    {
        "wall street",
        "new york",
        "united states",
        "north america",
        "the company",
    }
)


def _grounding_terms(hit: Hit, ticker: str) -> list[str]:
    """Distinctive terms from a retrieved hit that, if echoed in the answer, prove
    retrieval reached it.

    Prefers coined figures / percentages (a specific retrieved number) and
    multi-word proper nouns, excluding the ticker, its company name, and generic
    place/entity names the answer would carry anyway. Deriving the term from the
    live hit (not a frozen YAML string) keeps the grounding assertion sound
    against the rolling corpus.

    Deliberately conservative: a SINGLE-word distinctive noun is not extracted (a
    lone ``[A-Z][a-z]+`` is indistinguishable from "Management" / "Revenue" /
    "Quarterly" that the answer carries regardless -- matching it would false-PASS
    grounding). A hit whose only distinctive token is single-word yields no terms,
    so the fixture is reported UNGRADABLE (visible, excluded from the gate), never
    a false pass/fail -- the signal to refine that fixture's query.
    """
    text = hit.text
    terms: list[str] = []
    terms += _FIGURE_RE.findall(text)
    terms += _PERCENT_RE.findall(text)
    ticker_l = ticker.lower()
    for phrase in _PROPER_NOUN_RE.findall(text):
        # Drop a leading article the Title-case regex swept in ("The Foo Bar"),
        # so the grounding substring matches the answer's bare "Foo Bar".
        phrase = re.sub(r"^(?:The|A|An)\s+", "", phrase)
        pl = phrase.lower()
        # Skip if stripping left a single word ("The Management" -> "Management"):
        # a lone Title-case word is not distinctive evidence (false-PASS risk).
        if " " not in pl:
            continue
        # Whole-word ticker match (not substring) so a 2-letter ticker like MU
        # doesn't swallow "Municipal"; pad both sides to catch any word position.
        if pl in _GENERIC_PROPER_NOUNS or f" {ticker_l} " in f" {pl} ":
            continue
        terms.append(phrase)
    # Dedup case-insensitively, preserve order.
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        key = t.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(t.strip())
    return out


def _live_search(fixture: RagSmokeFixture) -> str:
    """Call the REAL hybrid+rerank search tool for the fixture's corpus."""
    if fixture.corpus == "news":
        return search_news(fixture.ticker, fixture.query)
    return search_earnings(fixture.ticker, fixture.query)


def _build_live_graph() -> Any:
    """Compile the graph with the REAL report + search tools, wired like prod
    (api/routers/agent_chat.py) minus the SSE instrumentation."""
    return build_graph(
        default_report_tools(),
        compact_company_tool=get_company_report_compact,
        search_news_tool=search_news,
        search_earnings_tool=search_earnings,
        comparison_metrics_tool=get_comparison_metrics,
    )


def evaluate(fixture: RagSmokeFixture) -> RagSmokeOutcome:
    """Run one fixture through the live chain and classify the outcome."""
    started = time.perf_counter()
    try:
        raw = _live_search(fixture)
        hits = _parse_hits(raw, fixture.corpus)
        if fixture.kind == "boilerplate_guard":
            return _evaluate_guard(fixture, hits, started)
        return _evaluate_relevant(fixture, hits, started)
    except Exception as exc:  # noqa: BLE001 — surface as a row, keep the loop alive
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if is_provider_pressure_error(exc):
            return RagSmokeOutcome(
                fixture=fixture,
                status="provider_error",
                hit_count=0,
                top_score=None,
                elapsed_ms=elapsed_ms,
                detail=provider_error_label(exc),
            )
        logger.exception("rag-smoke %s: search or graph raised", fixture.id)
        return RagSmokeOutcome(
            fixture=fixture,
            status="infra_error",
            hit_count=0,
            top_score=None,
            elapsed_ms=elapsed_ms,
            detail=f"infra error: {type(exc).__name__}",
        )


def _evaluate_guard(fixture: RagSmokeFixture, hits: list[Hit], started: float) -> RagSmokeOutcome:
    """Broad ask: every surfaced hit must PROVABLY clear the floor. Empty == pass.

    A hit fails the guard if its score is below the floor OR is ``None``. A
    ``None`` score means the API returned it on the non-reranked path (the
    cross-encoder declined / a BM25-only hit, see search.py): the QNT-279 floor
    only applies when rerank ran, so a scoreless hit has NO evidence it cleared
    the floor -- surfacing it as a source is the same leak the guard catches, and
    treating it as clean would be a false pass.
    """
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    floor = fixture.floor
    unverified = [h for h in hits if h.score is None or h.score < floor]
    top_score = hits[0].score if hits else None
    if unverified:
        numeric = [h.score for h in unverified if h.score is not None]
        worst = f"{min(numeric):.3f}" if numeric else "no-score"
        return RagSmokeOutcome(
            fixture=fixture,
            status="fail",
            hit_count=len(hits),
            top_score=top_score,
            elapsed_ms=elapsed_ms,
            detail=(
                f"{len(unverified)} hit(s) not provably above the {floor} floor "
                f"(worst {worst}) surfaced as sources -- pre-QNT-279 boilerplate "
                "leak or a rerank-declined fallback"
            ),
        )
    return RagSmokeOutcome(
        fixture=fixture,
        status="pass",
        hit_count=len(hits),
        top_score=top_score,
        elapsed_ms=elapsed_ms,
        detail="" if hits else "empty (correct broad-ask outcome: canned report)",
    )


def _evaluate_relevant(
    fixture: RagSmokeFixture, hits: list[Hit], started: float
) -> RagSmokeOutcome:
    """Narrow ask: the top hit must clear the floor (+ optional section) AND a
    distinctive term from it must reach the answer (the full graph runs)."""
    if not hits:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return RagSmokeOutcome(
            fixture=fixture,
            status="empty",
            hit_count=0,
            top_score=None,
            elapsed_ms=elapsed_ms,
            detail="no hit surfaced (recall gap or rolling window) -- not gated",
        )
    top = hits[0]
    floor = fixture.floor
    if top.score is None:
        # No rerank score => the API fell to the non-reranked path (rerank
        # declined / a BM25-only top hit): the floor is unverifiable, so the
        # surfaced-source-relevance assertion can't be graded. Report it ungated
        # rather than spend a graph call asserting grounding against an unverified
        # hit (a rerank-declined window is contamination, not a real defect).
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return RagSmokeOutcome(
            fixture=fixture,
            status="ungradable",
            hit_count=len(hits),
            top_score=None,
            elapsed_ms=elapsed_ms,
            detail="top hit has no rerank score (rerank likely declined) -- not gated",
        )
    if top.score < floor:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return RagSmokeOutcome(
            fixture=fixture,
            status="fail",
            hit_count=len(hits),
            top_score=top.score,
            elapsed_ms=elapsed_ms,
            detail=f"top hit {top.score:.3f} below the {floor} floor (boilerplate surfaced)",
        )
    if fixture.expected_section and fixture.expected_section.lower() not in top.section.lower():
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return RagSmokeOutcome(
            fixture=fixture,
            status="fail",
            hit_count=len(hits),
            top_score=top.score,
            elapsed_ms=elapsed_ms,
            detail=(
                f"top hit section {top.section!r} is not the expected "
                f"{fixture.expected_section!r} (boilerplate section surfaced)"
            ),
        )
    terms = _grounding_terms(top, fixture.ticker)
    if not terms:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return RagSmokeOutcome(
            fixture=fixture,
            status="ungradable",
            hit_count=len(hits),
            top_score=top.score,
            elapsed_ms=elapsed_ms,
            detail="no distinctive term in the top hit to ground against -- not gated",
        )

    # The retrieval is relevant -- now run the FULL graph and assert the fact
    # reaches the answer (the QNT-276 demotion axis the stubbed eval can't see).
    graph = _build_live_graph()
    state = cast(
        dict[str, Any], graph.invoke({"ticker": fixture.ticker, "question": fixture.query})
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    answer = _answer_text(state).lower()
    matched = next((t for t in terms if t.lower() in answer), "")
    if matched:
        return RagSmokeOutcome(
            fixture=fixture,
            status="pass",
            hit_count=len(hits),
            top_score=top.score,
            elapsed_ms=elapsed_ms,
            grounding_term=matched,
            grounded=True,
            graph_ran=True,
        )
    return RagSmokeOutcome(
        fixture=fixture,
        status="fail",
        hit_count=len(hits),
        top_score=top.score,
        elapsed_ms=elapsed_ms,
        grounding_term=terms[0],
        detail=(
            f"relevant hit (score {top.score}) surfaced but none of its distinctive "
            f"terms {terms[:3]} reached the answer -- the QNT-276 demotion"
        ),
        graph_ran=True,
    )


def precheck_environment(*, timeout: float = 5.0) -> None:
    """Raise if the live stack the smoke set needs is unreachable.

    Needs LiteLLM (classify + synthesize) and the report API (which proxies
    Qdrant + Cohere). A reachable HTTP response (any status) clears the check; a
    connection error fails it before a token / rerank call is spent. Cohere/Qdrant
    being down behind a reachable API is NOT a precheck failure -- the search
    tools degrade to ``"[]"`` and the run reports the empties.
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
            "rag-smoke eval precheck failed -- start the dev stack first "
            "(make dev-litellm / make dev-api / make tunnel):\n  " + "\n  ".join(unreachable)
        )


@dataclass(frozen=True)
class RagSmokeReport:
    """Aggregate of one run."""

    outcomes: tuple[RagSmokeOutcome, ...] = ()

    @property
    def gated(self) -> list[RagSmokeOutcome]:
        return [o for o in self.outcomes if o.gated]

    @property
    def passes(self) -> list[RagSmokeOutcome]:
        return [o for o in self.outcomes if o.status == "pass"]

    @property
    def failures(self) -> list[RagSmokeOutcome]:
        return [o for o in self.outcomes if o.status == "fail"]

    @property
    def pass_rate(self) -> float:
        gated = self.gated
        return len(self.passes) / len(gated) if gated else 0.0


# Each fixture fires one Cohere rerank call (hybrid+rerank search). The Cohere
# trial tier rate-limits per minute; a tight back-to-back sweep trips it, and a
# DECLINED rerank silently falls back to the floorless fused path (the QNT-279
# floor "only applies when the cross-encoder ran"), surfacing sub-floor boilerplate
# that reads as a guard FAIL when it is really a contaminated window. Spacing the
# fixtures keeps the rerank cadence under the limit so the floor reliably applies
# -- the "space fixtures" mitigation the ticket called out. ``--delay 0`` opts out
# (a single ``--only`` fixture never bursts).
_DEFAULT_FIXTURE_DELAY_S = 6.0


def run_all(
    *,
    only: str | None = None,
    retrieval_only: bool = False,
    skip_precheck: bool = False,
    delay_s: float = _DEFAULT_FIXTURE_DELAY_S,
) -> RagSmokeReport:
    """Run every fixture through the live chain and return the aggregate.

    ``retrieval_only`` skips the answer-grounding graph run -- it converts every
    ``relevant`` fixture to a retrieval-floor-only check (no Groq spend), useful
    for isolating the QNT-279 axis from the QNT-276 axis on a tight window.
    ``delay_s`` spaces the per-fixture Cohere rerank calls under the trial
    rate-limit (see ``_DEFAULT_FIXTURE_DELAY_S``).
    """
    if not skip_precheck:
        precheck_environment()
    fixtures = load_rag_smoke_fixtures()
    if only is not None:
        fixtures = [f for f in fixtures if f.id == only]
        if not fixtures:
            raise ValueError(f"no rag-smoke fixture with id {only!r}")
    outcomes: list[RagSmokeOutcome] = []
    for i, f in enumerate(fixtures):
        if i > 0 and delay_s > 0:
            time.sleep(delay_s)
        if retrieval_only and f.kind == "relevant":
            outcomes.append(_retrieval_only_outcome(f))
        else:
            outcomes.append(evaluate(f))
    return RagSmokeReport(outcomes=tuple(outcomes))


def _retrieval_only_outcome(fixture: RagSmokeFixture) -> RagSmokeOutcome:
    """A ``relevant`` fixture scored on the retrieval floor alone (no graph)."""
    started = time.perf_counter()
    try:
        hits = _parse_hits(_live_search(fixture), fixture.corpus)
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return RagSmokeOutcome(
            fixture=fixture,
            status="infra_error",
            hit_count=0,
            top_score=None,
            elapsed_ms=elapsed_ms,
            detail=f"infra error: {type(exc).__name__}",
        )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if not hits:
        return RagSmokeOutcome(
            fixture=fixture,
            status="empty",
            hit_count=0,
            top_score=None,
            elapsed_ms=elapsed_ms,
            detail="no hit surfaced -- not gated",
        )
    top = hits[0]
    if top.score is not None and top.score < fixture.floor:
        return RagSmokeOutcome(
            fixture=fixture,
            status="fail",
            hit_count=len(hits),
            top_score=top.score,
            elapsed_ms=elapsed_ms,
            detail=f"top hit {top.score:.3f} below the {fixture.floor} floor",
        )
    return RagSmokeOutcome(
        fixture=fixture,
        status="pass",
        hit_count=len(hits),
        top_score=top.score,
        elapsed_ms=elapsed_ms,
        detail="retrieval-only (answer grounding skipped)",
    )


def contamination_warning(report: RagSmokeReport) -> str | None:
    """Flag a run contaminated by Groq throttling (latency signal).

    Only rows that actually ran the graph carry a generation-latency signal, so
    the floors apply to ``graph_ran`` rows only (a guard row, a ``--retrieval-only``
    row, or a relevant fixture that went empty/below-floor before the graph ran
    elapsed in search milliseconds and must not be flagged). Mirrors
    rag_impact_eval: a slow row ran a call to its timeout, a fast row got a
    truncated completion that may have dropped the retrieved fact from the answer.
    """
    graph_rows = [o for o in report.outcomes if o.graph_ran]
    slow = [o for o in graph_rows if o.elapsed_ms >= CONTAMINATION_LATENCY_MS]
    fast = [
        o
        for o in graph_rows
        if o.status in ("pass", "fail") and o.elapsed_ms <= CONTAMINATION_FAST_LATENCY_MS
    ]
    if not slow and not fast:
        return None
    parts: list[str] = []
    if slow:
        parts.append(
            f"{len(slow)} fixture(s) over the {CONTAMINATION_LATENCY_MS}ms timeout "
            "ceiling (slow-throttle): "
            + ", ".join(f"{o.fixture.id}={o.elapsed_ms}ms" for o in slow)
        )
    if fast:
        parts.append(
            f"{len(fast)} relevant fixture(s) under the {CONTAMINATION_FAST_LATENCY_MS}ms "
            "fast-degraded floor (truncated completion may have dropped the fact): "
            + ", ".join(f"{o.fixture.id}={o.elapsed_ms}ms" for o in fast)
        )
    return (
        "CONTAMINATED RUN -- do not trust this aggregate. "
        + "; ".join(parts)
        + ". Re-run on a clean rate-limit window before publishing baseline numbers."
    )


def is_failing(report: RagSmokeReport) -> bool:
    """Hard signal: any gated fixture failed its assertion.

    Empty input fails too (a malformed fixture file that strips every row must not
    masquerade as a clean pass). On a pre-QNT-279 build the boilerplate_guard rows
    are expected to FAIL (sub-floor hits surface) -- that RED baseline IS the
    evidence the seam bug is real (AC4); the floor flips them GREEN.
    """
    if not report.gated:
        return True
    return bool(report.failures)


def summarise(report: RagSmokeReport) -> str:
    """Human-readable per-fixture + aggregate summary for stdout / the README."""
    lines: list[str] = []
    warning = contamination_warning(report)
    if warning is not None:
        lines += [warning, ""]

    gated = report.gated
    empties = [o for o in report.outcomes if o.status == "empty"]
    ungradable = [o for o in report.outcomes if o.status == "ungradable"]
    provider = [o for o in report.outcomes if o.status == "provider_error"]
    infra = [o for o in report.outcomes if o.status == "infra_error"]
    lines += [
        "RAG SMOKE EVAL (live Qdrant + Cohere -> answer text; full chain, nothing stubbed)",
        f"  pass_rate: {len(report.passes)}/{len(gated)} ({report.pass_rate:.0%})  "
        f"failures: {len(report.failures)}  empty: {len(empties)}  "
        f"ungradable: {len(ungradable)}  provider_errors: {len(provider)}  "
        f"infra_errors: {len(infra)}",
    ]
    for o in report.outcomes:
        mark = {
            "pass": "ok",
            "fail": "FAIL",
            "empty": "EMPTY",
            "ungradable": "UNGRADABLE",
            "provider_error": "PROVIDER-ERR",
            "infra_error": "INFRA-ERR",
        }[o.status]
        score = f"{o.top_score:.3f}" if o.top_score is not None else "-"
        ground = f" grounded={o.grounding_term!r}" if o.grounded else ""
        suffix = f"  -- {o.detail}" if o.detail else ""
        lines.append(
            f"    [{mark:12s}] {o.fixture.id:26s} "
            f"corpus={o.fixture.corpus:8s} kind={o.fixture.kind:16s} "
            f"hits={o.hit_count} top={score} {o.elapsed_ms}ms{ground}{suffix}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.evals.rag_smoke_eval")
    parser.add_argument("--only", help="Run only one fixture id")
    parser.add_argument(
        "--retrieval-only",
        action="store_true",
        help="Score relevant fixtures on the rerank floor alone (skip the graph / Groq spend).",
    )
    parser.add_argument(
        "--skip-precheck",
        action="store_true",
        help="Skip the LiteLLM/report-API reachability precheck (offline/testing only).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=_DEFAULT_FIXTURE_DELAY_S,
        help=(
            "Seconds between fixtures to keep Cohere rerank under the trial "
            f"rate-limit (default {_DEFAULT_FIXTURE_DELAY_S}; 0 = no spacing)."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    try:
        report = run_all(
            only=args.only,
            retrieval_only=args.retrieval_only,
            skip_precheck=args.skip_precheck,
            delay_s=args.delay,
        )
    except RuntimeError as exc:
        # Precheck failure -- stack down. Skip gracefully (exit 2) rather than
        # report every fixture as a failure.
        print(f"SKIPPED: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        # Bad --only id or a malformed fixture file. Exit 2 so a typo never
        # masquerades as the hard-gate failure.
        print(f"SKIPPED: {exc}", file=sys.stderr)
        return 2
    except Exception:
        logger.exception("rag-smoke eval run failed")
        return 1

    print(summarise(report))
    return 1 if is_failing(report) else 0


__all__ = [
    "CONTAMINATION_FAST_LATENCY_MS",
    "CONTAMINATION_LATENCY_MS",
    "MIN_GUARD_PER_CORPUS",
    "MIN_RELEVANT_PER_CORPUS",
    "RAG_SMOKE_GOLDENS_PATH",
    "RERANK_FLOORS",
    "Hit",
    "RagSmokeFixture",
    "RagSmokeOutcome",
    "RagSmokeReport",
    "contamination_warning",
    "evaluate",
    "is_failing",
    "load_rag_smoke_fixtures",
    "precheck_environment",
    "run_all",
    "summarise",
]


if __name__ == "__main__":
    sys.exit(main())
