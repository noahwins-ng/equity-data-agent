"""RAG impact eval harness (QNT-277).

No existing eval measures whether retrieved RAG content actually REACHES the
agent's final answer when search fires. The retrieval IR eval (QNT-261) scores
retrieval in isolation; DeepEval (QNT-264/275) scores generation quality.
Neither catches "search fired, retrieved a relevant hit, and the answer ignored
it" -- the exact gap that let the synthesis demotion (QNT-276) go unnoticed
while every component eval stayed green.

This harness closes that gap with a BEHAVIORAL assertion, not a statistical
retrieval score. For each fixture it:

* Compiles the graph with STUB tools via ``build_graph`` dependency injection:
  - the report tools return a canned digest that does NOT contain the planted
    fact;
  - ``search_news_tool`` / ``search_earnings_tool`` return a fixture hit carrying
    a distinctive, hard-to-paraphrase COINED entity (a proper noun + a figure).
* Invokes the graph on a question that fires the deterministic search router.
* Asserts the planted entity appears in the final ANSWER TEXT (positive) or does
  NOT appear (negative control, where the stub returns ``"[]"``).

Keying on the user-facing ANSWER TEXT (not internal ``retrieved_sources`` shape
or fold rendering) is the invariant that makes the eval-first ordering safe: the
contract survives the QNT-276 refactor, so the only way a fixture flips from RED
to GREEN is a genuine behavior change. Pre-QNT-276 the planted fact is dropped
(fold + "omission is fine") -> positives FAIL; post-QNT-276 it is foregrounded ->
positives PASS.

Because the search tools are stubbed, the eval touches neither Qdrant NOR Cohere
-- the only model calls are the agent's own classify + synthesize/narrate on the
Groq free tier. It runs OFF the per-PR hot path (workflow_dispatch / local), like
the DeepEval suite, but with zero rerank-quota cost. The offline fixture
validation lives in ``tests/agent/evals/test_rag_impact_yaml.py`` and DOES run in
the default unit sweep.

Examples::

    uv run python -m agent.evals.rag_impact_eval
    uv run python -m agent.evals.rag_impact_eval --only nvda-antitrust-news
    uv run python -m agent.evals.rag_impact_eval --no-history
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import yaml
from shared.config import settings
from shared.tickers import TICKERS

from agent.evals.golden_set import (
    HISTORY_FIELDS,
    HISTORY_PATH,
    _git_sha,
    _prompt_version,
)
from agent.evals.provider_errors import is_provider_pressure_error, provider_error_label
from agent.graph import build_graph

logger = logging.getLogger(__name__)

RAG_IMPACT_GOLDENS_PATH = Path(__file__).parent / "goldens" / "rag_impact.yaml"

# Coverage floors (size by COVERAGE, not budget -- the stub design makes Cohere a
# non-constraint). ~2 targeted-news, ~2 earnings-narrative, ~2 multi-corpus, ~2
# negative controls. This is a behavioral assertion eval, not the 50-200 labeled
# set of the IR eval (QNT-261).
MIN_NEWS_POSITIVES = 2
MIN_EARNINGS_POSITIVES = 2
MIN_MULTI_POSITIVES = 2
MIN_NEGATIVES = 2

_CORPORA = ("news", "earnings", "both")
_KINDS = ("positive", "negative_control")

# Classify + synthesize on a clean window return in a few seconds each. A fixture
# whose wall time clears one full LLM timeout ceiling means a call ran to its
# timeout -- the Groq-throttle signature. Mirrors golden_set / news_search_eval.
CONTAMINATION_LATENCY_MS = int(settings.LLM_REQUEST_TIMEOUT * 1000)

# QNT-278: the FAST-degraded floor. Throttling has two signatures, not one. The
# slow one (above) is a call that ran to its timeout. The fast one is Groq
# returning a truncated, low-effort completion in a fraction of a healthy
# generation -- which silently drops a planted entity that a full generation
# would have quoted (the msft-guidance-earnings flake: ~1.4s degraded vs ~6.6s
# isolated/healthy). Without a low floor, a fast-degraded run reports a
# "trustworthy" 7/8 instead of being flagged. Calibrated empirically: healthy
# positives run several seconds (~6.6s observed), the degraded one ~1.4s; 2500ms
# sits well clear of the healthy band and above the degraded one. Restricted to
# POSITIVES (a negative control's no-fabrication answer is legitimately short and
# quick -- flagging it would be a false alarm).
CONTAMINATION_FAST_LATENCY_MS = 2500


@dataclass(frozen=True)
class RagImpactFixture:
    """One row from goldens/rag_impact.yaml."""

    id: str
    ticker: str
    question: str
    corpus: Literal["news", "earnings", "both"]
    kind: Literal["positive", "negative_control"]
    planted_entity: str
    planted_figure: str

    @property
    def fires_news(self) -> bool:
        return self.corpus in ("news", "both")

    @property
    def fires_earnings(self) -> bool:
        return self.corpus in ("earnings", "both")


def _news_hit_json(fixture: RagImpactFixture) -> str:
    """A search_news payload carrying the planted entity in the headline+body.

    Matches the shape ``_format_search_hits`` parses ({headline, source, date,
    score, url, body}); the entity sits in the headline so it always survives the
    280-char body truncation.
    """
    entity = fixture.planted_entity
    figure = fixture.planted_figure
    return json.dumps(
        [
            {
                "headline": f"{entity}: {fixture.ticker} exposure pegged at {figure}",
                "source": "Reuters",
                "date": "2026-06-20",
                "score": 0.92,
                "url": "https://example.com/news/rag-impact",
                "body": (
                    f"Documents tied to the {entity} put {fixture.ticker}'s exposure "
                    f"at {figure}, according to people familiar with the matter."
                ),
            }
        ]
    )


def _earnings_hit_json(fixture: RagImpactFixture) -> str:
    """A search_earnings payload carrying the planted entity in the title+text.

    Matches the shape ``_format_earnings_hits`` parses ({title, section, date,
    score, url, text}); the entity sits in the title so it always renders.
    """
    entity = fixture.planted_entity
    figure = fixture.planted_figure
    return json.dumps(
        [
            {
                "title": f"{entity} -- {fixture.ticker} quarterly commentary",
                "section": "Item 2.02",
                "date": "2026-06-18",
                "score": 0.90,
                "url": "https://example.com/earnings/rag-impact",
                "text": (
                    f"Management cited the {entity}, guiding to {figure} over the coming period."
                ),
            }
        ]
    )


def _canned_reports(fixture: RagImpactFixture) -> dict[str, str]:
    """Generic per-tool digests that deliberately OMIT the planted fact (AC1).

    Every report tool the plan might select (company / technical / fundamental /
    news) returns a plausible-but-bland report mentioning only the ticker, never
    the coined entity -- so the only source of the entity is the stubbed search
    hit.
    """
    t = fixture.ticker
    return {
        "company": (
            f"## {t} Company Profile\n{t} operates across its core segments with "
            "an established competitive position. No company-specific events are "
            "noted in this profile."
        ),
        "technical": (
            f"## {t} Technical Snapshot\nRSI is neutral and the price trades near "
            "its 50-day moving average. No standout technical signal."
        ),
        "fundamental": (
            f"## {t} Fundamentals\nRevenue and margins are broadly in line with the "
            "prior period. Valuation sits near the sector median."
        ),
        "news": (
            f"## {t} News Digest\n- {t} shares moved in line with the broad market.\n"
            "- No major company-specific catalyst in the canned digest."
        ),
    }


class _RecordingStub:
    """A stubbed search tool that records whether the graph actually called it.

    The graph gates each search on the deterministic flag AND the resolved intent
    (``_intent_reads_corpus`` over the QNT-288 policy table). If a positive
    fixture's stub was never called, the question misrouted (wrong intent label)
    rather than failing the synthesis-fold contract -- a distinct axis we report
    but never fold into the pass-rate.
    """

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.called = False

    def __call__(self, _ticker: str, _query: str) -> str:
        self.called = True
        return self.payload


_PAYLOAD_KEYS = ("thesis", "quick_fact", "comparison", "conversational", "focused", "exploration")


def _answer_text(state: dict[str, Any]) -> str:
    """Reconstruct the full user-facing answer: narrative + structured payload.

    The agent's answer is the streamed analyst ``narrative`` plus whichever
    structured payload slot the synthesize node filled (each renders via
    ``to_markdown``). The targeted-news path drops the focused card and lets
    narrate own the spoken answer, so we must union both -- keying on this text
    (not internal structures) is the QNT-276-refactor-proof invariant.
    """
    parts: list[str] = []
    narrative = state.get("narrative")
    if narrative:
        parts.append(str(narrative))
    for key in _PAYLOAD_KEYS:
        obj = state.get(key)
        to_markdown = getattr(obj, "to_markdown", None)
        if obj is not None and callable(to_markdown):
            try:
                parts.append(str(to_markdown()))
            except Exception:  # noqa: BLE001 — a render glitch must not crash the eval
                logger.warning("rag-impact: %s.to_markdown() failed", key)
    return "\n\n".join(parts)


@dataclass(frozen=True)
class RagImpactOutcome:
    """Result of running one fixture through the stubbed graph."""

    fixture: RagImpactFixture
    status: Literal["pass", "fail", "misrouted", "provider_error", "infra_error"]
    entity_present: bool
    search_fired: bool
    answer_chars: int
    elapsed_ms: int
    detail: str = ""
    # Per-corpus firing, so a corpus="both" positive that silently degraded to a
    # single-corpus test (one gate fired, the other was silenced by the intent
    # label) is observable rather than an undetected pass (reviewer finding #1).
    news_fired: bool = False
    earnings_fired: bool = False

    @property
    def gated(self) -> bool:
        """Counts toward the pass-rate (pass/fail).

        misrouted (intent never routed to retrieval), provider_error (Groq
        throttle/timeout/5xx) and infra_error (a harness/graph-construction bug,
        NOT a synthesis miss) are all excluded -- an inconclusive run must never
        masquerade as the demotion gap and pollute the baseline (finding #2).
        """
        return self.status in ("pass", "fail")

    @property
    def partial_multi_corpus(self) -> bool:
        """A both-corpus positive where only one of the two search stubs fired."""
        return (
            self.fixture.kind == "positive"
            and self.fixture.corpus == "both"
            and self.search_fired
            and (self.news_fired != self.earnings_fired)
        )


def load_rag_impact_fixtures(
    path: Path = RAG_IMPACT_GOLDENS_PATH,
) -> list[RagImpactFixture]:
    """Parse + validate the YAML registry into typed fixtures.

    Validates here (unique ids, ticker in TICKERS, enum fields, planted entity
    present, coverage floors) so every consumer reads from one authority.
    """
    raw = yaml.safe_load(path.read_text())
    fixtures = raw.get("fixtures") if isinstance(raw, dict) else None
    if not isinstance(fixtures, list):
        raise ValueError(f"{path}: missing top-level `fixtures` list")

    records: list[RagImpactFixture] = []
    seen_ids: set[str] = set()
    for entry in fixtures:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each fixture must be a mapping, got {type(entry)}")
        try:
            rec_id = str(entry["id"])
            ticker = str(entry["ticker"])
            question = str(entry["question"])
            corpus = str(entry["corpus"])
            kind = str(entry["kind"])
            planted_entity = str(entry["planted_entity"])
            planted_figure = str(entry["planted_figure"])
        except KeyError as exc:
            raise ValueError(f"{path}: fixture missing field {exc}") from exc
        if rec_id in seen_ids:
            raise ValueError(f"{path}: duplicate fixture id {rec_id!r}")
        if ticker not in TICKERS:
            raise ValueError(f"{path}: fixture {rec_id!r} references unknown ticker {ticker!r}")
        if corpus not in _CORPORA:
            raise ValueError(f"{path}: fixture {rec_id!r} corpus {corpus!r} not in {_CORPORA}")
        if kind not in _KINDS:
            raise ValueError(f"{path}: fixture {rec_id!r} kind {kind!r} not in {_KINDS}")
        if not planted_entity.strip():
            raise ValueError(f"{path}: fixture {rec_id!r} has an empty planted_entity")
        seen_ids.add(rec_id)
        records.append(
            RagImpactFixture(
                id=rec_id,
                ticker=ticker,
                question=question,
                corpus=cast(Any, corpus),
                kind=cast(Any, kind),
                planted_entity=planted_entity,
                planted_figure=planted_figure,
            )
        )

    _check_coverage(records, path)
    return records


def _check_coverage(records: list[RagImpactFixture], path: Path) -> None:
    """Enforce the coverage floors (news / earnings / multi-corpus / negatives)."""
    positives = [r for r in records if r.kind == "positive"]
    news_pos = sum(1 for r in positives if r.corpus == "news")
    earnings_pos = sum(1 for r in positives if r.corpus == "earnings")
    multi_pos = sum(1 for r in positives if r.corpus == "both")
    negatives = sum(1 for r in records if r.kind == "negative_control")
    if news_pos < MIN_NEWS_POSITIVES:
        raise ValueError(f"{path}: {news_pos} news positives, need at least {MIN_NEWS_POSITIVES}")
    if earnings_pos < MIN_EARNINGS_POSITIVES:
        raise ValueError(
            f"{path}: {earnings_pos} earnings positives, need at least {MIN_EARNINGS_POSITIVES}"
        )
    if multi_pos < MIN_MULTI_POSITIVES:
        raise ValueError(
            f"{path}: {multi_pos} multi-corpus positives, need at least {MIN_MULTI_POSITIVES}"
        )
    if negatives < MIN_NEGATIVES:
        raise ValueError(f"{path}: {negatives} negative controls, need at least {MIN_NEGATIVES}")


def evaluate(fixture: RagImpactFixture) -> RagImpactOutcome:
    """Run one fixture through the stubbed graph and classify the outcome.

    A positive fixture's search stub returns the planted hit; a negative control's
    returns ``"[]"``. Only the corpora the fixture targets get a hit -- the others
    are stubbed-but-empty so an off-corpus fire can't smuggle the entity in.
    """
    reports = _canned_reports(fixture)
    report_tools = {name: (lambda _t, text=text: text) for name, text in reports.items()}

    positive = fixture.kind == "positive"
    news_payload = _news_hit_json(fixture) if (positive and fixture.fires_news) else "[]"
    earnings_payload = (
        _earnings_hit_json(fixture) if (positive and fixture.fires_earnings) else "[]"
    )
    news_stub = _RecordingStub(news_payload)
    earnings_stub = _RecordingStub(earnings_payload)

    started = time.perf_counter()
    try:
        graph = build_graph(
            cast(Any, report_tools),
            search_news_tool=news_stub,
            search_earnings_tool=earnings_stub,
        )
        state = cast(
            dict[str, Any], graph.invoke({"ticker": fixture.ticker, "question": fixture.question})
        )
    except Exception as exc:  # noqa: BLE001 — surface as a row, keep the loop alive
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if is_provider_pressure_error(exc):
            return RagImpactOutcome(
                fixture=fixture,
                status="provider_error",
                entity_present=False,
                search_fired=False,
                answer_chars=0,
                elapsed_ms=elapsed_ms,
                detail=provider_error_label(exc),
            )
        # A graph-construction / invoke bug (stub signature mismatch, a node
        # import error) is NOT a synthesis miss -- gating it as "fail" would let an
        # inconclusive run masquerade as the demotion gap and pollute the baseline.
        # Classify it ungated (finding #2); the logger.exception makes it loud.
        logger.exception("rag-impact %s: build_graph or graph.invoke raised", fixture.id)
        return RagImpactOutcome(
            fixture=fixture,
            status="infra_error",
            entity_present=False,
            search_fired=False,
            answer_chars=0,
            elapsed_ms=elapsed_ms,
            detail=f"infra error: {type(exc).__name__}",
        )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    answer = _answer_text(state)
    entity_present = fixture.planted_entity.lower() in answer.lower()
    search_fired = news_stub.called or earnings_stub.called

    # A positive whose search never fired misrouted (wrong intent label / flag) --
    # that is a routing axis (news_search_eval / routing.yaml), not the synthesis
    # fold this eval gates. Report it, keep it out of the pass-rate.
    if positive and not search_fired:
        status: Literal["pass", "fail", "misrouted", "provider_error", "infra_error"] = "misrouted"
        detail = "search never fired (intent/flag did not route to retrieval)"
    elif positive:
        status = "pass" if entity_present else "fail"
        detail = "" if entity_present else "planted entity absent from answer (the gap)"
    else:
        # Negative control: the coined entity must NOT appear. Note this passes
        # VACUOUSLY if the search never fired (the entity was never injected, so it
        # cannot leak) -- which is still the correct contract (no fabrication), but
        # means a negative pass does not by itself prove the routing fired. The
        # positive fixtures are what exercise the firing path.
        status = "fail" if entity_present else "pass"
        detail = "fabricated planted entity with no retrieval" if entity_present else ""

    return RagImpactOutcome(
        fixture=fixture,
        status=status,
        entity_present=entity_present,
        search_fired=search_fired,
        answer_chars=len(answer),
        elapsed_ms=elapsed_ms,
        detail=detail,
        news_fired=news_stub.called,
        earnings_fired=earnings_stub.called,
    )


def precheck_environment(*, timeout: float = 5.0) -> None:
    """Raise if the LiteLLM proxy is unreachable.

    The report + search tools are stubbed, so the ONLY live dependency is the
    LiteLLM proxy (the agent's classify + synthesize/narrate LLM calls). No
    tunnel, ClickHouse, Qdrant, report API, or Cohere is touched. A reachable
    HTTP response (any status) clears the check; a connection error fails it
    before a single token is spent.
    """
    base_url = settings.LITELLM_BASE_URL
    try:
        httpx.get(base_url, timeout=timeout)
    except httpx.HTTPError as exc:
        raise RuntimeError(
            "rag-impact eval precheck failed -- start the LiteLLM proxy first "
            f"(make dev-litellm): LiteLLM unreachable at {base_url} ({type(exc).__name__})"
        ) from exc


@dataclass(frozen=True)
class RagImpactReport:
    """Aggregate of one run."""

    outcomes: tuple[RagImpactOutcome, ...]

    @property
    def gated(self) -> list[RagImpactOutcome]:
        return [o for o in self.outcomes if o.gated]

    @property
    def passes(self) -> list[RagImpactOutcome]:
        return [o for o in self.outcomes if o.status == "pass"]

    @property
    def failures(self) -> list[RagImpactOutcome]:
        return [o for o in self.outcomes if o.status == "fail"]

    @property
    def misrouted(self) -> list[RagImpactOutcome]:
        return [o for o in self.outcomes if o.status == "misrouted"]

    @property
    def provider_errors(self) -> list[RagImpactOutcome]:
        return [o for o in self.outcomes if o.status == "provider_error"]

    @property
    def infra_errors(self) -> list[RagImpactOutcome]:
        return [o for o in self.outcomes if o.status == "infra_error"]

    @property
    def partial_multi_corpus(self) -> list[RagImpactOutcome]:
        return [o for o in self.outcomes if o.partial_multi_corpus]

    @property
    def pass_rate(self) -> float:
        gated = self.gated
        if not gated:
            return 0.0
        return len(self.passes) / len(gated)


def run_all(
    *,
    only: str | None = None,
    skip_precheck: bool = False,
) -> RagImpactReport:
    """Run every fixture through the stubbed graph and return the aggregate."""
    if not skip_precheck:
        precheck_environment()
    fixtures = load_rag_impact_fixtures()
    if only is not None:
        fixtures = [f for f in fixtures if f.id == only]
        if not fixtures:
            raise ValueError(f"no rag-impact fixture with id {only!r}")
    return RagImpactReport(outcomes=tuple(evaluate(f) for f in fixtures))


def contamination_warning(report: RagImpactReport) -> str | None:
    """Flag a run contaminated by Groq throttling (latency signal).

    Throttling shows up two ways. SLOW: a call ran to its timeout ceiling.
    FAST (QNT-278): Groq returned a truncated, low-effort completion in a
    fraction of a healthy generation, which silently drops a planted entity a
    full generation would have quoted -- turning a contaminated run into a false
    7/8 rather than a flagged one. The fast floor is scoped to gated POSITIVES
    (where the dropped-entity risk lives); a negative control's short answer is
    legitimately quick.
    """
    slow = [o for o in report.outcomes if o.elapsed_ms >= CONTAMINATION_LATENCY_MS]
    fast = [
        o
        for o in report.outcomes
        if o.status in ("pass", "fail")
        and o.fixture.kind == "positive"
        and o.elapsed_ms <= CONTAMINATION_FAST_LATENCY_MS
    ]
    if not slow and not fast:
        return None
    parts: list[str] = []
    if slow:
        parts.append(
            f"{len(slow)} fixture(s) over the {CONTAMINATION_LATENCY_MS}ms "
            "timeout-ceiling floor (slow-throttle): "
            + ", ".join(f"{o.fixture.id}={o.elapsed_ms}ms" for o in slow)
        )
    if fast:
        parts.append(
            f"{len(fast)} positive(s) under the {CONTAMINATION_FAST_LATENCY_MS}ms "
            "fast-degraded floor (truncated completion likely dropped the planted "
            "entity): " + ", ".join(f"{o.fixture.id}={o.elapsed_ms}ms" for o in fast)
        )
    return (
        "CONTAMINATED RUN -- do not trust this aggregate. "
        + "; ".join(parts)
        + ". Re-run on a clean rate-limit window before publishing baseline numbers."
    )


def is_failing(report: RagImpactReport) -> bool:
    """Hard signal: any gated fixture failed its assertion.

    Empty input fails too (a malformed stub that strips every fixture must not
    masquerade as a clean pass). Pre-QNT-276 the positives are expected to fail
    here -- that RED baseline IS the evidence the gap is real; QNT-276 flips it
    GREEN. Misrouted/provider_error/infra_error rows are excluded (distinct axes).
    """
    if not report.gated:
        return True
    return bool(report.failures)


def append_history(
    report: RagImpactReport,
    *,
    run_id: str | None = None,
    history_path: Path = HISTORY_PATH,
) -> str:
    """Append one aggregate ``eval_type="rag_impact"`` row to history.csv.

    Mirrors the retrieval-eval aggregate row (QNT-261): one row per run, the
    rag_impact_* columns filled and everything else blank, stamped with the same
    git_sha + prompt_version as the other eval types so a regression is bisectable
    against the same commits.
    """
    import csv

    rid = run_id or uuid.uuid4().hex[:8]
    new_file = not history_path.exists()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
        if new_file:
            writer.writeheader()
        row: dict[str, Any] = {field: "" for field in HISTORY_FIELDS}
        row.update(
            {
                "run_id": rid,
                "git_sha": _git_sha(),
                "prompt_version": _prompt_version(),
                "eval_type": "rag_impact",
                "rag_impact_pass_rate": round(report.pass_rate, 4),
                "rag_impact_n": len(report.gated),
            }
        )
        writer.writerow(cast(Any, row))
    return rid


def summarise(report: RagImpactReport) -> str:
    """Human-readable per-fixture + aggregate summary for stdout / the README."""
    lines: list[str] = []
    warning = contamination_warning(report)
    if warning is not None:
        lines += [warning, ""]

    gated = report.gated
    lines += [
        "RAG IMPACT EVAL (stubbed retrieval -> answer text; zero Cohere/Qdrant)",
        f"  pass_rate: {len(report.passes)}/{len(gated)} ({report.pass_rate:.0%})  "
        f"failures: {len(report.failures)}  misrouted: {len(report.misrouted)}  "
        f"provider_errors: {len(report.provider_errors)}  "
        f"infra_errors: {len(report.infra_errors)}",
    ]
    # A both-corpus positive that only fired one search degraded to a single-corpus
    # test -- the answer may carry the entity via the corpus that DID fire while the
    # other fold went untested. Surface it so the multi-corpus signal isn't trusted
    # blindly (finding #1).
    for o in report.partial_multi_corpus:
        lines.append(
            f"  WARNING: {o.fixture.id} (corpus=both) fired only "
            f"{'news' if o.news_fired else 'earnings'} -- the other corpus fold was "
            "untested this run."
        )
    for o in report.outcomes:
        mark = {
            "pass": "ok",
            "fail": "FAIL",
            "misrouted": "MISROUTED",
            "provider_error": "PROVIDER-ERR",
            "infra_error": "INFRA-ERR",
        }[o.status]
        suffix = f"  -- {o.detail}" if o.detail else ""
        lines.append(
            f"    [{mark:12s}] {o.fixture.id:26s} "
            f"corpus={o.fixture.corpus:8s} kind={o.fixture.kind:16s} "
            f"entity_present={str(o.entity_present):5s} fired={str(o.search_fired):5s} "
            f"{o.elapsed_ms}ms{suffix}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.evals.rag_impact_eval")
    parser.add_argument("--only", help="Run only one fixture id")
    parser.add_argument(
        "--skip-precheck",
        action="store_true",
        help="Skip the LiteLLM reachability precheck (offline/testing only).",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Do not append an aggregate row to history.csv.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    try:
        report = run_all(only=args.only, skip_precheck=args.skip_precheck)
    except RuntimeError as exc:
        # Precheck failure -- LiteLLM down. Skip gracefully (exit 2) rather than
        # report every fixture as a failure.
        print(f"SKIPPED: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        # Bad --only id or a malformed fixture file. "Could not run a measurement"
        # -- exit 2, not 1, so a typo never masquerades as the hard-gate failure.
        print(f"SKIPPED: {exc}", file=sys.stderr)
        return 2
    except Exception:
        logger.exception("rag-impact eval run failed")
        return 1

    print(summarise(report))
    if not args.no_history and args.only is None:
        rid = append_history(report)
        print(f"\nhistory run_id: {rid}")
    return 1 if is_failing(report) else 0


__all__ = [
    "CONTAMINATION_FAST_LATENCY_MS",
    "CONTAMINATION_LATENCY_MS",
    "MIN_EARNINGS_POSITIVES",
    "MIN_MULTI_POSITIVES",
    "MIN_NEGATIVES",
    "MIN_NEWS_POSITIVES",
    "RAG_IMPACT_GOLDENS_PATH",
    "RagImpactFixture",
    "RagImpactOutcome",
    "RagImpactReport",
    "append_history",
    "contamination_warning",
    "evaluate",
    "is_failing",
    "load_rag_impact_fixtures",
    "precheck_environment",
    "run_all",
    "summarise",
]


if __name__ == "__main__":
    sys.exit(main())
