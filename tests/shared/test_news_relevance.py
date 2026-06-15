"""Config integrity tests for shared.tickers.NEWS_RELEVANCE (QNT-173).

The runtime path also asserts ``set(NEWS_RELEVANCE) == set(TICKERS)`` at
module import, but a CI test gives us a clean failure signal in the test
report (rather than an opaque ``AssertionError`` during collection) and
covers a few additional invariants that the runtime assert doesn't:

* Every entry has a non-empty alias list.
* Every scope is one of the two supported values.
* The symbol itself isn't accidentally trimmed from a ticker that needs a
  body match (e.g. NVDA aliases must include "NVDA"). META is exempt — by
  design it drops the bare symbol from the alias list because "meta" is
  noisy in prose. (INTC is also scope=headline but keeps "INTC" since the
  symbol itself has no prose collision.)
"""

from __future__ import annotations

from shared.tickers import NEWS_RELEVANCE, TICKERS

_HEADLINE_ONLY_NO_SYMBOL_TICKERS = {"META"}


def test_news_relevance_covers_every_ticker() -> None:
    assert set(NEWS_RELEVANCE.keys()) == set(TICKERS)


def test_news_relevance_aliases_non_empty() -> None:
    for ticker, cfg in NEWS_RELEVANCE.items():
        aliases = cfg["aliases"]
        assert isinstance(aliases, list) and aliases, f"{ticker} has no aliases"


def test_news_relevance_scope_is_known() -> None:
    for ticker, cfg in NEWS_RELEVANCE.items():
        assert cfg["scope"] in {"any", "headline"}, f"{ticker} has unknown scope"


def test_news_relevance_includes_symbol_for_any_scope_tickers() -> None:
    """scope=any tickers must include their bare symbol in aliases.

    META deliberately excludes the bare symbol because "meta" is a
    high-false-positive substring; that exemption is documented in
    NEWS_RELEVANCE itself.
    """
    for ticker, cfg in NEWS_RELEVANCE.items():
        if ticker in _HEADLINE_ONLY_NO_SYMBOL_TICKERS:
            continue
        aliases = cfg["aliases"]
        assert isinstance(aliases, list)
        assert ticker in aliases, (
            f"{ticker} aliases must include the bare symbol for scope=any tickers"
        )
