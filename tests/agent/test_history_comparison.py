"""QNT-325 (v3 G-12): history-aware comparison ticker resolution.

Comparison resolution used to be history-blind: a warm thread that just discussed
NVDA and AMD, asked "now compare those two", got a clarify card for a question the
transcript answers verbatim. Now the resolver fills a short pair from the recent
transcript (most-recent distinct tickers), and the ambiguity gate defers exactly
when that resolved pair exists -- so gate and plan can never disagree.

- AC1: ``_resolve_comparison_tickers`` fills from history only when the question
  names fewer than two tickers; ordering (most recent wins) and dedupe against
  question-named tickers.
- AC2: ``_detect_ambiguity`` defers when the history-resolved pair exists; a cold
  "compare them" and a warm one-ticker thread still clarify.
- AC3: dialogue fixtures through the real ``classify_node`` -- a warm two-ticker
  thread plus a gesture compare produces the right pair; a warm one-ticker thread
  still clarifies.
"""

from __future__ import annotations

from typing import cast

import pytest
from agent import graph as graph_module
from agent.graph import AgentState, _detect_ambiguity, _resolve_comparison_tickers
from agent.nodes.classify import classify_node
from agent.nodes.deps import GraphDeps
from agent.nodes.plan import plan_node
from agent.prompts import ConversationMessage
from agent.support import _history_tickers


def _msg(role: str, content: str) -> ConversationMessage:
    return cast(ConversationMessage, {"role": role, "content": content})


# A warm thread that discussed NVDA first, then AMD (AMD is the most recent).
_NVDA_THEN_AMD: list[ConversationMessage] = [
    _msg("user", "give me a thesis on NVDA"),
    _msg("assistant", "NVDA looks stretched but the demand story holds (source: company)."),
    _msg("user", "now AMD"),
    _msg("assistant", "AMD trades at a discount to NVDA (source: fundamental)."),
]


# ─────────────────────────── AC1: resolver ───────────────────────────────────


def test_resolver_fills_pair_from_history_gesture() -> None:
    """A bare gesture (no named ticker) fills both sides from the transcript."""
    resolved = _resolve_comparison_tickers("", "now compare those two", _NVDA_THEN_AMD)
    assert set(resolved) == {"NVDA", "AMD"}


def test_resolver_history_most_recent_wins() -> None:
    """Most-recently-discussed ticker leads the history fill order."""
    resolved = _resolve_comparison_tickers("", "compare those two", _NVDA_THEN_AMD)
    # AMD was the most recent turn, so it precedes NVDA.
    assert resolved == ["AMD", "NVDA"]


def test_resolver_dedupes_history_against_named() -> None:
    """A ticker named in the question is not doubled by the history fill."""
    resolved = _resolve_comparison_tickers("", "how does NVDA compare?", _NVDA_THEN_AMD)
    assert resolved == ["NVDA", "AMD"]


def test_resolver_ignores_history_when_two_named() -> None:
    """History is only consulted to reach the pair -- two named tickers stand alone."""
    resolved = _resolve_comparison_tickers("", "compare NVDA and AAPL", _NVDA_THEN_AMD)
    assert resolved == ["NVDA", "AAPL"]


def test_resolver_url_context_still_fills_single_named() -> None:
    """QNT-233 preserved: a single-named compare from /ticker/NVDA uses the URL
    context for side two, ahead of any history."""
    resolved = _resolve_comparison_tickers("NVDA", "compare to AAPL", _NVDA_THEN_AMD)
    assert resolved == ["AAPL", "NVDA"]


def test_resolver_no_history_no_context_stays_short() -> None:
    """Nothing to fill from -> a single ticker, which the gate will clarify."""
    assert _resolve_comparison_tickers("", "compare them", None) == []


def test_history_tickers_newest_first_distinct() -> None:
    """The history extractor returns distinct tickers, most-recent turn first."""
    assert _history_tickers(_NVDA_THEN_AMD) == ["AMD", "NVDA"]
    assert _history_tickers(None) == []
    assert _history_tickers([_msg("user", "hello there")]) == []


def test_history_tickers_ignores_assistant_only_ticker() -> None:
    """A ticker only the ASSISTANT named (valuation colour) is not a fill source --
    a false pairing is worse than a clarify."""
    thread = [
        _msg("user", "give me a thesis on AMD"),
        _msg("assistant", "AMD trades at a discount to NVDA (source: fundamental)."),
    ]
    # NVDA appears only in the assistant turn, so it must not be surfaced.
    assert _history_tickers(thread) == ["AMD"]


# ─────────────────────────── AC2: ambiguity gate ─────────────────────────────


def test_gate_defers_when_history_resolves_pair() -> None:
    """The gate defers (no clarify) once the transcript supplies the second side."""
    assert (
        _detect_ambiguity(
            "comparison",
            "now compare those two",
            has_prior_turn=True,
            has_context_ticker=True,
            context_ticker="NVDA",
            history=_NVDA_THEN_AMD,
        )
        is None
    )


def test_gate_clarifies_cold_compare_them() -> None:
    """AC2: a cold 'compare them' with no history still routes to clarify."""
    assert (
        _detect_ambiguity("comparison", "compare them", has_prior_turn=False)
        == "needs_second_ticker"
    )


