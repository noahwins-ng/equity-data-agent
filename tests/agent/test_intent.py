"""Tests for agent.intent (QNT-149).

The classifier picks one of two response shapes for an inbound question:
``thesis`` (Setup / Bull / Bear / Verdict) or ``quick_fact`` (short prose +
a single cited value). The architecture deliberately defaults to ``thesis``
on any failure so the QNT-67 hallucination + QNT-128 golden-set contracts
can never regress because of a misbehaving classifier.

This module covers:
* The keyword heuristic that short-circuits common cases without an LLM call.
* The LLM-fallback path with a ``with_structured_output(IntentDecision)``
  stub that mimics the production path.
* The exception-handling contract: any LLM failure or unexpected return
  shape biases to ``thesis``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from agent import intent as intent_module
from agent.intent import IntentDecision, _heuristic_intent, classify_intent

# ─── Heuristic ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "question,expected",
    [
        # Quick-fact tokens, short questions
        ("What's the RSI?", "quick_fact"),
        ("What is NVDA's P/E?", "quick_fact"),
        ("Show me MACD for AAPL", "quick_fact"),
        ("EPS for MSFT?", "quick_fact"),
        ("What's the volume today?", "quick_fact"),
        # Thesis tokens win even when a quick-fact token also appears
        ("Give me a balanced thesis on V", "thesis"),
        ("Walk me through NVDA's setup", "thesis"),
        ("Should I buy AAPL?", "thesis"),
        ("Bull case for META", "thesis"),
        # Empty defers to thesis (safe default)
        ("", "thesis"),
    ],
)
def test_heuristic_classifies_known_phrases(question: str, expected: str) -> None:
    assert _heuristic_intent(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "What does NVDA's most recent fundamental picture look like?",
        "Triangulate technicals fundamentals and news for META",
        "Tell me about UNH",  # ambiguous open-ended
    ],
)
def test_heuristic_returns_none_for_ambiguous_questions(question: str) -> None:
    """Ambiguous mid-length questions should defer to the LLM rather than
    pick a wrong shape from a keyword false-positive."""
    assert _heuristic_intent(question) is None


def test_heuristic_does_not_match_substring_of_word() -> None:
    """Whole-word boundary check: 'priced' must not match a price-shaped
    token, 'macdonald' must not match 'macd'."""
    # Word boundary regression — "priced in" is rhetorical, not a price ask.
    assert _heuristic_intent("Is the bad news priced in for AMZN?") is None


def test_heuristic_does_not_misroute_price_target_questions() -> None:
    """Regression: a bare ``price`` token in _QUICK_FACT_TOKENS would
    mis-classify ``price target`` / ``price action`` asks (both thesis-
    shaped) as quick_fact and bias AWAY from the safe default. The
    classifier must defer these to the LLM (return None) instead."""
    # Both questions are thesis-shaped; if either auto-classifies as
    # quick_fact the bare ``price`` token has crept back in.
    assert _heuristic_intent("What is NVDA's price target right now?") != "quick_fact"
    assert _heuristic_intent("Tell me about price action for META") != "quick_fact"


def test_heuristic_quick_fact_token_in_long_question_defers_to_llm() -> None:
    """A long question that happens to contain "rsi" but is really a thesis
    ask must NOT auto-classify as quick_fact. The word-count guard catches
    this; an LLM call would resolve it correctly."""
    long_q = (
        "I'd like a comprehensive walk-through that covers the technical setup "
        "including RSI, MACD, and trend, the fundamental picture, and recent "
        "news flow for NVDA so I can decide on a position."
    )
    # Word count exceeds the short-question limit AND contains a thesis token
    # ("walk-through" doesn't match exactly, but the bias-to-thesis arm of
    # the heuristic kicks in only on explicit thesis tokens). Without a
    # thesis token it defers to the LLM.
    assert _heuristic_intent(long_q) is None


# ─── LLM fallback ─────────────────────────────────────────────────────────


def _patch_llm_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    structured_response: Any,
    *,
    invoke_raises: BaseException | None = None,
) -> MagicMock:
    """Replace ``intent.get_llm`` with a stub that returns ``structured_response``
    from ``with_structured_output(IntentDecision).invoke(...)``."""
    structured = MagicMock()
    if invoke_raises is not None:
        structured.invoke = MagicMock(side_effect=invoke_raises)
    else:
        structured.invoke = MagicMock(return_value=structured_response)
    llm = MagicMock()
    llm.with_structured_output = MagicMock(return_value=structured)
    monkeypatch.setattr(intent_module, "get_llm", MagicMock(return_value=llm))
    monkeypatch.setattr(
        intent_module.langfuse,
        "traced_invoke",
        lambda runnable, prompt, *, name: runnable.invoke(prompt),
    )
    return structured


def test_llm_fallback_returns_thesis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heuristic returns None → LLM fires → returns "thesis"."""
    _patch_llm_pipeline(monkeypatch, IntentDecision(intent="thesis"))
    assert classify_intent("Give me your read on UNH") == "thesis"


def test_llm_fallback_returns_quick_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heuristic returns None → LLM fires → returns "quick_fact"."""
    _patch_llm_pipeline(monkeypatch, IntentDecision(intent="quick_fact"))
    assert classify_intent("Tell me about UNH") == "quick_fact"


def test_llm_failure_defaults_to_thesis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any LLM exception biases to thesis — the safe default that preserves
    QNT-67 / QNT-128 contracts."""
    _patch_llm_pipeline(monkeypatch, None, invoke_raises=RuntimeError("network"))
    assert classify_intent("Tell me about UNH") == "thesis"


def test_llm_unexpected_shape_defaults_to_thesis(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``include_raw=True``-shaped failure (parsed=None) biases to thesis."""
    _patch_llm_pipeline(monkeypatch, {"parsed": None, "parsing_error": "x"})
    assert classify_intent("Tell me about UNH") == "thesis"


def test_llm_include_raw_dict_with_parsed_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``include_raw=True`` shape with a parsed IntentDecision is honoured
    so a future opt-in to raw logging keeps producing typed intents."""
    decision = IntentDecision(intent="quick_fact")
    _patch_llm_pipeline(monkeypatch, {"parsed": decision, "raw": "..."})
    assert classify_intent("Tell me about UNH") == "quick_fact"


def test_heuristic_short_circuits_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A heuristic-matched question must NOT call the LLM — saves token cost
    on the common case."""
    structured = _patch_llm_pipeline(monkeypatch, IntentDecision(intent="quick_fact"))
    assert classify_intent("What's the RSI?") == "quick_fact"
    assert structured.invoke.call_count == 0
