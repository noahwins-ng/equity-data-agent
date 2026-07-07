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
        ("Give me a balanced thesis on AMD", "thesis"),
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
        "Triangulate technicals fundamentals and news for META",
        "Tell me about INTC",  # ambiguous open-ended
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
    # The question mentions both "technical setup" and "fundamental picture",
    # so focused_hits = ["technical", "fundamental"]. The mutual-exclusivity
    # guard (len > 1) prevents any focused intent from firing, and no thesis
    # token matches either, so the heuristic defers to the LLM.
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
    # QNT-181: classify_intent calls structured_llm.invoke(prompt, config=...)
    # directly now that traced_invoke is gone. The MagicMock accepts the extra
    # ``config=`` kwarg unchanged so no extra patching is required here.
    return structured


def test_llm_fallback_returns_thesis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heuristic returns None → LLM fires → returns "thesis"."""
    _patch_llm_pipeline(monkeypatch, IntentDecision(intent="thesis"))
    assert classify_intent("Give me your read on INTC") == "thesis"


def test_llm_fallback_returns_quick_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heuristic returns None → LLM fires → returns "quick_fact"."""
    _patch_llm_pipeline(monkeypatch, IntentDecision(intent="quick_fact"))
    assert classify_intent("Tell me about INTC") == "quick_fact"


def test_llm_failure_defaults_to_thesis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any LLM exception biases to thesis — the safe default that preserves
    QNT-67 / QNT-128 contracts."""
    _patch_llm_pipeline(monkeypatch, None, invoke_raises=RuntimeError("network"))
    assert classify_intent("Tell me about INTC") == "thesis"


def test_llm_unexpected_shape_defaults_to_thesis(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``include_raw=True``-shaped failure (parsed=None) biases to thesis."""
    _patch_llm_pipeline(monkeypatch, {"parsed": None, "parsing_error": "x"})
    assert classify_intent("Tell me about INTC") == "thesis"