def test_gate_clarifies_warm_one_ticker_thread() -> None:
    """A warm thread that only ever named ONE ticker cannot form a pair -> clarify."""
    one_ticker = [
        _msg("user", "give me a thesis on NVDA"),
        _msg("assistant", "NVDA looks stretched (source: company)."),
    ]
    assert (
        _detect_ambiguity(
            "comparison",
            "compare those two",
            has_prior_turn=True,
            has_context_ticker=True,
            context_ticker="NVDA",
            history=one_ticker,
        )
        == "needs_second_ticker"
    )


# ─────────────────────── AC3: classify_node -> plan_node fixtures ─────────────


def _deps(*, with_tools: bool = False) -> GraphDeps:
    tools = {name: (lambda _t: "") for name in graph_module.REPORT_TOOLS} if with_tools else {}
    return GraphDeps(
        tools=cast(dict, tools),
        event_emitter=None,
        compact_company_tool=None,
        comparison_metrics_tool=None,
        active_retrievals=(),
    )


def _run_classify_comparison(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ticker: str,
    question: str,
    messages: list[ConversationMessage],
) -> dict[str, object]:
    """Drive the real classify_node with the classify LLM stubbed to 'comparison'."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda *a, **k: ("comparison", "stub", False, False, ""),
    )
    state = cast(AgentState, {"ticker": ticker, "question": question, "messages": messages})
    return classify_node(state, {}, _deps())


def _run_plan_after_classify(
    initial: dict[str, object], classify_result: dict[str, object]
) -> list[str]:
    """Merge classify's return into state (as the graph does) and run the REAL
    plan_node, returning the comparison_tickers plan settled on."""
    state = cast(AgentState, {**initial, **classify_result})
    result = plan_node(state, {}, _deps(with_tools=True))
    return cast(list[str], result["comparison_tickers"])


def test_classify_then_plan_warm_two_ticker_gesture(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: warm NVDA+AMD thread + 'compare those two' -> gate defers, routes to plan,
    and the REAL plan_node settles on the NVDA/AMD pair (gate and plan agree)."""
    initial = {
        "ticker": "NVDA",
        "question": "now compare those two",
        "messages": list(_NVDA_THEN_AMD),
    }
    result = _run_classify_comparison(
        monkeypatch, ticker="NVDA", question="now compare those two", messages=list(_NVDA_THEN_AMD)
    )
    assert result["ambiguity_kind"] is None
    assert result["route"] == "plan"
    # plan_node consumes the pair classify forwarded -- exercise it for real.
    assert set(_run_plan_after_classify(initial, result)) == {"NVDA", "AMD"}


def test_classify_warm_one_ticker_gesture_clarifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: warm NVDA-only thread + 'compare those two' -> still clarifies."""
    result = _run_classify_comparison(
        monkeypatch,
        ticker="NVDA",
        question="compare those two",
        messages=[
            _msg("user", "give me a thesis on NVDA"),
            _msg("assistant", "NVDA looks stretched (source: company)."),
        ],
    )
    assert result["ambiguity_kind"] == "needs_second_ticker"
    assert result["route"] == "clarify"


def test_gate_and_plan_agree_at_history_cap_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: at the HISTORY_TURN_LIMIT boundary, classify appends the current
    turn and re-trims -- dropping the OLDEST turn from state['messages']. If plan_node
    re-derived history from that window-shifted list it would miss a ticker the gate
    saw and redirect on <2. classify now forwards the resolved pair, so plan agrees.

    The oldest USER turn names NVDA and only a recent USER turn names AMD; the thread
    is padded to the 20-message (10-turn) cap so the append shifts NVDA out."""
    filler = [
        _msg("user" if i % 2 == 0 else "assistant", f"some neutral remark number {i}")
        for i in range(16)
    ]
    messages = [
        _msg("user", "give me a thesis on NVDA"),  # oldest -- shifts out on append
        _msg("assistant", "NVDA read (source: company)."),
        *filler,  # 16 tickerless turns
        _msg("user", "now AMD"),
        _msg("assistant", "AMD read (source: company)."),
    ]
    assert len(messages) == 20  # exactly at the cap

    initial = {"ticker": "NVDA", "question": "compare those two", "messages": list(messages)}
    result = _run_classify_comparison(
        monkeypatch, ticker="NVDA", question="compare those two", messages=list(messages)
    )
    # Gate saw the full pre-append window (NVDA + AMD) -> defers.
    assert result["ambiguity_kind"] is None
    assert result["route"] == "plan"
    # The append+re-trim shifted the NVDA USER turn out of the persisted transcript,
    # so a plan-side re-derive would see only AMD (the hazard the forwarding fixes).
    persisted = cast(list[ConversationMessage], result["messages"])
    shifted_history = graph_module._history_before_current(persisted, "compare those two")
    assert _history_tickers(shifted_history) == ["AMD"]
    # ...yet plan still settles on the full pair, because classify forwarded it.
    assert set(_run_plan_after_classify(initial, result)) == {"NVDA", "AMD"}
