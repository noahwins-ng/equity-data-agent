"""Offline validation for goldens/news_search.yaml (QNT-231 AC1).

Mirrors test_questions_yaml: locks the invariants the live runner can't catch
without spending tokens. These run in the default unit sweep; the live flag +
retrieval layers (agent.evals.news_search_eval) do NOT -- they need the tunnel,
LiteLLM, and live Qdrant.
"""

from __future__ import annotations

from agent.evals.news_search_eval import (
    MIN_NEGATIVES,
    MIN_POSITIVES,
    load_news_search_fixtures,
)
from agent.intent import _is_targeted_news
from shared.tickers import TICKERS


def test_fixtures_load_and_validate() -> None:
    fixtures = load_news_search_fixtures()
    assert fixtures, "no news-search fixtures loaded"


def test_fixture_ids_unique() -> None:
    ids = [f.id for f in load_news_search_fixtures()]
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"


def test_tickers_in_registry() -> None:
    for f in load_news_search_fixtures():
        assert f.ticker in TICKERS, f"{f.id}: unknown ticker {f.ticker!r}"


def test_positive_negative_floors() -> None:
    fixtures = load_news_search_fixtures()
    positives = sum(1 for f in fixtures if f.expected_news_search)
    negatives = len(fixtures) - positives
    assert positives >= MIN_POSITIVES, f"{positives} positives < {MIN_POSITIVES}"
    assert negatives >= MIN_NEGATIVES, f"{negatives} negatives < {MIN_NEGATIVES}"


def test_positives_carry_expected_terms() -> None:
    for f in load_news_search_fixtures():
        if f.expected_news_search:
            assert f.expected_terms, f"{f.id}: positive must carry expected_terms"
        else:
            assert not f.expected_terms, f"{f.id}: negative must not carry expected_terms"


def test_fixture_phrasing_matches_deterministic_router() -> None:
    """The flag is deterministic (_is_targeted_news, QNT-229), so a fixture
    whose phrasing disagrees with its expected_news_search is a curation bug,
    not a model miss. Pin it here so the YAML and the router can't drift -- this
    is the offline half of the AC2 flag contract (the live run only re-confirms
    it and exercises the intent-label LLM path)."""
    for f in load_news_search_fixtures():
        actual = _is_targeted_news(f.question)
        assert actual == f.expected_news_search, (
            f"{f.id}: _is_targeted_news({f.question!r})={actual} "
            f"but expected_news_search={f.expected_news_search}"
        )