def test_llm_include_raw_dict_with_parsed_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``include_raw=True`` shape with a parsed IntentDecision is honoured
    so a future opt-in to raw logging keeps producing typed intents."""
    decision = IntentDecision(intent="quick_fact")
    _patch_llm_pipeline(monkeypatch, {"parsed": decision, "raw": "..."})
    assert classify_intent("Tell me about INTC") == "quick_fact"


def test_llm_classifier_uses_small_tiering_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """QNT-220 (#7): when the heuristic abstains the LLM classifier must resolve
    the small tiering alias, not the 70b default."""
    from agent.llm import SMALL_NODE_ALIAS

    structured = MagicMock()
    structured.invoke = MagicMock(return_value=IntentDecision(intent="thesis"))
    llm = MagicMock()
    llm.with_structured_output = MagicMock(return_value=structured)
    get_llm_spy = MagicMock(return_value=llm)
    monkeypatch.setattr(intent_module, "get_llm", get_llm_spy)

    classify_intent("Give me your read on INTC")  # heuristic abstains -> LLM fires

    assert get_llm_spy.call_args.kwargs.get("model_alias") == SMALL_NODE_ALIAS


def test_heuristic_short_circuits_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A heuristic-matched question must NOT call the LLM — saves token cost
    on the common case."""
    structured = _patch_llm_pipeline(monkeypatch, IntentDecision(intent="quick_fact"))
    assert classify_intent("What's the RSI?") == "quick_fact"
    assert structured.invoke.call_count == 0


# ─── QNT-156: comparison + conversational heuristic ───────────────────────


@pytest.mark.parametrize(
    "question",
    [
        "Compare NVDA vs AAPL on valuation.",
        "How does META stack up against GOOGL on margins?",
        "Which is cheaper, MU or INTC?",
        "NVDA vs AAPL",
        "AAPL versus MSFT — which is the better buy?",
    ],
)
def test_heuristic_classifies_comparison_with_two_tickers(question: str) -> None:
    """Two named tickers + a comparison phrase should heuristically route
    to ``comparison`` without an LLM call."""
    from agent.intent import _heuristic_intent

    assert _heuristic_intent(question) == "comparison"


def test_heuristic_does_not_classify_single_ticker_comparison_phrase() -> None:
    """A comparison phrase with only ONE named ticker is ambiguous — the
    user might be comparing to a synthetic peer not in our coverage. The
    heuristic must NOT fire ``comparison`` (we'd have nothing to compare
    against). Either the heuristic falls through to thesis/quick_fact for
    a different reason, or it defers to the LLM — both are acceptable;
    the only forbidden outcome is ``comparison``."""
    from agent.intent import _heuristic_intent

    # One ticker + a comparison phrase — must NOT trigger the comparison
    # branch (we can't satisfy it). Heuristic may still pick another
    # shape via downstream checks.
    assert _heuristic_intent("How does NVDA compare to the broader chip sector?") != "comparison"


def test_heuristic_does_not_classify_three_tickers_as_comparison() -> None:
    """Three tickers + comparison phrase: the heuristic still routes to
    comparison (the synthesize node clips to 2 and the redirect handles
    the overflow case). The LLM doesn't need to disambiguate this."""
    from agent.intent import _heuristic_intent

    # Three tickers — heuristic still says comparison; graph clips to 2.
    assert _heuristic_intent("Compare NVDA vs AAPL vs MSFT on margins") == "comparison"


@pytest.mark.parametrize(
    "question",
    [
        "hi",
        "hello",
        "hey",
        "Hi!",
        "Hello?",
        # Common greeting variants / misspellings — a mistyped hello used to
        # fall through to the LLM and get bounced as off-domain.
        "halo",
        "hallow",
        "hiya",
        "hello there",
        "sup",
        "What can you do?",
        "what do you do",
        "How does this work?",
        "What's the weather?",
        "tell me a joke",
        "Sing me a song",
    ],
)
def test_heuristic_classifies_conversational(question: str) -> None:
    """Greetings, capability asks, and clearly off-domain inputs should
    heuristically route to ``conversational`` without an LLM call."""
    from agent.intent import _heuristic_intent

    assert _heuristic_intent(question) == "conversational"


def test_heuristic_ambiguous_open_ended_about_ticker_is_not_conversational() -> None:
    """An open-ended ticker question that happens to start with a word
    overlapping the conversational vocabulary must not route to
    conversational. ``Tell me about INTC`` is the canonical example —
    QNT-149 has it as the headline ambiguity case."""
    from agent.intent import _heuristic_intent

    assert _heuristic_intent("Tell me about INTC") is None


def test_extract_tickers_handles_ordering_and_dupes() -> None:
    """``extract_tickers`` returns first-occurrence order, dedup'd, only
    matching shared.tickers.TICKERS — used by the comparison-resolution
    path in graph.py."""
    from agent.intent import extract_tickers

    assert extract_tickers("Compare NVDA vs AAPL and NVDA again") == ["NVDA", "AAPL"]
    assert extract_tickers("no tickers here") == []
    # Boundary check: NVDA inside a longer alpha run does NOT match
    assert extract_tickers("nvdaily") == []


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        # QNT-257: plain company name resolves to the ticker (the symptom).
        ("what's the thesis of micron", ["MU"]),
        ("what's the thesis of Micron", ["MU"]),
        ("google vs tesla", ["GOOGL", "TSLA"]),
        ("is Apple a buy?", ["AAPL"]),
        ("thoughts on Alphabet", ["GOOGL"]),
        ("how does Advanced Micro Devices look", ["AMD"]),
    ],
)
def test_extract_tickers_resolves_company_names(question: str, expected: list[str]) -> None:
    from agent.intent import extract_tickers

    assert extract_tickers(question) == expected


def test_extract_tickers_collapses_symbol_and_name() -> None:
    """A symbol and its company name in one question yield one entry (QNT-257)."""
    from agent.intent import extract_tickers

    assert extract_tickers("compare Micron and MU") == ["MU"]
    assert extract_tickers("MU vs micron") == ["MU"]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        # Collision guards (QNT-257): accepted resolutions for prose-colliding names.
        ("is intel a buy?", ["INTC"]),
        ("news on Facebook", ["META"]),
        ("thesis on Meta", ["META"]),
        # ...and the no-false-positive cases the word boundary protects.
        ("show me the metadata for this run", []),
        ("any intelligence on the sector?", []),
    ],
)
def test_extract_tickers_collision_guards(question: str, expected: list[str]) -> None:
    from agent.intent import extract_tickers

    assert extract_tickers(question) == expected


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        # ACCEPTED trade-offs (QNT-257): the company short name is itself an
        # English/unit word, so these resolve to the ticker. This is the
        # deliberate cost of plain-name resolution — you can't catch "tesla's
        # thesis" without also catching "tesla coil" absent context-aware NER
        # (out of scope). Pinned so a future alias widener sees the risk surface;
        # the alpha-only boundary is intentional (blocking hyphens would drop
        # legitimate "Micron-based" / "Tesla-built").
        ("a tesla coil experiment", ["TSLA"]),
        ("apple juice recipe", ["AAPL"]),
        ("the amazon rainforest", ["AMZN"]),
        ("just google it", ["GOOGL"]),
        ("sub-micron lithography", ["MU"]),
    ],
)
def test_extract_tickers_accepted_common_word_collisions(
    question: str, expected: list[str]
) -> None:
    from agent.intent import extract_tickers

    assert extract_tickers(question) == expected


