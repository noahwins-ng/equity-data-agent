"""Tests for agent.quick_fact (QNT-149).

Covers the structured ``QuickFactAnswer`` schema + ``to_markdown``
re-render. The structured-output flow itself is exercised end-to-end in
``tests/agent/test_graph.py``; this module focuses on the schema's own
contract.
"""

from __future__ import annotations

import pytest
from agent.quick_fact import QuickFactAnswer
from pydantic import ValidationError


def test_minimal_answer_with_value_renders_value_line() -> None:
    qf = QuickFactAnswer(
        answer="RSI sits at 62 (source: technical).",
        cited_value="62",
        source="technical",
    )
    md = qf.to_markdown()
    assert "RSI sits at 62" in md
    # The "Value:" suffix is the contract the chat panel and the eval
    # hallucination scorer both read.
    assert "**Value:** 62 (source: technical)" in md


def test_answer_without_value_omits_value_line() -> None:
    """When the reports don't cover the question, the answer is a 'not
    available' apology and cited_value is empty — render must not invent
    a Value line in that case."""
    qf = QuickFactAnswer(
        answer="P/E ratio not available in the supplied reports.",
        cited_value="",
        source=None,
    )
    md = qf.to_markdown()
    assert "not available" in md
    assert "**Value:**" not in md


def test_invalid_source_rejected_at_construction() -> None:
    """``source`` is a closed Literal so the chat panel and the eval
    hallucination scorer can rely on the canonical names."""
    with pytest.raises(ValidationError):
        QuickFactAnswer(
            answer="RSI is 62.",
            cited_value="62",
            source="bogus",  # type: ignore[arg-type]
        )


def test_to_markdown_safe_when_answer_is_blank() -> None:
    """An empty answer (rare provider-side failure) must still render
    deterministically rather than crash. The CLI relies on this for the
    ``rendered or fallback`` branch in ``__main__``."""
    qf = QuickFactAnswer(answer="", cited_value="", source=None)
    md = qf.to_markdown()
    assert md.strip() != ""
