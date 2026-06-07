"""Tests for the exploration-scan card (QNT-220 follow-up).

Covers the ``ExplorationAnswer`` schema render, the classifier-safety
invariant (the LLM classifier can never emit "exploration"), and the
synthesize branch's deterministic fallback when no reports were gathered.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from agent import graph as graph_module
from agent.exploration import ExplorationAnswer, ExplorationValue
from agent.graph import build_graph
from agent.intent import IntentDecision
from langchain_core.messages import AIMessage


def test_exploration_to_markdown_renders_all_sections() -> None:
    card = ExplorationAnswer(
        headline="Blackwell demand drives the tape; momentum is stretched (source: news).",
        observations=[
            "New order headlines this week (source: news).",
            "RSI-14 daily 71, overbought (source: technical).",
        ],
        cited_values=[ExplorationValue(label="RSI", value="71", source="technical")],
    )
    md = card.to_markdown()
    assert "Blackwell demand drives the tape" in md
    assert "- New order headlines this week (source: news)." in md
    assert "- **RSI:** 71 (source: technical)" in md
    # ADR-003 disclaimer is always appended (CLI/eval parity).
    assert "not investment advice" in md


def test_exploration_to_markdown_minimal() -> None:
    """Empty observations / cited_values still renders headline + disclaimer."""
    md = ExplorationAnswer(headline="Nothing notable stands out.").to_markdown()
    assert md.startswith("Nothing notable stands out.")
    assert "not investment advice" in md


def test_classifier_schema_cannot_emit_exploration() -> None:
    """Constraint: "exploration" is internal-only — set by explore_supervisor,
    never picked by the classifier LLM. The IntentDecision JSON Schema enum
    must therefore exclude it, or the model could bypass routing."""
    enum = IntentDecision.model_json_schema()["properties"]["intent"]["enum"]
    assert "exploration" not in enum
    assert "thesis" in enum  # the rest of the classifier vocabulary is intact


class _FallbackLLM:
    """Synthesize stub that returns None for any bound schema, forcing the
    deterministic conversational fallback."""

    def with_structured_output(self, _schema: type) -> _FallbackLLM:
        return self

    def with_retry(self, *_args: Any, **_kwargs: Any) -> _FallbackLLM:
        return self

    def invoke(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        return iter([AIMessage(content="")])


class _CapturingLLM:
    """Produces a real ExplorationAnswer for synthesize and captures the prompt
    narrate streams, so a test can assert the exploration card is fed to the
    narrator as substrate (the narrate-substrate selection must include it)."""

    def __init__(self) -> None:
        self.card = ExplorationAnswer(
            headline="EXPLORATION_HEADLINE_MARKER stands out (source: news).",
            observations=["Fresh headlines (source: news)."],
            cited_values=[],
        )
        self.narrate_prompt: list[Any] = []

    def with_structured_output(self, _schema: type) -> _CapturingLLM:
        return self

    def with_retry(self, *_args: Any, **_kwargs: Any) -> _CapturingLLM:
        return self

    def invoke(self, *_args: Any, **_kwargs: Any) -> Any:
        return self.card

    def stream(self, prompt: list[Any], *_args: Any, **_kwargs: Any) -> Any:
        self.narrate_prompt = prompt
        return iter([AIMessage(content="Scan narrative.")])


def test_exploration_card_feeds_narrate_substrate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The narrate node must summarise the exploration card. Regression guard
    for the substrate-selection chain omitting ``state['exploration']``."""
    llm = _CapturingLLM()
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda *_a, **_k: ("thesis", "heuristic"),
    )
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_k: llm)
    tools = {
        "news": MagicMock(return_value="## news\nHeadlines\n"),
        "technical": MagicMock(return_value="## technical\nChart\n"),
    }

    result = build_graph(tools).invoke(
        {"ticker": "AAPL", "question": "What's interesting about AAPL this week?"}
    )

    assert result["intent"] == "exploration"
    rendered = " ".join(str(getattr(m, "content", m)) for m in llm.narrate_prompt)
    assert "EXPLORATION_HEADLINE_MARKER" in rendered


def test_exploration_with_no_reports_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """If every tool errors, the exploration branch degrades to the in-domain
    conversational redirect rather than rendering a blank card."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda *_a, **_k: ("thesis", "heuristic"),
    )
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_k: _FallbackLLM())
    # Both lenses raise so _gather_reports returns empty -> synthesize fallback.
    tools = {
        "news": MagicMock(side_effect=RuntimeError("news down")),
        "technical": MagicMock(side_effect=RuntimeError("tech down")),
    }

    result = build_graph(tools).invoke(
        {"ticker": "AAPL", "question": "What's interesting about AAPL this week?"}
    )

    assert result["intent"] == "exploration"
    assert result["exploration"] is None
    assert result["conversational"] is not None