def test_llm_fallback_returns_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heuristic returns None → LLM picks comparison."""
    _patch_llm_pipeline(monkeypatch, IntentDecision(intent="comparison"))
    assert (
        classify_intent("How would you contrast INTC and the broader semiconductor names?")
        == "comparison"
    )


def test_llm_fallback_returns_conversational(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heuristic returns None → LLM picks conversational."""
    _patch_llm_pipeline(monkeypatch, IntentDecision(intent="conversational"))
    assert classify_intent("Are you a chatbot or what exactly?") == "conversational"


def test_heuristic_help_phrase_with_ticker_does_not_misclassify_as_conversational() -> None:
    """Regression (review finding): "help me understand NVDA's RSI" used to
    heuristically route to conversational because "help me" is in the
    conversational token list. With a ticker named in the question the
    heuristic must defer to the LLM (or fall through to a downstream
    branch) — never fire conversational on a question that's clearly
    about a covered equity."""
    from agent.intent import _heuristic_intent

    assert _heuristic_intent("help me understand NVDA's RSI") != "conversational"
    assert _heuristic_intent("help me with AAPL fundamentals") != "conversational"
    # No-ticker conversational asks still fire correctly.
    assert _heuristic_intent("help me figure out how this works") == "conversational"


# ─── QNT-176: focused-analysis heuristic ────────────────────────────────────


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Give me a fundamental analysis of NVDA", "fundamental"),
        ("Walk me through META's fundamentals", "fundamental"),
        ("valuation deep dive on AAPL", "fundamental"),
        ("technical analysis of NVDA", "technical"),
        ("how do the technicals look on AAPL?", "technical"),
        ("TA on TSLA please", "technical"),
        ("chart setup for MSFT", "technical"),
        ("What's the news sentiment on AAPL?", "news"),
        ("what is the sentiment for INTC?", "news"),
        ("give me a news read on META", "news"),
    ],
)
def test_heuristic_classifies_focused_intents(question: str, expected: str) -> None:
    """Each focused-analysis trigger phrase routes to the matching intent
    without an LLM call."""
    from agent.intent import _heuristic_intent

    assert _heuristic_intent(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        # Review finding: bare 'sentiment on' / 'headlines on' tripped the
        # heuristic on phrases that name no ticker and target no domain.
        # These must NOT route to news.
        "What's the market sentiment on the sector right now?",
        "Based on recent sentiment on Wall Street, what's next?",
        "Headlines on the bond market today",
    ],
)
def test_heuristic_does_not_misfire_on_overbroad_sentiment_phrases(question: str) -> None:
    """Regression guard for the QNT-176 review finding — the focused
    heuristic must not capture non-domain-specific sentiment talk."""
    from agent.intent import _heuristic_intent

    assert _heuristic_intent(question) != "news"


def test_heuristic_focused_loses_to_thesis_when_multiple_focuses_named() -> None:
    """If the user names multiple report families in one breath ("triangulate
    technicals AND fundamentals"), they want a thesis, not a single-domain
    read. The focused branch must defer in that case."""
    from agent.intent import _heuristic_intent

    # Two focuses → not a single focused intent; falls through to None or
    # thesis depending on later checks. The forbidden outcome is any single
    # focused intent.
    result = _heuristic_intent("Triangulate technicals fundamentals and news for META")
    assert result not in {"fundamental", "technical", "news"}


def test_heuristic_focused_loses_to_explicit_thesis_token() -> None:
    """When BOTH a focused token and a thesis token appear, the focused
    branch wins because we check it FIRST — but only when it's an
    unambiguous focused phrasing. ``walk me through META's fundamentals``
    is the canonical focused ask the QNT-176 ticket calls out."""
    from agent.intent import _heuristic_intent

    assert _heuristic_intent("walk me through META's fundamentals") == "fundamental"


def test_heuristic_short_circuits_focused_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """A heuristic-matched focused question must NOT call the LLM."""
    structured = _patch_llm_pipeline(monkeypatch, IntentDecision(intent="thesis"))
    assert classify_intent("technical analysis of NVDA") == "technical"
    assert structured.invoke.call_count == 0


