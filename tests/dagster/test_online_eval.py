"""Tests for the online-eval trace renderer (QNT-307).

``_extract_generated`` reconstructs the user-facing answer markdown from a
serialized Langfuse trace's ``answer`` field. QNT-307 collapsed the seven legacy
per-shape slot keys into one ``answer`` key with no discriminator, so the renderer
must disambiguate the overlapping shapes by exact field set -- a QuickFactAnswer
dict otherwise validates as ConversationalAnswer (extras ignored) and loses its
cited value.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from agent.conversational import ConversationalAnswer
from agent.quick_fact import QuickFactAnswer
from agent.thesis import AspectView, Thesis
from dagster import build_op_context
from dagster_pipelines import online_eval
from dagster_pipelines.online_eval import (
    _extract_generated,
    _extract_question,
    _fetch_agent_chat_runs,
    _parse_io,
)


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


# ---------------------------------------------------------------------------
# QNT-360: Langfuse v2 observations fetch. v2 returns input/output as raw JSON
# strings (not dicts), and paginates by cursor instead of page numbers.
# ---------------------------------------------------------------------------


def test_parse_io_decodes_v2_json_string() -> None:
    """v2 serializes I/O as a JSON string; without decoding, _extract_question
    would take the whole JSON blob as the question."""
    raw_in = json.dumps({"ticker": "NVDA", "question": "Give me a thesis on NVDA"})
    assert _extract_question(_parse_io(raw_in)) == "Give me a thesis on NVDA"

    conv = ConversationalAnswer(answer="I cover US equities.", suggestions=["a?", "b?"])
    raw_out = json.dumps({"answer": conv.model_dump()})
    assert "I cover US equities." in _extract_generated(_parse_io(raw_out))


def test_parse_io_passes_through_non_json() -> None:
    assert _parse_io(None) is None
    assert _parse_io({"question": "q"}) == {"question": "q"}
    assert _parse_io("plain question") == "plain question"


def test_fetch_agent_chat_runs_follows_cursor_and_filters() -> None:
    """Pages until meta.cursor is empty, and scopes to the langgraph-run span of
    agent-chat traces (the root span name differs between code paths, the
    langgraph-run span carries the graph state on every trace)."""
    pages = {
        None: SimpleNamespace(data=["a", "b"], meta=SimpleNamespace(cursor="c1")),
        "c1": SimpleNamespace(data=["c"], meta=SimpleNamespace(cursor=None)),
    }
    calls: list[dict] = []

    def get_many(**kwargs):
        calls.append(kwargs)
        return pages[kwargs.get("cursor")]

    client = SimpleNamespace(api=SimpleNamespace(observations=SimpleNamespace(get_many=get_many)))
    now = datetime(2026, 10, 5, tzinfo=UTC)
    runs = _fetch_agent_chat_runs(client, now - timedelta(days=7), now)

    assert runs == ["a", "b", "c"]
    assert [c.get("cursor") for c in calls] == [None, "c1"]
    assert "io" in calls[0]["fields"]
    filters = {(f["column"], f["operator"]): f.get("value") for f in json.loads(calls[0]["filter"])}
    assert filters[("traceName", "=")] == "agent-chat"
    assert filters[("name", "=")] == "langgraph-run"
    assert ("startTime", ">=") in filters and ("startTime", "<") in filters


def test_fetch_failure_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """QNT-360: the op used to log and return on a fetch error, so every weekly run
    since launch reported SUCCESS while scoring nothing. A failed fetch must fail
    the run so the breakage is visible in Dagster."""

    def get_many(**_kwargs):
        raise RuntimeError("langfuse 400")

    client = SimpleNamespace(api=SimpleNamespace(observations=SimpleNamespace(get_many=get_many)))
    monkeypatch.setattr(online_eval, "_build_langfuse_client", lambda: client)

    with pytest.raises(RuntimeError, match="langfuse 400"):
        online_eval.run_online_eval(build_op_context())


def test_scores_only_thesis_traces_on_reference_free_axes(monkeypatch: pytest.MonkeyPatch) -> None:
    """QNT-360 follow-up: the judge scores faithfulness/correctness against a
    REFERENCE thesis and structure against the thesis aspect blocks, but prod
    traces carry no reference and span every intent. The first prod run scored a
    well-grounded fundamental answer faithfulness 0 / structure 0. Score only
    thesis-intent traces, and push only the axes that hold without a reference."""
    from agent.evals import judge

    def run(trace_id: str, intent: str) -> SimpleNamespace:
        return SimpleNamespace(
            trace_id=trace_id,
            input=json.dumps({"question": f"q {trace_id}"}),
            output=json.dumps({"intent": intent, "answer": _thesis().model_dump()}),
        )

    runs = [run("t-thesis", "thesis"), run("t-conv", "conversational")]
    scores: list[tuple[str, str]] = []
    client = SimpleNamespace(
        api=SimpleNamespace(
            observations=SimpleNamespace(
                get_many=lambda **_k: SimpleNamespace(data=runs, meta=SimpleNamespace(cursor=None))
            )
        ),
        create_score=lambda trace_id, name, **_k: scores.append((trace_id, name)),
        flush=lambda: None,
    )
    monkeypatch.setattr(online_eval, "_build_langfuse_client", lambda: client)
    monkeypatch.setattr(online_eval.settings, "ONLINE_EVAL_SAMPLE_RATE", 1.0)
    monkeypatch.setattr(
        judge,
        "score",
        lambda **_k: judge.JudgeScore(faithfulness=1, structure=9, correctness=1, analyst_logic=8),
    )

    online_eval.run_online_eval(build_op_context())

    assert scores == [("t-thesis", "structure"), ("t-thesis", "analyst_logic")]
