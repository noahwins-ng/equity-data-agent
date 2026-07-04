"""Tests for the online-eval trace renderer (QNT-307).

``_extract_generated`` reconstructs the user-facing answer markdown from a
serialized Langfuse trace's ``answer`` field. QNT-307 collapsed the seven legacy
per-shape slot keys into one ``answer`` key with no discriminator, so the renderer
must disambiguate the overlapping shapes by exact field set -- a QuickFactAnswer
dict otherwise validates as ConversationalAnswer (extras ignored) and loses its
cited value.
"""

from __future__ import annotations

from agent.conversational import ConversationalAnswer
from agent.quick_fact import QuickFactAnswer
from agent.thesis import AspectView, Thesis
from dagster_pipelines.online_eval import _extract_generated


def _thesis() -> Thesis:
    aspect = AspectView(label=None, summary="s (source: technical).", supports=[], challenges=[])
    return Thesis(
        company=aspect,
        fundamental=aspect,
        technical=aspect,
        news=aspect,
        verdict="Overweight",
        verdict_rationale="Balanced.",
    )


def test_quick_fact_answer_renders_as_quick_fact_not_conversational() -> None:
    """Regression: a quick_fact answer dict shares ``answer`` with
    ConversationalAnswer (which ignores the extra ``cited_value``/``source``), so a
    naive try-each-in-order rendered it as conversational and dropped the cited
    value. Exact field-set matching must render it as the QuickFactAnswer it is."""
    qf = QuickFactAnswer(answer="RSI is 62.", cited_value="62", source="technical")
    rendered = _extract_generated({"answer": qf.model_dump()})
    # The QuickFactAnswer markdown carries the cited value; the conversational
    # render would not.
    assert "**Value:** 62" in rendered
    assert "RSI is 62." in rendered


def test_conversational_answer_still_renders() -> None:
    conv = ConversationalAnswer(answer="I cover US equities.", suggestions=["a?", "b?"])
    rendered = _extract_generated({"answer": conv.model_dump()})
    assert "I cover US equities." in rendered


def test_thesis_answer_renders() -> None:
    assert "Overweight" in _extract_generated({"answer": _thesis().model_dump()})


def test_old_shape_trace_without_answer_key_falls_back() -> None:
    """A PRE-QNT-307 trace carries legacy slot keys and no ``answer`` -- the
    renderer degrades to the string fallback rather than raising."""
    old_shape = {"thesis": _thesis().model_dump(), "narrative": "x"}
    # No ``answer`` key -> not renderable via the union path -> str fallback.
    assert _extract_generated(old_shape) == str(old_shape).strip()


def test_empty_trace_output_is_empty_string() -> None:
    assert _extract_generated(None) == ""
    assert _extract_generated({}) == ""