def test_llm_fallback_returns_focused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heuristic returns None → LLM picks one of the focused intents."""
    _patch_llm_pipeline(monkeypatch, IntentDecision(intent="fundamental"))
    assert classify_intent("How is MSFT looking from a value angle?") == "fundamental"


# ─── QNT-186: heuristic-token expansion ──────────────────────────────────────


@pytest.mark.parametrize(
    "question,expected",
    [
        # Technical: new phrasings from QNT-186 scope
        ("Walk me through TSLA technical setup", "technical"),
        ("what do the charts say for NVDA?", "technical"),
        ("is AAPL overbought?", "technical"),
        ("is TSLA oversold right now?", "technical"),
        # Fundamental: new phrasings from QNT-186 scope
        ("valuation read on META", "fundamental"),
        ("what does the balance sheet say about AAPL?", "fundamental"),
        ("is NVDA expensive?", "fundamental"),
        # News sentiment: new phrasings from QNT-186 scope
        ("what's the news say on META?", "news"),
        ("how is sentiment on AAPL?", "news"),
        ("any catalysts for TSLA?", "news"),
    ],
)
def test_heuristic_classifies_qnt186_expanded_phrasings(question: str, expected: str) -> None:
    """QNT-186 natural-language phrasings route to the correct focused intent
    without an LLM call."""
    from agent.intent import _heuristic_intent

    assert _heuristic_intent(question) == expected


def test_heuristic_walk_through_without_technical_stays_thesis() -> None:
    """'Walk me through NVDA's setup' still routes to thesis — the new
    'technical setup' token must not capture a generic walk-through ask
    that lacks the word 'technical'."""
    from agent.intent import _heuristic_intent

    assert _heuristic_intent("Walk me through NVDA's setup") == "thesis"


# ─── QNT-189: classify_intent_with_source ───────────────────────────────────


def test_with_source_heuristic_path() -> None:
    """A heuristic-matched question returns source='heuristic'."""
    from agent.intent import classify_intent_with_source

    intent, source, _flag, _earn, _query, *_ = classify_intent_with_source("What's the RSI?")
    assert intent == "quick_fact"
    assert source == "heuristic"


def test_with_source_llm_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heuristic abstains, LLM succeeds — source='llm'."""
    _patch_llm_pipeline(monkeypatch, IntentDecision(intent="thesis"))
    from agent.intent import classify_intent_with_source

    intent, source, _flag, _earn, _query, *_ = classify_intent_with_source("Tell me about INTC")
    assert intent == "thesis"
    assert source == "llm"


def test_with_source_fallback_path_on_llm_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM raises — source='fallback', intent defaults to 'thesis'."""
    _patch_llm_pipeline(monkeypatch, None, invoke_raises=RuntimeError("timeout"))
    from agent.intent import classify_intent_with_source

    intent, source, _flag, _earn, _query, *_ = classify_intent_with_source("Tell me about INTC")
    assert intent == "thesis"
    assert source == "fallback"


def test_with_source_fallback_path_on_unexpected_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM returns unparseable shape — source='fallback', intent defaults to 'thesis'."""
    _patch_llm_pipeline(monkeypatch, {"parsed": None, "parsing_error": "x"})
    from agent.intent import classify_intent_with_source

    intent, source, _flag, _earn, _query, *_ = classify_intent_with_source("Tell me about INTC")
    assert intent == "thesis"
    assert source == "fallback"


def test_with_source_llm_include_raw_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """include_raw=True dict with a parsed IntentDecision → source='llm'."""
    decision = IntentDecision(intent="quick_fact")
    _patch_llm_pipeline(monkeypatch, {"parsed": decision, "raw": "..."})
    from agent.intent import classify_intent_with_source

    intent, source, _flag, _earn, _query, *_ = classify_intent_with_source("Tell me about INTC")
    assert intent == "quick_fact"
    assert source == "llm"


# ─── QNT-327: folded thesis plan pick (report_picks / plan_rationale) ─────────


def test_folded_picks_surface_on_llm_thesis_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """QNT-327 (v3 G-6): a thesis IntentDecision surfaces report_picks +
    plan_rationale as the 6th/7th tuple elements on the llm path."""
    _patch_llm_pipeline(
        monkeypatch,
        IntentDecision(
            intent="thesis",
            report_picks=["company", "fundamental"],
            plan_rationale="Valuation-led read.",
        ),
    )
    from agent.intent import classify_intent_with_source

    intent, source, _news, _earn, _query, report_picks, plan_rationale = (
        classify_intent_with_source("is AAPL a buy?")
    )
    assert (intent, source) == ("thesis", "llm")
    assert report_picks == ["company", "fundamental"]
    assert plan_rationale == "Valuation-led read."


def test_folded_picks_suppressed_on_non_thesis_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The picks are gated on intent=='thesis': a stray pick on a non-thesis
    decision (which plan_node never reads) is dropped so it can't leak into state."""
    _patch_llm_pipeline(
        monkeypatch,
        IntentDecision(
            intent="quick_fact",
            report_picks=["company", "technical"],
            plan_rationale="should be dropped",
        ),
    )
    from agent.intent import classify_intent_with_source

    intent, _source, _news, _earn, _query, report_picks, plan_rationale = (
        classify_intent_with_source("what's AAPL's P/E?")
    )
    assert intent == "quick_fact"
    assert report_picks == []
    assert plan_rationale == ""


