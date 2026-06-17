"""Config integrity tests for shared.tickers.TICKER_NAME_ALIASES (QNT-257).

The runtime path asserts ``set(TICKER_NAME_ALIASES) == set(TICKERS)`` at module
import; these tests give a clean failure signal in CI and pin a few additional
invariants the bare assert doesn't (non-empty aliases, no duplicate alias
mapping to two different tickers, aliases distinct from the bare symbol).
"""

from __future__ import annotations

from shared.tickers import TICKER_NAME_ALIASES, TICKERS


def test_name_aliases_cover_every_ticker() -> None:
    assert set(TICKER_NAME_ALIASES.keys()) == set(TICKERS)


def test_name_aliases_non_empty() -> None:
    for ticker, aliases in TICKER_NAME_ALIASES.items():
        assert isinstance(aliases, list) and aliases, f"{ticker} has no name aliases"


def test_name_aliases_do_not_repeat_the_bare_symbol() -> None:
    """Don't paste the literal symbol string as an alias — the parser matches
    the symbol separately, so it would be dead weight. Exact (case-sensitive)
    check: META's company name "Meta" is a legitimate alias even though it
    lowercase-collides with the symbol "META"."""
    for ticker, aliases in TICKER_NAME_ALIASES.items():
        assert ticker not in aliases, f"{ticker} pastes its symbol string in aliases"


def test_name_aliases_are_unambiguous() -> None:
    """No single alias maps to two different tickers (case-insensitive)."""
    seen: dict[str, str] = {}
    for ticker, aliases in TICKER_NAME_ALIASES.items():
        for alias in aliases:
            key = alias.lower()
            assert key not in seen, f"alias {alias!r} maps to both {seen[key]} and {ticker}"
            seen[key] = ticker
