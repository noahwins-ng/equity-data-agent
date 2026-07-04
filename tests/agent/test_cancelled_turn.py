"""Cancelled-turn checkpoint semantics (QNT-300, item B-5).

The chat panel aborts the in-flight SSE run when the user sends a new
message or the component unmounts (frontend AbortController), and the SSE
generator cancels the runner task on client disconnect
(``agent_chat._stream`` GeneratorExit -> ``runner_task.cancel()`` with a
shielded teardown). But the graph runs in a worker thread
(``asyncio.to_thread``), and cancelling the asyncio task does NOT kill the
thread: the graph keeps running in the background and its checkpointer
writes (reports, transcript, thesis) still land.

This test reproduces that exact mechanism against a real SqliteSaver-backed
graph and pins down the observed post-cancel semantics.

OBSERVED SEMANTICS (this is the deliberate decision for B-5)
-------------------------------------------------------------
A turn whose SSE consumer was cancelled mid-run but whose worker thread ran
to completion commits into the checkpointer transcript IDENTICALLY to a
delivered turn -- there is no marker distinguishing "the user saw this" from
"the user never saw this". A subsequent follow-up on the same ``thread_id``
therefore hydrates, and can anchor on, an assistant turn the user never saw
(``test_cancelled_turn_commits_transcript_the_user_never_saw`` +
``test_followup_after_cancel_anchors_on_never_seen_turn``).

DECISION: accept and document (do NOT mark the turn).
Rationale:
  * The worker thread has no cancellation signal -- the abort happens on the
    asyncio side after the thread has already been handed the work -- so a
    graph-side "mark this turn cancelled" is not observable at write time.
  * "Gate transcript append on the turn having been delivered" would require
    plumbing an SSE-delivery signal back into the checkpoint write across the
    thread boundary -- exactly the turn-delivery infrastructure QNT-300's
    scope says NOT to build.
  * The divergence is bounded and self-healing: the transcript stores only
    compact surface text, and if a follow-up references the never-seen turn
    the follow-up's own answer surfaces that content to the user. It is a
    mild coherence quirk, not a correctness or safety defect.
If this ever needs to change, the cheap lever is API-side: on the disconnect
teardown, delete the just-written checkpoint for the aborted turn -- but that
is a race against the worker's own commit and is deferred until a real user
report justifies it.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest
from agent import graph as graph_module
from agent.graph import build_graph
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver

from ._thesis_factory import make_thesis


class _StubLLM:
    """Structured-output stub: Thesis -> stub thesis, QuickFact -> stub fact.

    Mirrors the shape ``tests/agent/test_followup.py`` uses so the real graph
    runs classify -> plan -> gather -> synthesize -> narrate deterministically
    without touching LiteLLM.
    """

    def __init__(self) -> None:
        self.invoke = MagicMock(return_value=AIMessage(content="company, technical, fundamental"))
        thesis = make_thesis(verdict="Overweight")
        quick_fact = QuickFactAnswer(
            answer="Momentum is constructive (source: technical).",
            cited_value="62",
            source="technical",
        )

        def make_structured(schema: type) -> MagicMock:
            m = MagicMock()
            if schema is Thesis:
                m.invoke = MagicMock(return_value=thesis)
            elif schema is QuickFactAnswer:
                m.invoke = MagicMock(return_value=quick_fact)
            else:
                m.invoke = MagicMock(return_value=None)
            m.with_retry.return_value = m
            return m

        self._make_structured = make_structured

    def with_structured_output(self, schema: type, **_kwargs: object) -> MagicMock:
        return self._make_structured(schema)

    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        return iter([AIMessage(content="On balance "), AIMessage(content="the read is firm.")])


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch) -> _StubLLM:
    stub = _StubLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)
    return stub


@pytest.fixture
def saver() -> Any:
    """File-independent in-memory SqliteSaver shared across the worker thread.

    ``check_same_thread=False`` because the graph runs in ``asyncio.to_thread``
    (a different thread than the one that reads ``get_state`` afterwards),
    exactly as the API endpoint compiles it.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    return SqliteSaver(conn)


def _assistant_turns(state_values: dict[str, Any]) -> list[dict[str, str]]:
    messages = state_values.get("messages") or []
    return [m for m in messages if m.get("role") == "assistant"]