def test_folded_picks_empty_on_heuristic_path() -> None:
    """No LLM ran -> no folded picks; plan_node falls back to the ThesisPlan call."""
    from agent.intent import classify_intent_with_source

    # A thesis-token heuristic short-circuit (no LLM) still returns empty picks.
    intent, source, _news, _earn, _query, report_picks, plan_rationale = (
        classify_intent_with_source("give me a balanced thesis on NVDA")
    )
    assert source == "heuristic"
    assert (report_picks, plan_rationale) == ([], "")


# ─── QNT-280: semantic needs_news_search / needs_earnings_search flags ────────


def test_needs_news_search_honours_llm_flag_on_llm_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """QNT-280: the LLM's semantic flag is the primary trigger on the llm path."""
    _patch_llm_pipeline(monkeypatch, IntentDecision(intent="quick_fact", needs_news_search=True))
    from agent.intent import classify_intent_with_source

    intent, source, needs_news_search, _earn, _query, *_ = classify_intent_with_source(
        "what did the CEO say about the buyback?"
    )
    assert (intent, source) == ("quick_fact", "llm")
    assert needs_news_search is True


def test_needs_news_search_semantic_flag_catches_topical_phrasing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing QNT-280 case: a topical/competitive ask that carries NO
    keyword token (_is_targeted_news is False) still fires because the LLM's
    semantic flag is True. This is the exact prod miss the ticket fixes."""
    from agent.intent import _is_targeted_news, classify_intent_with_source

    question = "What's the latest on Nvidia in the data center switching / networking market?"
    # Precondition: the keyword floor genuinely cannot reach this phrasing.
    assert _is_targeted_news(question) is False
    _patch_llm_pipeline(monkeypatch, IntentDecision(intent="news", needs_news_search=True))

    intent, source, needs_news_search, _earn, _query, *_ = classify_intent_with_source(question)
    assert (intent, source) == ("news", "llm")
    assert needs_news_search is True


def test_needs_news_search_generic_ask_stays_off_when_both_signals_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-280 generic-suppression WIRING pin (offline regression anchor).

    Under the new contract the LLM owns the generic-ask judgment (it is prompted
    to return False for "what's the news on AAPL?"); the keyword floor is also
    False there. This pins that when BOTH signals are False the wiring fires
    nothing -- the OR must not invent a True. (The model's live judgment on
    generics is guarded separately by routing_eval's false-positive hard-gate;
    the old "keyword overrides LLM True" suppression was deliberately removed by
    QNT-280, so we assert the model-trusting wiring, not a keyword override.)"""
    from agent.intent import _is_targeted_news, classify_intent_with_source

    question = "what's the news on AAPL?"
    assert _is_targeted_news(question) is False  # floor stays off on a generic ask
    _patch_llm_pipeline(monkeypatch, IntentDecision(intent="news", needs_news_search=False))

    intent, source, needs_news_search, _earn, _query, *_ = classify_intent_with_source(question)
    assert (intent, source) == ("news", "llm")
    assert needs_news_search is False


def test_needs_news_search_keyword_floor_rescues_llm_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-280: the keyword decider is demoted to a recall FLOOR -- an obvious
    named event ("collaboration") the small model overlooks still fires via OR."""
    _patch_llm_pipeline(monkeypatch, IntentDecision(intent="news", needs_news_search=False))
    from agent.intent import classify_intent_with_source

    intent, source, needs_news_search, _earn, _query, *_ = classify_intent_with_source(
        "any news on NVDA collaboration with Nokia?"
    )
    assert (intent, source) == ("news", "llm")
    assert needs_news_search is True


def test_needs_news_search_keyword_floor_on_heuristic_path() -> None:
    """When the heuristic short-circuits (no LLM), the keyword floor carries the
    signal -- "any catalysts" is a heuristic news hit AND a targeted token."""
    from agent.intent import classify_intent_with_source

    intent, source, needs_news_search, _earn, _query, *_ = classify_intent_with_source(
        "any catalysts for NVDA?"
    )
    assert source == "heuristic"
    assert needs_news_search is True


def test_needs_earnings_search_honours_llm_flag_on_llm_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-280: needs_earnings_search is carried by the same semantic classify
    call. A narrative ask phrased without a keyword token still fires via the LLM
    flag (keyword floor here is False)."""
    from agent.intent import _is_earnings_search, classify_intent_with_source

    question = "How did the leadership team characterise the quarter on the call?"
    assert _is_earnings_search(question) is False
    _patch_llm_pipeline(
        monkeypatch, IntentDecision(intent="fundamental", needs_earnings_search=True)
    )

    intent, source, _news, needs_earnings_search, _query, *_ = classify_intent_with_source(question)
    assert (intent, source) == ("fundamental", "llm")
    assert needs_earnings_search is True


