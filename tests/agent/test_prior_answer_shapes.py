"""QNT-324 (v3 G-5): the classify prior_answer snapshot generalizes beyond Thesis.

QNT-307 scoped the followup ``prior_answer`` channel to a Thesis only, reproducing
the retired ``thesis`` slot's lifetime. This lets a followup point back at ANY
analytical card the prior turn produced (comparison / lean comparison / focused /
exploration), while chit-chat (``ConversationalAnswer``) and a followup's own
compact ``QuickFactAnswer`` are never carried as substrate.

- AC1: ``classify_node`` snapshots each analytical shape across the turn boundary
  (one test per shape); ConversationalAnswer -> None.
- AC2: ``build_followup_prompt`` carries the prior card for each shape under a
  shape-labelled heading.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from agent import graph as graph_module
from agent.comparison import ComparisonAnswer, LeanComparisonAnswer, LeanComparisonRow
from agent.conversational import ConversationalAnswer
from agent.exploration import ExplorationAnswer
from agent.focused import FocusedAnalysis
from agent.graph import AgentState, build_followup_prompt
from agent.nodes.classify import classify_node
from agent.nodes.deps import GraphDeps
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis

from ._thesis_factory import make_comparison_section, make_thesis

# ─────────────────────────── shape factories ─────────────────────────────────


def _thesis() -> Thesis:
    return make_thesis(company_summary="TSLA framing (source: company).")


def _comparison() -> ComparisonAnswer:
    return ComparisonAnswer(
        sections=[
            make_comparison_section("NVDA", "Premium", "Uptrend"),
            make_comparison_section("AAPL", "Inline", "Sideways"),
        ],
        differences="NVDA carries a richer multiple than AAPL (source: fundamental).",
    )


def _lean_comparison() -> LeanComparisonAnswer:
    return LeanComparisonAnswer(
        rows=[
            LeanComparisonRow(ticker="NVDA", pe="65", rsi="78", net_margin="55%", price="$120"),
            LeanComparisonRow(ticker="AAPL", pe="30", rsi="55", net_margin="25%", price="$180"),
        ]
    )


def _focused() -> FocusedAnalysis:
    return FocusedAnalysis(focus="fundamental", summary="Premium multiple (source: fundamental).")


def _exploration() -> ExplorationAnswer:
    return ExplorationAnswer(
        headline="Blackwell demand drives the tape; momentum is stretched (source: news)."
    )


def _conversational() -> ConversationalAnswer:
    return ConversationalAnswer(answer="Glad that read lands with you.", suggestions=[])


def _quick_fact() -> QuickFactAnswer:
    return QuickFactAnswer(
        answer="RSI is 78 (source: technical).", cited_value="78", source="technical"
    )


_ANALYTICAL_CASES = [
    pytest.param(_thesis, Thesis, id="thesis"),
    pytest.param(_comparison, ComparisonAnswer, id="comparison"),
    pytest.param(_lean_comparison, LeanComparisonAnswer, id="lean_comparison"),
    pytest.param(_focused, FocusedAnalysis, id="focused"),
    pytest.param(_exploration, ExplorationAnswer, id="exploration"),
]


def _deps() -> GraphDeps:
    return GraphDeps(
        tools={},
        event_emitter=None,
        compact_company_tool=None,
        comparison_metrics_tool=None,
        active_retrievals=(),
    )


def _run_classify(monkeypatch: pytest.MonkeyPatch, hydrated_answer: object | None) -> object | None:
    """Call classify_node with ``hydrated_answer`` as the prior turn's ``answer``
    (what the checkpointer hands classify at the top of the next turn) and return
    the ``prior_answer`` it snapshots. The classify LLM is stubbed so no network
    call fires -- the snapshot logic is independent of this turn's intent."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda *a, **k: ("thesis", "stub", False, False, "", [], ""),
    )
    state = cast(
        AgentState, {"ticker": "TSLA", "question": "tell me more", "answer": hydrated_answer}
    )
    result = classify_node(state, {}, _deps())
    return result["prior_answer"]


# ─────────────────────────── AC1: snapshot per shape ─────────────────────────


@pytest.mark.parametrize(("factory", "shape"), _ANALYTICAL_CASES)
def test_classify_snapshots_analytical_shape(
    factory: Any, shape: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each analytical card produced by the prior turn is carried into
    ``prior_answer`` across the turn boundary."""
    payload = factory()
    assert isinstance(_run_classify(monkeypatch, payload), shape)


def test_classify_conversational_answer_with_no_prior_card_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prior ConversationalAnswer is not itself substrate; with nothing behind
    it (cold thread / bare greeting) the snapshot is None."""
    assert _run_classify(monkeypatch, _conversational()) is None


def test_classify_carries_prior_card_across_conversational_interlude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-349 follow-up: a non-analytical interlude (conversational / clarify,
    ``answer=ConversationalAnswer``) must carry the earlier analytical card
    forward -- otherwise a thesis -> "hi" -> "tell me more" chain loses the card
    the followup should narrate over, even though R-1 preserves its reports."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda *a, **k: ("thesis", "stub", False, False, "", [], ""),
    )
    prior = _comparison()
    state = cast(
        AgentState,
        {
            "ticker": "TSLA",
            "question": "tell me more",
            "answer": _conversational(),
            "prior_answer": prior,
        },
    )
    result = classify_node(state, {}, _deps())
    assert result["prior_answer"] is prior


