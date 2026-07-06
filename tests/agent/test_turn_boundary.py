"""QNT-323 (G-4): classify_node owns the whole turn boundary.

The checkpointer persists AgentState across turns on a warm thread, so a key a
node leaves unwritten hydrates with the PRIOR turn's value. classify_node's
``_turn_boundary_reset`` clears every per-turn SCRATCH key at the boundary;
downstream nodes carry only what they produce (no defensive resets). Two guards:

- AC2 meta-test: statically walk every non-classify node's return-dict literals
  and assert none writes a reset for a scratch key it never populates.
- AC3 round-trip: a followup on a warm thread still sees the prior reports
  (durable), while a fresh analytical turn on a warm thread sees clean scratch
  (a prior comparison's per-ticker bundle does NOT leak in).
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from agent import graph as graph_module
from agent.graph import SCRATCH_RESET_KEYS, build_graph
from agent.nodes import clarify, gather, narrate, plan, synthesize
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver

# ─── AC2: meta-test — no node other than classify resets a foreign scratch key ──

# Every node function that runs downstream of classify. classify_node is EXEMPT
# (it owns the resets). gather.py hosts two nodes, walked separately so per-node
# ownership is enforced (gather populates reports_by_ticker; explore_supervisor
# only ever reset it -- treating the module as one unit would hide that).
# Each entry is (module, walked_fn, registered_node): synthesize's dict returns
# live in ``_synthesize_payload`` and its nested ``_answer`` / ``_fallback``
# helpers (which ``ast.walk`` reaches), while build_graph registers the thin
# ``synthesize_node`` wrapper; for every other node the two names coincide.
_NODE_FUNCS: list[tuple[Any, str, str]] = [
    (plan, "plan_node", "plan_node"),
    (gather, "gather_node", "gather_node"),
    (gather, "explore_supervisor_node", "explore_supervisor_node"),
    (synthesize, "_synthesize_payload", "synthesize_node"),
    (narrate, "narrate_node", "narrate_node"),
    (clarify, "clarify_node", "clarify_node"),
]


def _is_reset_value(node: ast.AST) -> bool:
    """True when the dict value is a 'reset' literal: empty container, None,
    "", 0, 0.0, or False. Anything else (a Name, a Call, a non-empty literal)
    counts as the node populating the key with a produced value."""
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return not node.elts
    if isinstance(node, ast.Constant):
        return node.value in (None, "", 0, 0.0, False)
    return False


def _find_func(module: Any, name: str) -> ast.AST:
    tree = ast.parse(Path(inspect.getfile(module)).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {module.__name__}")


def _scratch_writes(func_node: ast.AST) -> tuple[set[str], set[str]]:
    """Return (populated, reset_only-candidates) scratch keys for a node.

    Walks every ``return {...}`` dict literal in the function (including nested
    helpers). ``**unpack`` entries carry a ``None`` key and are skipped -- their
    contents can't be resolved statically.
    """
    populated: set[str] = set()
    reset: set[str] = set()
    for sub in ast.walk(func_node):
        if not (isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict)):
            continue
        for key, value in zip(sub.value.keys, sub.value.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if key.value not in SCRATCH_RESET_KEYS:
                continue
            (reset if _is_reset_value(value) else populated).add(key.value)
    return populated, reset


@pytest.mark.parametrize("module,walked,name", _NODE_FUNCS, ids=[n for _, _, n in _NODE_FUNCS])
def test_no_node_resets_a_scratch_key_it_does_not_populate(
    module: Any, walked: str, name: str
) -> None:
    """AC2: every scratch key a downstream node returns must be one it also
    populates with a real value somewhere -- a key it ONLY ever returns empty is
    a defensive reset that belongs to classify_node's turn boundary (QNT-323)."""
    populated, reset = _scratch_writes(_find_func(module, walked))
    offenders = reset - populated
    assert not offenders, (
        f"{module.__name__}.{name} returns scratch key(s) {sorted(offenders)} only as a "
        f"reset and never populates them -- classify_node owns the turn-boundary reset "
        f"(QNT-323 G-4). Drop the defensive reset from this node's return dict."
    )


def test_meta_test_covers_every_downstream_node() -> None:
    """Guard the guard: if a new node is registered in build_graph without being
    added to ``_NODE_FUNCS``, its return dicts go unchecked. Pin the covered set
    against the node functions build_graph imports (classify excluded)."""
    build_src = Path(inspect.getfile(graph_module)).read_text()
    imported = {
        node.names[0].asname or node.names[0].name
        for node in ast.walk(ast.parse(build_src))
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("agent.nodes")
    }
    # Node fns are imported as ``<name> as _<name>_fn`` inside build_graph; the
    # router and GraphDeps are not nodes.
    node_fn_aliases = {a for a in imported if a.endswith("_node_fn")}
    covered = {f"_{name}_fn" for _, _, name in _NODE_FUNCS} | {"_classify_node_fn"}
    assert node_fn_aliases == covered, (
        f"build_graph wires node fns {sorted(node_fn_aliases)} but the meta-test covers "
        f"{sorted(covered)}. Add any new node to _NODE_FUNCS (or classify's exemption)."
    )


# ─── AC3: checkpointer round-trip — durable reports survive, scratch is cleared ──