def test_needs_earnings_search_keyword_floor_rescues_llm_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The earnings keyword decider is the recall floor: "guidance" fires even
    when the LLM flag is False."""
    _patch_llm_pipeline(
        monkeypatch, IntentDecision(intent="fundamental", needs_earnings_search=False)
    )
    from agent.intent import classify_intent_with_source

    _intent, source, _news, needs_earnings_search, _query, *_ = classify_intent_with_source(
        "what did management say about guidance?"
    )
    assert source == "llm"
    assert needs_earnings_search is True


def test_is_targeted_news_distinguishes_targeted_from_generic() -> None:
    from agent.intent import _is_targeted_news

    assert _is_targeted_news("any litigation news on NVDA?") is True
    assert _is_targeted_news("what did the CEO say about the buyback?") is True
    assert _is_targeted_news("any recall news on TSLA?") is True
    assert _is_targeted_news("what's the latest on the Micron partnership?") is True
    assert _is_targeted_news("any news on NVDA collaboration with Nokia?") is True
    assert _is_targeted_news("what the latest news NVDA with SK Hynix?") is True
    assert _is_targeted_news("headlines on META about antitrust?") is True
    # Generic news asks name no specific event/entity.
    assert _is_targeted_news("what's the news on AAPL?") is False
    assert _is_targeted_news("what's the latest news on NVDA?") is False
    assert _is_targeted_news("what's the latest on NVDA?") is False
    assert _is_targeted_news("headlines on META") is False
    # Whole-word matching: "sue" must not fire on "issue", "sec" not on "second".
    assert _is_targeted_news("what are the issues this second?") is False


# ─── QNT-263: multi-corpus routing (news vs 8-K earnings) ────────────────────


def test_is_earnings_search_fires_on_narrative_asks() -> None:
    from agent.intent import _is_earnings_search

    assert _is_earnings_search("what did management say about guidance?") is True
    assert _is_earnings_search("what was NVDA's outlook for next quarter?") is True
    assert _is_earnings_search("summarize AAPL's latest earnings release") is True
    assert _is_earnings_search("what did NVDA say on the earnings call?") is True
    assert _is_earnings_search("management commentary on margins") is True
    assert _is_earnings_search("what was Intel's forward guidance?") is True
    # The numbers (P/E, revenue, RSI) are NOT RAG material -- they flow through
    # the fundamental report, so a bare metric ask must not fire earnings search.
    assert _is_earnings_search("what's NVDA's P/E?") is False
    assert _is_earnings_search("what's the RSI on TSLA?") is False
    assert _is_earnings_search("what's the news on AAPL?") is False
    # Whole-word matching: "guided" token must not fire on an unrelated substring.
    assert _is_earnings_search("what are the misguidedness issues?") is False


def test_route_search_corpora_composes_the_two_flags() -> None:
    """QNT-280: route_search_corpora is a pure OR over the two resolved flags
    (the runtime + eval share this one composition point), not a re-derivation
    from the question text."""
    from agent.intent import route_search_corpora

    # news-only.
    assert route_search_corpora(True, False) == ("news",)
    # earnings-only.
    assert route_search_corpora(False, True) == ("earnings",)
    # both -> ordered news,earnings.
    assert route_search_corpora(True, True) == ("news", "earnings")
    # neither -> canned digests carry it.
    assert route_search_corpora(False, False) == ()


# ─── QNT-289: search_query rewrite + guardrails ───────────────────────────────


def test_sanitize_search_query_passes_a_clean_rewrite() -> None:
    from agent.intent import sanitize_search_query

    assert sanitize_search_query("NVDA buyback") == "NVDA buyback"
    assert sanitize_search_query("  NVDA buyback  ") == "NVDA buyback"


def test_sanitize_search_query_empty_or_blank_returns_empty() -> None:
    from agent.intent import sanitize_search_query

    assert sanitize_search_query("") == ""
    assert sanitize_search_query("   ") == ""


def test_sanitize_search_query_rejects_over_length_cap() -> None:
    from agent.intent import _QUERY_MAX_LEN, sanitize_search_query

    too_long = "NVDA buyback " + "x" * _QUERY_MAX_LEN
    assert len(too_long) > _QUERY_MAX_LEN
    assert sanitize_search_query(too_long) == ""


def test_sanitize_search_query_allows_common_finance_acronyms() -> None:
    """Tokens like CEO/SEC that ARE ticker-shaped (2-5 uppercase letters) but
    are not tickers must not trip the hallucinated-entity guard."""
    from agent.intent import sanitize_search_query

    assert sanitize_search_query("NVDA CEO comments on the buyback") != ""
    assert sanitize_search_query("AAPL SEC probe") != ""


def test_sanitize_search_query_rejects_unknown_ticker() -> None:
    """A ticker-shaped token outside shared.tickers.TICKERS (a hallucinated or
    out-of-coverage entity) rejects the whole rewrite -- callers fall back to
    the raw question rather than search on a ticker that isn't ours."""
    from agent.intent import sanitize_search_query

    assert sanitize_search_query("SMCI buyback") == ""
    assert sanitize_search_query("TSM litigation update") == ""


