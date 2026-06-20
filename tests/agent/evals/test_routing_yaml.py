"""Offline validation + correctness for goldens/routing.yaml (QNT-263 AC3).

Mirrors test_news_search_yaml: the multi-corpus router is deterministic
(agent.intent.route_search_corpora), so these run in the default unit sweep --
no tunnel, no Qdrant, no LiteLLM. The standalone scorecard
(agent.evals.routing_eval) is the PR artifact; this file pins the invariants
the YAML and the router can't be allowed to drift apart on.
"""

from __future__ import annotations

from agent.evals.routing_eval import (
    MIN_BOTH,
    MIN_EARNINGS_ONLY,
    MIN_NEITHER,
    MIN_NEWS_ONLY,
    is_failing,
    load_routing_fixtures,
    run_all,
)
from agent.intent import route_search_corpora
from shared.tickers import TICKERS


def test_fixtures_load_and_validate() -> None:
    fixtures = load_routing_fixtures()
    assert fixtures, "no routing fixtures loaded"


def test_fixture_ids_unique() -> None:
    ids = [f.id for f in load_routing_fixtures()]
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"


def test_tickers_in_registry() -> None:
    for f in load_routing_fixtures():
        assert f.ticker in TICKERS, f"{f.id}: unknown ticker {f.ticker!r}"


def test_class_coverage_floors() -> None:
    fixtures = load_routing_fixtures()
    counts = {"news_only": 0, "earnings_only": 0, "both": 0, "neither": 0}
    for f in fixtures:
        counts[f.routing_class] += 1
    assert counts["news_only"] >= MIN_NEWS_ONLY
    assert counts["earnings_only"] >= MIN_EARNINGS_ONLY
    assert counts["both"] >= MIN_BOTH
    assert counts["neither"] >= MIN_NEITHER


def test_router_matches_expected_corpora() -> None:
    """The router is deterministic, so a fixture whose phrasing disagrees with
    its expected_corpora is a curation bug. This is the offline half of the AC3
    routing contract -- the standalone scorecard only re-confirms it."""
    for f in load_routing_fixtures():
        actual = frozenset(route_search_corpora(f.question))
        assert actual == f.expected_corpora, (
            f"{f.id}: route_search_corpora({f.question!r})={sorted(actual)} "
            f"but expected_corpora={sorted(f.expected_corpora)}"
        )


def test_run_all_passes_the_gate() -> None:
    """The full set must route cleanly -- the gate the standalone runner enforces."""
    report = run_all()
    assert not is_failing(report), "routing eval has misrouted fixtures:\n" + "\n".join(
        f"  {o.fixture.id}: expected={sorted(o.fixture.expected_corpora)} "
        f"actual={sorted(o.actual_corpora)}"
        for o in report.misses
    )