class _StubLLM:
    """Schema-dispatched stub (mirrors tests/agent/test_followup._StubLLM):
    Thesis / QuickFactAnswer resolve to canned shapes, everything else (e.g.
    ComparisonAnswer) returns None so synthesize takes its deterministic
    redirect -- gather has already populated reports_by_ticker by then. ``stream``
    feeds the narrate node two chunks."""

    def __init__(self) -> None:
        self.invoke = MagicMock(return_value=AIMessage(content="technical, fundamental, news"))
        thesis = _stub_thesis()
        quick_fact = QuickFactAnswer(
            answer="RSI 78 (source: technical).", cited_value="78", source="technical"
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
        return iter([AIMessage(content="On balance "), AIMessage(content="the read is cautious.")])


def _stub_thesis() -> Thesis:
    from ._thesis_factory import make_thesis

    return make_thesis(
        company_summary="TSLA framing (source: company).",
        supports=["EV/EBITDA 65 (source: fundamental)"],
        challenges=["RSI 78 overbought (source: technical)"],
        verdict="Underweight",
        verdict_rationale="Premium multiple paired with exhaustion (source: technical).",
    )


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch) -> _StubLLM:
    stub = _StubLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)
    return stub


@pytest.fixture
def saver() -> Any:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    return SqliteSaver(conn)


def _tools() -> dict[str, MagicMock]:
    return {
        "technical": MagicMock(return_value="## technical\nRSI 78\n"),
        "fundamental": MagicMock(return_value="## fundamental\nP/E 80\n"),
        "company": MagicMock(return_value="## company\nDescription\n"),
        "news": MagicMock(return_value="## news\n- headline\n"),
    }


def test_followup_still_sees_prior_reports_across_the_boundary_reset(
    stub_llm: _StubLLM,  # noqa: ARG001
    saver: Any,
) -> None:
    """AC3: ``reports`` is DURABLE on followup -- classify's turn-boundary reset
    deliberately omits it for followup, so a warm-thread followup reuses the
    prior turn's hydrated reports verbatim (zero tool calls)."""
    tools = _tools()
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "boundary:TSLA"}}

    first = graph.invoke({"ticker": "TSLA", "question": "is TSLA overvalued?"}, config=config)
    assert first["intent"] == "thesis"
    assert first["reports"]  # hydrated for the followup turn
    for t in tools.values():
        t.reset_mock()

    second = graph.invoke({"ticker": "TSLA", "question": "elaborate on the RSI"}, config=config)
    assert second["intent"] == "followup"
    # Durable: the followup sees the SAME reports the thesis turn produced...
    assert second["reports"] == first["reports"]
    # ...and re-runs no report tools to get them.
    assert sum(t.call_count for t in tools.values()) == 0


def test_fresh_analytical_turn_on_warm_thread_sees_no_prior_scratch(
    stub_llm: _StubLLM,  # noqa: ARG001
    saver: Any,
) -> None:
    """AC3: a comparison turn populates ``reports_by_ticker`` (per-ticker bundle)
    and ``comparison_tickers``; a later single-ticker quick_fact on the SAME
    thread must see them cleared. Only classify's turn-boundary reset clears
    reports_by_ticker now (gather/plan/explore dropped their defensive resets),
    so this fails if that reset regresses -- the prior comparison's bundle would
    leak into the quick_fact turn's state."""
    tools = _tools()
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "boundary:scratch"}}

    # Turn 1: a two-ticker comparison. gather's rich branch fills reports_by_ticker
    # for both tickers before synthesize runs (the ComparisonAnswer LLM call
    # returns None -> deterministic redirect, but the bundle is already in state).
    first = graph.invoke(
        {"ticker": "NVDA", "question": "Compare NVDA and AAPL on valuation."}, config=config
    )
    assert first["intent"] == "comparison"
    assert set(first["reports_by_ticker"]) == {"NVDA", "AAPL"}
    assert first["comparison_tickers"] == ["NVDA", "AAPL"]

    # Turn 2: a fresh single-ticker quick_fact on the warm thread. No comparison
    # scratch may survive the boundary.
    second = graph.invoke({"ticker": "NVDA", "question": "What's NVDA's RSI?"}, config=config)
    assert second["intent"] == "quick_fact"
    assert second["reports_by_ticker"] == {}
    assert second["comparison_tickers"] == []
    assert second["retrieved_sources"] == []


def test_single_writer_scratch_keys_cleared_on_warm_thread(
    stub_llm: _StubLLM,  # noqa: ARG001
    saver: Any,
) -> None:
    """AC3 regression: ``plan_rationale`` (only plan / explore_supervisor write it)
    and ``supervisor_iterations`` (only explore_supervisor writes it) are the
    single-writer scratch keys that leak most quietly. An exploration turn sets
    both; a later quick_fact that never runs the supervisor must see them cleared.
    Only classify's turn-boundary reset clears them now -- before QNT-323 the
    stale plan_rationale fed into build_narrate_prompt on the next turn and the
    stale iteration count into every subsequent SSE done event."""
    tools = _tools()
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "boundary:single-writer"}}

    # Turn 1: a broad exploratory scan -- explore_supervisor sets plan_rationale
    # (deterministic scan sentence) and supervisor_iterations (len of the plan).
    first = graph.invoke(
        {"ticker": "TSLA", "question": "What's interesting about TSLA this week?"}, config=config
    )
    assert first["intent"] == "exploration"
    assert first.get("plan_rationale")  # producer set a real rationale
    assert first.get("supervisor_iterations", 0) > 0

    # Turn 2: a single-metric quick_fact never runs the supervisor or a rationale
    # writer -- neither key may survive the boundary.
    second = graph.invoke({"ticker": "TSLA", "question": "What's TSLA's RSI?"}, config=config)
    assert second["intent"] == "quick_fact"
    assert second.get("plan_rationale") is None
    assert second.get("supervisor_iterations") == 0
