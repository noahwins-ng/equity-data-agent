"""Offline validation for goldens/rag_smoke.yaml (QNT-278 AC3).

Mirrors test_rag_impact_yaml / test_news_search_yaml: locks the invariants the
live runner can't catch without spending Cohere/Groq. These run in the default
unit sweep; the live behavioral run (agent.evals.rag_smoke_eval) does NOT -- it
needs the live stack.

The load-bearing contract here is routing: a `relevant` fixture runs the FULL
graph, so its query MUST fire the matching deterministic search router or the
graph never retrieves and the fixture silently tests nothing.
"""

from __future__ import annotations

from typing import Literal

import pytest
from agent.evals.rag_smoke_eval import (
    CONTAMINATION_LATENCY_MS,
    MIN_GUARD_PER_CORPUS,
    MIN_RELEVANT_PER_CORPUS,
    RERANK_FLOORS,
    Hit,
    RagSmokeFixture,
    RagSmokeOutcome,
    RagSmokeReport,
    _evaluate_guard,
    _evaluate_relevant,
    _grounding_terms,
    _parse_hits,
    contamination_warning,
    load_rag_smoke_fixtures,
)
from agent.intent import _is_earnings_search, _is_targeted_news
from shared.tickers import TICKERS


def _guard_fx(corpus: Literal["news", "earnings"] = "earnings") -> RagSmokeFixture:
    return RagSmokeFixture(
        id="g", ticker="NVDA", query="q", corpus=corpus, kind="boilerplate_guard"
    )


def _relevant_fx(corpus: Literal["news", "earnings"] = "earnings") -> RagSmokeFixture:
    return RagSmokeFixture(id="r", ticker="NVDA", query="q", corpus=corpus, kind="relevant")


def _outcome(
    *, status: str, elapsed_ms: int, graph_ran: bool, kind: str = "relevant"
) -> RagSmokeOutcome:
    fx = RagSmokeFixture(id="x", ticker="NVDA", query="q", corpus="earnings", kind=kind)  # type: ignore[arg-type]
    return RagSmokeOutcome(
        fixture=fx,
        status=status,  # type: ignore[arg-type]
        hit_count=1,
        top_score=0.6,
        elapsed_ms=elapsed_ms,
        graph_ran=graph_ran,
    )


def test_fixtures_load_and_validate() -> None:
    assert load_rag_smoke_fixtures(), "no rag-smoke fixtures loaded"


def test_fixture_ids_unique() -> None:
    ids = [f.id for f in load_rag_smoke_fixtures()]
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"


def test_tickers_in_registry() -> None:
    for f in load_rag_smoke_fixtures():
        assert f.ticker in TICKERS, f"{f.id}: unknown ticker {f.ticker!r}"


def test_coverage_floors() -> None:
    fixtures = load_rag_smoke_fixtures()
    for corpus in ("news", "earnings"):
        relevant = sum(1 for f in fixtures if f.corpus == corpus and f.kind == "relevant")
        guard = sum(1 for f in fixtures if f.corpus == corpus and f.kind == "boilerplate_guard")
        assert relevant >= MIN_RELEVANT_PER_CORPUS, f"{corpus}: {relevant} relevant"
        assert guard >= MIN_GUARD_PER_CORPUS, f"{corpus}: {guard} guard"


def test_floors_cover_every_corpus() -> None:
    for f in load_rag_smoke_fixtures():
        assert f.corpus in RERANK_FLOORS, f"{f.id}: no floor for corpus {f.corpus!r}"
        assert f.floor == RERANK_FLOORS[f.corpus]


def test_relevant_queries_fire_matching_router() -> None:
    """A `relevant` fixture runs the graph; its query must route to the corpus
    search it claims, or the live run tests nothing. Pinned so the YAML and the
    deterministic routers can't drift."""
    for f in load_rag_smoke_fixtures():
        if f.kind != "relevant":
            continue
        if f.corpus == "news":
            assert _is_targeted_news(f.query), (
                f"{f.id}: relevant news query does not fire _is_targeted_news: {f.query!r}"
            )
        else:
            assert _is_earnings_search(f.query), (
                f"{f.id}: relevant earnings query does not fire _is_earnings_search: {f.query!r}"
            )


def test_parse_hits_handles_both_corpora_and_degraded() -> None:
    news = _parse_hits(
        '[{"headline": "Acme Corp deal", "body": "x", "score": 0.71, "source": "Reuters"}]',
        "news",
    )
    assert news and news[0].score == 0.71 and "Acme Corp deal" in news[0].text
    earnings = _parse_hits(
        '[{"title": "Project Foo", "text": "y", "score": 0.62, "section": "Item 2.02"}]',
        "earnings",
    )
    assert earnings and earnings[0].section == "Item 2.02"
    assert _parse_hits("[]", "news") == []
    assert _parse_hits("not json", "earnings") == []


