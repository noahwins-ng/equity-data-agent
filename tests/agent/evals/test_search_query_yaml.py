"""Offline validation for goldens/search_query.yaml (QNT-289).

Mirrors test_news_search_yaml / test_routing_yaml: locks the invariants the
live runner can't catch without spending tokens. These run in the default
unit sweep; the live flag + query layers (agent.evals.search_query_eval) do
NOT -- they need the tunnel and LiteLLM.
"""

from __future__ import annotations

from agent.evals.search_query_eval import (
    ELLIPTICAL_QUERY_ACCURACY_FLOOR,
    MIN_COLD_TARGETED,
    MIN_ELLIPTICAL,
    MIN_GENERIC,
    SearchQueryFixture,
    SearchQueryOutcome,
    SearchQueryReport,
    is_failing,
    load_search_query_fixtures,
)
from shared.tickers import TICKERS


def _elliptical_fixture(fid: str) -> SearchQueryFixture:
    return SearchQueryFixture(
        id=fid,
        ticker="NVDA",
        kind="elliptical",
        history=({"role": "user", "content": "Give me a thesis on NVDA."},),
        question="what about the buyback?",
        expected_needs_search=True,
        ticker_terms=("nvda",),
        topic_terms=("buyback",),
        corpus="news",
    )


def _outcome(fixture: SearchQueryFixture, *, query_ok_query: str) -> SearchQueryOutcome:
    return SearchQueryOutcome(
        fixture=fixture,
        resolved_intent="news",
        classifier_source="llm",
        actual_needs_search=True,
        search_query=query_ok_query,
        elapsed_ms=100,
    )


def test_fixtures_load_and_validate() -> None:
    fixtures = load_search_query_fixtures()
    assert fixtures, "no search-query fixtures loaded"


def test_fixture_ids_unique() -> None:
    ids = [f.id for f in load_search_query_fixtures()]
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"


def test_tickers_in_registry() -> None:
    for f in load_search_query_fixtures():
        assert f.ticker in TICKERS, f"{f.id}: unknown ticker {f.ticker!r}"


def test_per_kind_floors() -> None:
    fixtures = load_search_query_fixtures()
    counts = {
        kind: sum(1 for f in fixtures if f.kind == kind)
        for kind in ("elliptical", "cold_targeted", "generic")
    }
    assert counts["elliptical"] >= MIN_ELLIPTICAL
    assert counts["cold_targeted"] >= MIN_COLD_TARGETED
    assert counts["generic"] >= MIN_GENERIC


def test_elliptical_fixtures_carry_history_and_no_ticker_in_question() -> None:
    """The whole point of an elliptical fixture is that the question alone
    carries no ticker anchor -- history is REQUIRED to resolve it. If a
    fixture's raw question already names the ticker, it isn't testing the
    rewrite (it should be a cold_targeted fixture instead)."""
    for f in load_search_query_fixtures():
        if f.kind != "elliptical":
            continue
        assert f.history, f"{f.id}: elliptical fixture must carry history"
        question_lower = f.question.lower()
        assert not any(term.lower() in question_lower for term in f.ticker_terms), (
            f"{f.id}: elliptical fixture's question already names the ticker "
            f"({f.ticker_terms}) -- this isn't testing ellipsis resolution"
        )


def test_cold_and_generic_fixtures_carry_no_history() -> None:
    for f in load_search_query_fixtures():
        if f.kind in ("cold_targeted", "generic"):
            assert not f.history, f"{f.id}: {f.kind} fixture must not carry history"


def test_positive_fixtures_carry_both_term_lists() -> None:
    for f in load_search_query_fixtures():
        if f.expected_needs_search:
            assert f.ticker_terms, f"{f.id}: positive must carry ticker_terms"
            assert f.topic_terms, f"{f.id}: positive must carry topic_terms"
        else:
            assert not f.ticker_terms and not f.topic_terms, (
                f"{f.id}: negative must not carry ticker_terms/topic_terms"
            )


def test_positive_fixtures_carry_a_corpus_negatives_do_not() -> None:
    for f in load_search_query_fixtures():
        if f.expected_needs_search:
            assert f.corpus in ("news", "earnings"), f"{f.id}: positive must set corpus"
        else:
            assert f.corpus is None, f"{f.id}: negative must not carry corpus"


# ─── is_failing gate (review finding: needed an aggregate floor, not just a
# human reading summarise()'s percentages) ─────────────────────────────────


def test_is_failing_passes_when_elliptical_accuracy_clears_the_floor() -> None:
    fixtures = [_elliptical_fixture(f"e{i}") for i in range(8)]
    n_ok = round(ELLIPTICAL_QUERY_ACCURACY_FLOOR * len(fixtures)) + 1
    outcomes = [
        _outcome(f, query_ok_query="NVDA buyback" if i < n_ok else "")
        for i, f in enumerate(fixtures)
    ]
    report = SearchQueryReport(outcomes=tuple(outcomes))
    assert not is_failing(report)


def test_is_failing_fails_when_elliptical_accuracy_drops_below_the_floor() -> None:
    fixtures = [_elliptical_fixture(f"e{i}") for i in range(8)]
    n_ok = max(0, round(ELLIPTICAL_QUERY_ACCURACY_FLOOR * len(fixtures)) - 2)
    outcomes = [
        _outcome(f, query_ok_query="NVDA buyback" if i < n_ok else "")
        for i, f in enumerate(fixtures)
    ]
    report = SearchQueryReport(outcomes=tuple(outcomes))
    assert is_failing(report)


def test_is_failing_true_on_empty_report() -> None:
    assert is_failing(SearchQueryReport(outcomes=()))
