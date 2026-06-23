"""Offline validation + keyword-soundness for goldens/routing.yaml.

QNT-280 made the routing decision SEMANTIC (the classify LLM's flags), so full
accuracy is scored by the LIVE standalone runner (agent.evals.routing_eval),
not here. These offline tests run in the default unit sweep -- no tunnel, no
Qdrant, no LiteLLM -- and pin only what holds without the model:

* structure (ids unique, tickers in registry, per-class coverage floors);
* keyword SOUNDNESS -- the demoted keyword floor (_is_targeted_news /
  _is_earnings_search) may UNDER-fire vs the label (topical positives need the
  LLM), but it must never fire a corpus a fixture says should stay quiet. A
  keyword-floor hit on a "neither" / wrong-corpus fixture is a curation bug.
"""

from __future__ import annotations

from agent.evals.routing_eval import (
    MIN_BOTH,
    MIN_EARNINGS_ONLY,
    MIN_NEITHER,
    MIN_NEWS_ONLY,
    load_routing_fixtures,
)
from agent.intent import _is_earnings_search, _is_targeted_news, route_search_corpora
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


def test_keyword_floor_is_sound() -> None:
    """The demoted keyword floor must be SOUND: whatever it fires must be a
    subset of the fixture's expected_corpora. It may under-fire (topical
    positives like nvda-datacenter-switching need the live LLM and are scored by
    routing_eval), but a floor hit on a corpus the label does not list -- worst
    of all on a 'neither' fixture -- is a curation bug. This is the offline,
    model-free half of the routing contract."""
    for f in load_routing_fixtures():
        floor = frozenset(
            route_search_corpora(_is_targeted_news(f.question), _is_earnings_search(f.question))
        )
        assert floor <= f.expected_corpora, (
            f"{f.id}: keyword floor fired {sorted(floor)} but expected_corpora="
            f"{sorted(f.expected_corpora)} -- the floor must never over-fire"
        )