def test_classify_drops_quick_fact_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A followup's own compact QuickFactAnswer is not a full analytical card, so
    it is not carried (unchanged from QNT-307's non-thesis behaviour)."""
    assert _run_classify(monkeypatch, _quick_fact()) is None


def test_classify_preserves_prior_answer_on_narrative_only_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A narrative-only followup writes ``answer=None``; classify must preserve
    the earlier card carried in ``prior_answer`` so it survives a chain."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda *a, **k: ("thesis", "stub", False, False, "", [], ""),
    )
    prior = _comparison()
    state = cast(
        AgentState,
        {"ticker": "TSLA", "question": "why?", "answer": None, "prior_answer": prior},
    )
    result = classify_node(state, {}, _deps())
    assert result["prior_answer"] is prior


# ─────────────────────────── AC2: prompt per shape ───────────────────────────


@pytest.mark.parametrize(
    ("factory", "label"),
    [
        pytest.param(_thesis, "thesis", id="thesis"),
        pytest.param(_comparison, "comparison", id="comparison"),
        pytest.param(_lean_comparison, "comparison", id="lean_comparison"),
        pytest.param(_focused, "focused analysis", id="focused"),
        pytest.param(_exploration, "exploration scan", id="exploration"),
    ],
)
def test_followup_prompt_carries_prior_card(factory: Any, label: str) -> None:
    """The followup prompt renders the prior card via ``to_markdown`` under a
    heading that names the card's shape."""
    payload = factory()
    messages = build_followup_prompt("TSLA", "which looks stronger?", {}, payload)
    user_msg = messages[-1].content
    assert isinstance(user_msg, str)
    # Shape-labelled heading.
    assert f"your earlier {label} on this ticker" in user_msg
    # The card body itself is present (a distinctive fragment of each to_markdown).
    body = payload.to_markdown()
    assert body.splitlines()[0].strip("# ").split("(")[0].strip()[:20] in user_msg


def test_followup_prompt_omits_prior_section_when_none() -> None:
    """No prior card -> no prior-turn heading at all."""
    messages = build_followup_prompt("TSLA", "why?", {"technical": "RSI 78"}, None)
    user_msg = messages[-1].content
    assert isinstance(user_msg, str)
    assert "your earlier" not in user_msg


# ── Grounding substrate: the prior card grounds a followup (QNT-324) ──────────


def test_runtime_report_texts_folds_prior_card_on_followup() -> None:
    """On a followup, the prior card is a grounding source so faithfully
    re-quoting its figures is not flagged as unsupported. This matters once the
    card is a non-thesis shape: a comparison's second-ticker reports_by_ticker is
    cleared by the turn-boundary reset, so only the card carries those numbers."""
    prior = _comparison()
    state = cast(
        AgentState,
        {"intent": "followup", "reports": {"technical": "RSI 78"}, "prior_answer": prior},
    )
    texts = graph_module._runtime_report_texts(state)
    assert "RSI 78" in texts  # surviving reports still present
    assert any("richer multiple than AAPL" in t for t in texts)  # prior card folded in


def test_runtime_report_texts_omits_prior_card_off_followup() -> None:
    """A non-followup turn does not fold ``prior_answer`` into the grounding
    substrate -- classify can carry a hydrated card there without it silently
    grounding an unrelated turn's numbers."""
    state = cast(
        AgentState,
        {"intent": "thesis", "reports": {"technical": "RSI 78"}, "prior_answer": _comparison()},
    )
    texts = graph_module._runtime_report_texts(state)
    assert texts == ["RSI 78"]


def test_followup_fold_still_flags_invented_numbers() -> None:
    """Folding the prior card is not a blanket grounding bypass: a followup that
    invents a number absent from BOTH the card and the surviving reports is still
    flagged, while a figure carried by the reports/card is supported."""
    state = cast(
        AgentState,
        {
            "intent": "followup",
            "reports": {"technical": "RSI-14 at 78"},
            "prior_answer": _comparison(),
        },
    )
    substrate = graph_module._runtime_report_texts(state)
    result, _rate = graph_module._runtime_grounding_check(
        "momentum reads 78 but the target is 99999", substrate
    )
    assert "99999" in result.unsupported  # invented number caught
    assert "78" not in result.unsupported  # grounded number supported