async def test_cancelled_turn_commits_transcript_the_user_never_saw(
    stub_llm: _StubLLM,  # noqa: ARG001
    saver: Any,
) -> None:
    """The core B-5 invariant: cancelling the asyncio runner task mid-run does
    NOT prevent the worker thread from finishing and committing the turn.

    Reproduces the endpoint's exact worker-thread mechanism
    (``asyncio.create_task(asyncio.to_thread(_runner))`` -> ``.cancel()`` ->
    shielded await) with one gated tool so the cancel lands while the graph is
    still gathering. After release, the thread runs to completion and the
    checkpointer holds the assistant transcript entry the user never saw.
    """
    tool_entered = threading.Event()
    release_tool = threading.Event()
    worker_finished = threading.Event()

    def _gated_company(_ticker: str) -> str:
        tool_entered.set()
        # Block so the asyncio-side cancel lands while gather is in flight.
        release_tool.wait(timeout=5)
        return "## company\nProfile\n"

    tools = {
        "company": _gated_company,
        "technical": MagicMock(return_value="## technical\nRSI 62\n"),
        "fundamental": MagicMock(return_value="## fundamental\nP/E 40\n"),
    }
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "cancel:NVDA"}}

    def _runner() -> None:
        try:
            graph.invoke({"ticker": "NVDA", "question": "is NVDA overvalued?"}, config=config)
        finally:
            worker_finished.set()

    runner_task = asyncio.create_task(asyncio.to_thread(_runner))

    # Wait until the graph is mid-gather (blocked in the gated tool).
    for _ in range(250):
        if tool_entered.is_set():
            break
        await asyncio.sleep(0.02)
    assert tool_entered.is_set(), "graph never reached the gated tool"

    # SSE consumer disconnects: the endpoint's finally cancels the runner task
    # and shields its teardown. The worker THREAD keeps running.
    runner_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.shield(runner_task)

    # Let the orphaned worker thread finish the graph (the "runs in the
    # background and writes its state" case B-5 is about).
    release_tool.set()
    for _ in range(250):
        if worker_finished.is_set():
            break
        await asyncio.sleep(0.02)
    assert worker_finished.is_set(), "worker thread did not finish after release"

    # The cancelled-but-completed turn committed its transcript. The user never
    # saw this assistant turn, yet it is indistinguishable from a delivered one.
    committed = graph.get_state(config).values
    assistant = _assistant_turns(committed)
    assert assistant, "cancelled turn did not commit an assistant transcript entry"
    assert "thesis" in assistant[-1]["content"].lower()


async def test_followup_after_cancel_anchors_on_never_seen_turn(
    stub_llm: _StubLLM,  # noqa: ARG001
    saver: Any,
) -> None:
    """A follow-up on the same thread hydrates the cancelled turn and routes as
    ``followup`` -- documenting that the never-seen turn is a live anchor.

    This is the user-visible consequence of the accepted semantics above: the
    second message reuses the prior (never-delivered) turn's checkpointer state
    rather than starting cold.
    """
    tools = {
        "company": MagicMock(return_value="## company\nProfile\n"),
        "technical": MagicMock(return_value="## technical\nRSI 62\n"),
        "fundamental": MagicMock(return_value="## fundamental\nP/E 40\n"),
    }
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "cancel-followup:NVDA"}}

    # Turn 1 runs to completion in the background (the cancelled turn's worker
    # thread finishing) -- a synchronous invoke is the committed end state a
    # cancelled-but-completed run leaves behind.
    graph.invoke({"ticker": "NVDA", "question": "is NVDA overvalued?"}, config=config)
    before_followup = graph.get_state(config).values
    assert _assistant_turns(before_followup), "prior turn missing from transcript"

    # Turn 2: a bare metric-shaped follow-up. has_prior_turn is True because the
    # never-seen turn is in the transcript, so it routes as followup and reuses
    # the hydrated reports (zero new tool calls on the report tools).
    for t in tools.values():
        if isinstance(t, MagicMock):
            t.reset_mock()
    second = graph.invoke({"ticker": "NVDA", "question": "elaborate on the RSI"}, config=config)

    assert second["intent"] == "followup"
    tool_calls = sum(t.call_count for t in tools.values() if isinstance(t, MagicMock))
    assert tool_calls == 0, "followup re-ran report tools instead of reusing hydrated state"
