"""QNT-244: clickable conversational/clarify suggestions must be answerable.

The prose answer stays LLM-generated, but the visible ``suggestions`` list is
validated/normalized so a cold "what can you do?" never offers a generic
placeholder ("trend for a specific stock?") that routes straight to clarify on
click. These tests pin the deterministic guardrail:

* invalid LLM suggestions are replaced (AC2)
* valid LLM suggestions pass through (AC2)
* deterministic fallback / bank suggestions stay in-scope (AC1, AC3)
* clarify needs_second_ticker suggests covered pairs (AC4)
* every starter suggestion routes to a non-clarify path (AC5)
"""

from __future__ import annotations

import pytest
from agent.conversational import (
    _SUGGESTION_BANK,
    _SUGGESTION_COUNT,
    coerce_suggestions,
    domain_redirect,
    is_answerable_suggestion,
)
from agent.graph import _detect_ambiguity
from agent.intent import Intent, extract_tickers
from shared.tickers import TICKERS

# The observed regression: broad LLM suggestions with placeholder scopes.
_GENERIC_LLM_SUGGESTIONS = [
    "What's the current trend for a specific stock?",
    "How does a company's valuation compare to its peers?",
    "What are the key drivers for a particular sector?",
]

_VALID_LLM_SUGGESTIONS = [
    "What's NVDA's RSI right now?",
    "How is MSFT valued relative to its earnings?",
    "Compare NVDA vs AAPL on valuation.",
]

# Bank label -> the intent a clicked suggestion of that shape classifies as.
_LABEL_TO_INTENT: dict[str, Intent] = {
    "technical": "technical",
    "fundamental": "fundamental",
    "news": "news",
    "thesis": "thesis",
    "comparison": "comparison",
}


# ─── is_answerable_suggestion ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "What's the current trend for a specific stock?",
        "How does a company's valuation compare to its peers?",
        "What are the key drivers for a particular sector?",
        "How is the broader market doing?",
        "What's the macro outlook?",
        "Should I buy this ETF?",
        "How is the SPY benchmark doing?",
        "What about crypto / bitcoin?",
        "Should I trade NVDA options?",
        "How should I allocate my portfolio across NVDA and AAPL?",
        "What's your price target for NVDA?",
        "Can you give me financial advice on NVDA?",
        "What's a good stock to buy?",  # no covered ticker at all
        "",
    ],
)
def test_rejects_placeholder_and_out_of_scope(text: str) -> None:
    assert is_answerable_suggestion(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "What's NVDA's RSI right now?",
        "How is MSFT valued relative to its earnings?",
        "Compare NVDA vs AAPL on valuation.",
        "What's NVDA's market cap?",  # "market cap" is a real metric, allowed
    ],
)
def test_accepts_concrete_in_scope(text: str) -> None:
    assert is_answerable_suggestion(text) is True


# ─── coerce_suggestions (AC2) ──────────────────────────────────────────────


def test_invalid_llm_suggestions_are_replaced() -> None:
    """AC2: generic placeholder suggestions fall back to deterministic ones."""
    coerced = coerce_suggestions(_GENERIC_LLM_SUGGESTIONS)
    assert len(coerced) == _SUGGESTION_COUNT
    assert coerced != _GENERIC_LLM_SUGGESTIONS
    assert all(is_answerable_suggestion(s) for s in coerced)


def test_valid_llm_suggestions_pass_through() -> None:
    """AC2: a list of three answerable suggestions is kept verbatim."""
    coerced = coerce_suggestions(_VALID_LLM_SUGGESTIONS)
    assert coerced == _VALID_LLM_SUGGESTIONS


def test_empty_suggestions_stay_empty() -> None:
    """A bare greeting carries no suggestions — don't force a card."""
    assert coerce_suggestions([]) == []


def test_incomplete_list_falls_back() -> None:
    """AC2: fewer than three answerable suggestions is replaced wholesale."""
    coerced = coerce_suggestions(["What's NVDA's RSI right now?"])
    assert len(coerced) == _SUGGESTION_COUNT
    assert all(is_answerable_suggestion(s) for s in coerced)


def test_mixed_valid_and_invalid_falls_back() -> None:
    """Two valid + one placeholder is incomplete -> deterministic fallback."""
    mixed = [
        "What's NVDA's RSI right now?",
        "How is MSFT trending?",
        "How does a company compare to its peers?",
    ]
    coerced = coerce_suggestions(mixed)
    assert len(coerced) == _SUGGESTION_COUNT
    assert all(is_answerable_suggestion(s) for s in coerced)


def test_comparison_hint_yields_covered_pairs() -> None:
    """AC4: the comparison hint biases to suggestions naming two tickers."""
    coerced = coerce_suggestions(_GENERIC_LLM_SUGGESTIONS, hint="comparison")
    assert len(coerced) == _SUGGESTION_COUNT
    for s in coerced:
        assert is_answerable_suggestion(s)
        named = extract_tickers(s)
        assert len(named) >= 2, f"comparison suggestion needs two tickers: {s!r}"


# ─── deterministic bank / domain_redirect (AC1, AC3) ───────────────────────


def test_every_bank_suggestion_is_answerable() -> None:
    """AC3: the centralized bank only holds in-scope, answerable prompts."""
    for label, question in _SUGGESTION_BANK:
        assert is_answerable_suggestion(question), f"{label}: {question!r}"


def test_bank_comparison_entries_name_two_tickers() -> None:
    for label, question in _SUGGESTION_BANK:
        if label == "comparison":
            assert len(extract_tickers(question)) >= 2, question


def test_domain_redirect_suggestions_valid() -> None:
    """AC1/AC3: the deterministic redirect ships exactly three answerable."""
    redirect = domain_redirect(reason="I had trouble answering that.", tickers=TICKERS)
    assert len(redirect.suggestions) == _SUGGESTION_COUNT
    assert all(is_answerable_suggestion(s) for s in redirect.suggestions)


# ─── routing (AC5) ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(("label", "question"), _SUGGESTION_BANK)
def test_starter_suggestion_does_not_route_to_clarify(label: str, question: str) -> None:
    """AC5: clicking any starter suggestion routes to an answerable path.

    A suggestion is only sent to clarify when it names no ticker. Each bank
    entry names its ticker(s), so ``_detect_ambiguity`` returns None for the
    shape that suggestion maps to — i.e. it routes to thesis / quick_fact /
    comparison / focused, never clarify.
    """
    intent = _LABEL_TO_INTENT[label]
    ambiguity = _detect_ambiguity(intent, question, has_prior_turn=False)
    assert ambiguity is None, f"{label} suggestion routed to clarify: {question!r}"