def test_grounding_terms_pull_distinctive_evidence() -> None:
    """Figures, percentages, and non-generic proper nouns are grounding evidence;
    the ticker / company name and generic places are not (the answer carries them
    regardless of retrieval, so they'd false-pass the grounding check)."""
    hit = Hit(
        text="NVDA cited the Blackwell Ultra ramp, guiding to $5.2 billion and a 71% margin.",
        section="Item 2.02",
        score=0.8,
    )
    terms = [t.lower() for t in _grounding_terms(hit, "NVDA")]
    assert any("5.2" in t for t in terms), terms
    assert any("71" in t for t in terms), terms
    assert any("blackwell ultra" in t for t in terms), terms


def test_grounding_terms_exclude_ticker_and_generic_nouns() -> None:
    hit = Hit(text="Wall Street watched New York closely.", section="", score=0.5)
    assert _grounding_terms(hit, "NVDA") == []


def test_grounding_terms_ticker_match_is_whole_word_not_substring() -> None:
    """A 2-letter ticker (MU) must not swallow a proper noun that merely contains
    those letters (Municipal Bond), or grounding would be silently emptied."""
    # "mu" is a substring of "municipal" but not a whole word -> kept.
    hit = Hit(text="The Municipal Bond Authority cited gains.", section="", score=0.6)
    assert _grounding_terms(hit, "MU") == ["Municipal Bond Authority"]
    # A ticker that surfaces as a Title-case word (META -> "Meta Platforms") IS
    # excluded as a whole word -- the answer carries the company name regardless.
    hit2 = Hit(text="Meta Platforms raised its outlook.", section="", score=0.6)
    assert _grounding_terms(hit2, "META") == []


def test_guard_none_score_hit_is_not_a_false_pass() -> None:
    """A surfaced hit with no rerank score (rerank declined -> non-reranked
    fallback, no floor) has NO evidence it cleared the floor -- the guard must
    FAIL it, not pass it as clean (the reviewer-found hole)."""
    hits = [Hit(text="About NVIDIA boilerplate", section="About NVIDIA", score=None)]
    outcome = _evaluate_guard(_guard_fx(), hits, started=0.0)
    assert outcome.status == "fail"
    assert "not provably above" in outcome.detail


def test_guard_empty_is_pass() -> None:
    outcome = _evaluate_guard(_guard_fx(), [], started=0.0)
    assert outcome.status == "pass"


def test_guard_above_floor_is_pass() -> None:
    hits = [Hit(text="strong guidance chunk", section="Item 2.02", score=0.7)]
    assert _evaluate_guard(_guard_fx(), hits, started=0.0).status == "pass"


def test_relevant_none_top_score_is_ungradable_not_floor_pass() -> None:
    """A None-score top hit (rerank declined) is unverifiable against the floor --
    report ungradable, do NOT fall through to the graph as if the floor passed."""
    hits = [Hit(text="Some Headline With $5.2 billion", section="", score=None)]
    outcome = _evaluate_relevant(_relevant_fx(), hits, started=0.0)
    assert outcome.status == "ungradable"
    assert not outcome.graph_ran


def test_contamination_ignores_non_graph_rows() -> None:
    """The fast-degraded floor is a GENERATION signal. A retrieval-only row (guard,
    --retrieval-only, or a relevant fixture empty/below-floor before the graph ran)
    elapsed in search milliseconds and must NOT be flagged -- the bug a real run
    surfaced where every fast search false-flagged as throttled."""
    report = RagSmokeReport(
        outcomes=(
            _outcome(status="pass", elapsed_ms=800, graph_ran=False),  # fast search
            _outcome(status="fail", elapsed_ms=900, graph_ran=False, kind="boilerplate_guard"),
        )
    )
    assert contamination_warning(report) is None


def test_contamination_flags_fast_graph_row() -> None:
    """A row that DID run the graph and finished suspiciously fast is the
    truncated-completion signature -- flag it."""
    report = RagSmokeReport(outcomes=(_outcome(status="fail", elapsed_ms=900, graph_ran=True),))
    warning = contamination_warning(report)
    assert warning is not None
    assert "fast-degraded" in warning


def test_contamination_flags_slow_graph_row() -> None:
    report = RagSmokeReport(
        outcomes=(_outcome(status="pass", elapsed_ms=CONTAMINATION_LATENCY_MS + 1, graph_ran=True),)
    )
    warning = contamination_warning(report)
    assert warning is not None
    assert "slow-throttle" in warning


@pytest.mark.parametrize("fid_kind", [(f.id, f.kind) for f in load_rag_smoke_fixtures()])
def test_expected_section_only_on_relevant(fid_kind: tuple[str, str]) -> None:
    """A boilerplate_guard surfaces nothing to assert a section on -- the loader
    rejects expected_section there; this re-pins it at the row level."""
    fixtures = {f.id: f for f in load_rag_smoke_fixtures()}
    fid, _kind = fid_kind
    f = fixtures[fid]
    if f.expected_section:
        assert f.kind == "relevant", f"{fid}: expected_section on a {f.kind}"