def test_with_source_search_query_empty_on_heuristic_path() -> None:
    """The heuristic short-circuit never runs an LLM, so no rewrite exists."""
    from agent.intent import classify_intent_with_source

    _intent, source, _news, _earn, search_query, *_ = classify_intent_with_source("What's the RSI?")
    assert source == "heuristic"
    assert search_query == ""


def test_with_source_search_query_empty_on_fallback_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An LLM failure biases to thesis AND carries no rewrite."""
    _patch_llm_pipeline(monkeypatch, None, invoke_raises=RuntimeError("timeout"))
    from agent.intent import classify_intent_with_source

    _intent, source, _news, _earn, search_query, *_ = classify_intent_with_source(
        "Tell me about INTC"
    )
    assert source == "fallback"
    assert search_query == ""


def test_with_source_search_query_carries_llm_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    """The LLM path threads a clean rewrite through, sanitized."""
    _patch_llm_pipeline(
        monkeypatch,
        IntentDecision(
            intent="quick_fact",
            needs_news_search=True,
            search_query="NVDA buyback",
        ),
    )
    from agent.intent import classify_intent_with_source

    _intent, source, _news, _earn, search_query, *_ = classify_intent_with_source(
        "what about the buyback?"
    )
    assert source == "llm"
    assert search_query == "NVDA buyback"


def test_with_source_search_query_rejected_hallucinated_ticker_backstops_to_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rewrite naming a ticker outside TICKERS is rejected -- the hallucinated
    ticker never reaches Qdrant. QNT-322 follow-up: instead of an empty query
    (raw-question fallback), the LLM path now backstops to the deterministic
    floor query, since a floor fired on "buyback"."""
    _patch_llm_pipeline(
        monkeypatch,
        IntentDecision(
            intent="quick_fact",
            needs_news_search=True,
            search_query="SMCI buyback",
        ),
    )
    from agent.intent import classify_intent_with_source

    _intent, source, news, _earn, search_query, *_ = classify_intent_with_source(
        "what about the buyback?"
    )
    assert source == "llm"
    assert news is True
    # safety property preserved: the out-of-coverage ticker is gone ...
    assert "SMCI" not in search_query
    # ... replaced by the deterministic floor topic (no ticker in the question,
    # so topic-only; the covered ticker is applied downstream from state).
    assert search_query == "buyback"


