"""Offline validation for goldens/rag_impact.yaml (QNT-277 AC1).

Mirrors test_news_search_yaml: locks the invariants the live runner can't catch
without spending tokens. These run in the default unit sweep; the live behavioral
assertion (agent.evals.rag_impact_eval) does NOT -- it needs the LiteLLM proxy.

The offline contracts here are AC1's other half: every positive plants a fact
that is genuinely retrieved-only (absent from the canned digest the harness also
stubs), the question routes to the matching deterministic search router, and the
stub payloads parse + carry the entity.
"""

from __future__ import annotations

import json

from agent.evals.rag_impact_eval import (
    MIN_EARNINGS_POSITIVES,
    MIN_MULTI_POSITIVES,
    MIN_NEGATIVES,
    MIN_NEWS_POSITIVES,
    _canned_reports,
    _earnings_hit_json,
    _news_hit_json,
    load_rag_impact_fixtures,
)
from agent.intent import _is_earnings_search, _is_targeted_news
from shared.tickers import TICKERS


def test_fixtures_load_and_validate() -> None:
    fixtures = load_rag_impact_fixtures()
    assert fixtures, "no rag-impact fixtures loaded"


def test_fixture_ids_unique() -> None:
    ids = [f.id for f in load_rag_impact_fixtures()]
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"


def test_tickers_in_registry() -> None:
    for f in load_rag_impact_fixtures():
        assert f.ticker in TICKERS, f"{f.id}: unknown ticker {f.ticker!r}"


def test_coverage_floors() -> None:
    fixtures = load_rag_impact_fixtures()
    positives = [f for f in fixtures if f.kind == "positive"]
    news_pos = sum(1 for f in positives if f.corpus == "news")
    earnings_pos = sum(1 for f in positives if f.corpus == "earnings")
    multi_pos = sum(1 for f in positives if f.corpus == "both")
    negatives = sum(1 for f in fixtures if f.kind == "negative_control")
    assert news_pos >= MIN_NEWS_POSITIVES, f"{news_pos} news positives"
    assert earnings_pos >= MIN_EARNINGS_POSITIVES, f"{earnings_pos} earnings positives"
    assert multi_pos >= MIN_MULTI_POSITIVES, f"{multi_pos} multi-corpus positives"
    assert negatives >= MIN_NEGATIVES, f"{negatives} negative controls"


def test_questions_fire_matching_router() -> None:
    """A fixture whose phrasing disagrees with its corpus is a curation bug, not
    a model miss. Pin it so the YAML and the deterministic routers can't drift --
    the offline half of the AC2 contract (the live run only re-confirms it and
    exercises the intent-label LLM path)."""
    for f in load_rag_impact_fixtures():
        if f.fires_news:
            assert _is_targeted_news(f.question), (
                f"{f.id}: corpus={f.corpus} but _is_targeted_news({f.question!r}) is False"
            )
        if f.fires_earnings:
            assert _is_earnings_search(f.question), (
                f"{f.id}: corpus={f.corpus} but _is_earnings_search({f.question!r}) is False"
            )


def test_planted_fact_absent_from_canned_digest() -> None:
    """AC1: the planted entity must be retrieved-only -- never present in the
    digest the report tools stub, so its appearance in an answer can only come
    from the retrieval hit."""
    for f in load_rag_impact_fixtures():
        for name, text in _canned_reports(f).items():
            assert f.planted_entity.lower() not in text.lower(), (
                f"{f.id}: planted entity leaked into canned {name} report"
            )
            assert f.planted_figure.lower() not in text.lower(), (
                f"{f.id}: planted figure leaked into canned {name} report"
            )


def test_stub_hits_parse_and_carry_entity() -> None:
    """The stub search payloads must be valid JSON in the shape the graph parses,
    and the planted entity must survive into the rendered fields."""
    for f in load_rag_impact_fixtures():
        if f.fires_news:
            rows = json.loads(_news_hit_json(f))
            assert rows and isinstance(rows, list)
            blob = json.dumps(rows).lower()
            assert f.planted_entity.lower() in blob, f"{f.id}: entity missing from news hit"
            assert "headline" in rows[0], f"{f.id}: news hit missing headline"
        if f.fires_earnings:
            rows = json.loads(_earnings_hit_json(f))
            assert rows and isinstance(rows, list)
            blob = json.dumps(rows).lower()
            assert f.planted_entity.lower() in blob, f"{f.id}: entity missing from earnings hit"
            assert "title" in rows[0], f"{f.id}: earnings hit missing title"