def test_llm_path_empty_rewrite_with_floor_backstops_to_floor_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact live-eval miss (meta-layoffs / intc-ceo): the small model
    misclassified/skipped the rewrite (empty search_query) but a keyword floor
    fired. The LLM path now composes the floor query instead of "" -- no path
    emits flag=True with an empty query."""
    _patch_llm_pipeline(
        monkeypatch,
        IntentDecision(
            intent="conversational",
            needs_news_search=False,
            search_query="",
        ),
    )
    from agent.intent import classify_intent_with_source

    _intent, source, news, _earn, search_query, *_ = classify_intent_with_source(
        "any news on the layoffs?"
    )
    assert source == "llm"
    assert news is True  # floor rescued the flag ("layoffs")
    assert search_query == "layoffs"  # ... and now the query, too


def test_llm_path_empty_rewrite_no_floor_stays_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """No floor + empty model rewrite -> still "" (raw-question fallback). The
    backstop only fills when a floor actually fired."""
    _patch_llm_pipeline(
        monkeypatch,
        IntentDecision(intent="thesis", needs_news_search=False, search_query=""),
    )
    from agent.intent import classify_intent_with_source

    _intent, source, news, earn, search_query, *_ = classify_intent_with_source(
        "give me your read on the setup"
    )
    assert source == "llm"
    assert (news, earn) == (False, False)
    assert search_query == ""


def test_classify_prompt_carves_out_search_query_from_the_history_ban() -> None:
    """Regression pin (review finding, QNT-289): the prompt's blanket "do not
    use history to infer a ticker" disclaimer predates search_query and would
    contradict it word-for-word if left unqualified -- the same LLM call sees
    both instructions, and a contradiction here is exactly the kind of thing
    that degrades silently with a model swap. Pin that the carve-out survives
    any future edit to either instruction."""
    from agent.intent import _CLASSIFY_PROMPT

    assert "search_query" in _CLASSIFY_PROMPT
    # The history disclaimer must explicitly exempt search_query from the
    # "don't use history" rule, not just describe search_query elsewhere.
    disclaimer_start = _CLASSIFY_PROMPT.index("Recent conversation")
    disclaimer = _CLASSIFY_PROMPT[disclaimer_start : disclaimer_start + 300]
    assert "search_query" in disclaimer


# ─── QNT-322 (G-10): context-aware hallucinated-entity guard ──────────────────


def test_sanitize_keeps_user_supplied_entity_named_in_the_question() -> None:
    """A ticker-shaped token outside TICKERS (SK) survives when the user named
    the entity in the question -- the QNT-289 rewrite that resolves it from a
    warm thread must not be killed for naming a real competitor."""
    from agent.intent import sanitize_search_query

    assert (
        sanitize_search_query(
            "NVDA SK Hynix partnership", question="what's the deal with SK Hynix?"
        )
        == "NVDA SK Hynix partnership"
    )


def test_sanitize_keeps_user_supplied_entity_named_in_history() -> None:
    """The entity may live only in the classifier's history window (the exact
    warm-thread ellipsis loss case) -- it still survives."""
    from agent.intent import sanitize_search_query

    assert (
        sanitize_search_query(
            "NVDA ASML supply chain",
            history_text="user: how much does NVDA depend on ASML?",
        )
        == "NVDA ASML supply chain"
    )


def test_sanitize_rejects_invented_entity_absent_from_question_and_history() -> None:
    """An invented ticker-shaped token that appears in NEITHER the question nor
    the history is still a hallucination -- reject the whole rewrite."""
    from agent.intent import sanitize_search_query

    assert (
        sanitize_search_query("NVDA SMCI partnership", question="what about the partnership?") == ""
    )


def test_sanitize_without_context_preserves_rejection() -> None:
    """Regression: with no question/history supplied (the default), an
    out-of-coverage entity still rejects -- the QNT-289 contract holds."""
    from agent.intent import sanitize_search_query

    assert sanitize_search_query("NVDA SK Hynix partnership") == ""


# ─── QNT-322 (G-11): deterministic query on the no-LLM classify paths ─────────


def test_heuristic_path_composes_ticker_plus_floor_topic() -> None:
    """A heuristic short-circuit that also trips a keyword floor emits a
    ticker+topic query instead of "" -- Qdrant gets a clean string, not the
    raw question."""
    from agent.intent import classify_intent_with_source

    _intent, source, news, _earn, search_query, *_ = classify_intent_with_source(
        "is NVDA overbought given the buyback?"
    )
    assert source == "heuristic"
    assert news is True
    assert search_query == "NVDA buyback"


def test_heuristic_path_floor_query_without_ticker_uses_topic_only() -> None:
    """A followup ellipsis that names no ticker composes the topic alone; the
    ticker is applied downstream from resolved state."""
    from agent.intent import classify_intent_with_source

    _intent, source, news, _earn, search_query, *_ = classify_intent_with_source(
        "why the buyback?", has_prior_turn=True
    )
    assert source == "heuristic"
    assert news is True
    assert search_query == "buyback"


def test_heuristic_path_empty_query_when_no_floor_fires() -> None:
    """No floor -> "" (AC2: empty only when no floor fired)."""
    from agent.intent import classify_intent_with_source

    _intent, source, news, earn, search_query, *_ = classify_intent_with_source(
        "is NVDA overbought?"
    )
    assert source == "heuristic"
    assert (news, earn) == (False, False)
    assert search_query == ""


def test_floor_search_query_over_length_cap_returns_empty() -> None:
    """A pathological, punctuation-free qualifier run would push the composed
    query past the tool-side cap -- guard it so the caller falls back to the
    raw question instead of degrading to a "[]" search (review advisory)."""
    from agent.intent import _QUERY_MAX_LEN, _floor_search_query

    long_qualifier = "latest news on NVDA with " + "supply " * _QUERY_MAX_LEN
    assert len(long_qualifier) > _QUERY_MAX_LEN
    assert _floor_search_query(long_qualifier) == ""
    # a normal qualifier still composes a ticker+topic query
    assert _floor_search_query("latest news on NVDA with SK Hynix") == "NVDA sk hynix"


def test_fallback_path_composes_floor_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """The LLM-failure fallback path also composes the deterministic query --
    the defect was symmetric across every no-LLM path (QNT-322 title)."""
    _patch_llm_pipeline(monkeypatch, None, invoke_raises=RuntimeError("timeout"))
    from agent.intent import classify_intent_with_source

    _intent, source, news, _earn, search_query, *_ = classify_intent_with_source(
        "what about the buyback?"
    )
    assert source == "fallback"
    assert news is True
    assert search_query == "buyback"
