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
from agent.comparison import ComparisonAnswer
from agent.graph import SCRATCH_RESET_KEYS, build_graph
from agent.intent import IntentDecision
from agent.nodes import clarify, gather, narrate, plan, synthesize
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver

from ._thesis_factory import make_comparison_section

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


# QNT-323 follow-up: helpers unpacked (``**helper(...)``) into a node return dict
# whose keys the AST walk cannot resolve statically. Each MUST be confirmed to
# only POPULATE scratch keys, never reset one, before being listed here --
# ``test_no_unreviewed_scratch_helper_unpacks`` fails loudly on any new unpack so
# the blind spot can't hide a reset. Current members:
#   _quick_fact_grounding -> {grounding_rate, grounding_unsupported, confidence},
#     all computed from the answer markdown (populate, never reset).
#   project_answer -> {"answer": ...} -- ``answer`` is not a scratch key at all.
_REVIEWED_SCRATCH_UNPACK_HELPERS: frozenset[str] = frozenset(
    {"_quick_fact_grounding", "project_answer"}
)


def _unpack_name(node: ast.AST) -> str:
    """A readable name for a ``**unpack`` source (the called helper, or the name)."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return ast.dump(target)


def _scratch_writes(func_node: ast.AST) -> tuple[set[str], set[str], set[str]]:
    """Return (populated, reset-only-candidates, unpack-helpers) for a node.

    Walks every ``return {...}`` dict literal in the function (including nested
    helpers). A ``**unpack`` entry carries a ``None`` key: its keys can't be
    resolved statically, so instead of silently skipping it (the pre-follow-up
    blind spot) we record the unpacked helper's name for the allowlist guard.
    """
    populated: set[str] = set()
    reset: set[str] = set()
    unpacks: set[str] = set()
    for sub in ast.walk(func_node):
        if not (isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict)):
            continue
        for key, value in zip(sub.value.keys, sub.value.values):
            if key is None:  # ``**unpack`` -- keys are opaque to the walk
                unpacks.add(_unpack_name(value))
                continue
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if key.value not in SCRATCH_RESET_KEYS:
                continue
            (reset if _is_reset_value(value) else populated).add(key.value)
    return populated, reset, unpacks


@pytest.mark.parametrize("module,walked,name", _NODE_FUNCS, ids=[n for _, _, n in _NODE_FUNCS])
def test_no_node_resets_a_scratch_key_it_does_not_populate(
    module: Any, walked: str, name: str
) -> None:
    """AC2: every scratch key a downstream node returns must be one it also
    populates with a real value somewhere -- a key it ONLY ever returns empty is
    a defensive reset that belongs to classify_node's turn boundary (QNT-323)."""
    populated, reset, _ = _scratch_writes(_find_func(module, walked))
    offenders = reset - populated
    assert not offenders, (
        f"{module.__name__}.{name} returns scratch key(s) {sorted(offenders)} only as a "
        f"reset and never populates them -- classify_node owns the turn-boundary reset "
        f"(QNT-323 G-4). Drop the defensive reset from this node's return dict."
    )


def test_no_unreviewed_scratch_helper_unpacks() -> None:
    """QNT-323 follow-up: close the meta-test's one blind spot. A node return can
    hide keys behind ``**helper(...)`` that the AST walk can't resolve; a helper
    that returned a scratch key as a reset would slip past
    ``test_no_node_resets_...``. Assert every unpacked helper across the walked
    nodes is on the reviewed allowlist -- a new one fails here until someone
    confirms it doesn't reset a scratch key and adds it (or inlines the keys)."""
    seen: set[str] = set()
    for module, walked, _name in _NODE_FUNCS:
        seen |= _scratch_writes(_find_func(module, walked))[2]
    unreviewed = seen - _REVIEWED_SCRATCH_UNPACK_HELPERS
    assert not unreviewed, (
        f"node return dicts unpack unreviewed helper(s) {sorted(unreviewed)} -- the meta-test "
        f"cannot see their keys. Confirm each only POPULATES scratch keys (never resets one), "
        f"then add it to _REVIEWED_SCRATCH_UNPACK_HELPERS, or inline the keys into the return."
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


def _tool_calls(tools: dict[str, MagicMock]) -> int:
    return sum(t.call_count for t in tools.values())


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


# ─── QNT-349 R-1: an interlude turn must not wipe the thread substrate ───────────


@pytest.mark.parametrize(
    "followup_question,expects_card",
    [
        ("tell me more", False),  # narrative-only followup -> answer None, narrate speaks
        ("elaborate on the RSI", True),  # metric-ask followup -> QuickFactAnswer card
    ],
    ids=["narrative-only", "metric-ask"],
)
def test_conversational_interlude_then_followup_reaches_prior_reports(
    stub_llm: _StubLLM,  # noqa: ARG001
    saver: Any,
    followup_question: str,
    expects_card: bool,
) -> None:
    """AC2 (R-1): thesis -> conversational ("hi") -> followup. The conversational
    interlude routes to synthesize and skips plan/gather, so pre-QNT-349 its
    turn-boundary reset wiped ``reports`` and nothing repopulated them -- the
    followup then hit synthesize's no-context redirect ("I don't have a prior
    turn..."). Now the conversational route preserves the substrate, so the
    followup reaches the prior reports instead of the redirect.

    Scope note: the stubbed narrate stream / QuickFactAnswer return canned output
    regardless of prompt, so this fixture pins the STRUCTURAL claims (no redirect,
    ``reports`` preserved verbatim, zero re-fetch) -- not answer content grounding.
    The prior analytical CARD (``prior_answer``) is separately nulled across a
    ConversationalAnswer interlude by pre-existing QNT-307 semantics (out of scope
    here per the ticket); raw report facts still survive, which is what R-1 fixes."""
    tools = _tools()
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": f"interlude:conv:{followup_question}"}}

    first = graph.invoke({"ticker": "NVDA", "question": "give me an NVDA thesis"}, config=config)
    assert first["intent"] == "thesis"
    assert first["reports"]

    # Turn 2: a bare greeting -- a non-analytical interlude (route=synthesize).
    second = graph.invoke({"ticker": "NVDA", "question": "hi"}, config=config)
    assert second["intent"] == "conversational"
    # R-1: the interlude preserved the thread substrate rather than wiping it.
    assert second["reports"] == first["reports"]

    for t in tools.values():
        t.reset_mock()

    # Turn 3: a followup that must land on the prior reports, NOT the redirect.
    third = graph.invoke({"ticker": "NVDA", "question": followup_question}, config=config)
    assert third["intent"] == "followup"
    assert _tool_calls(tools) == 0  # reused hydrated reports, no re-fetch
    assert third["reports"] == first["reports"]
    # QNT-349 follow-up: the prior thesis CARD survives the conversational
    # interlude too (carried through prior_answer), so the followup narrates over
    # the earlier analysis rather than reasoning from raw reports alone.
    assert isinstance(third.get("prior_answer"), Thesis)
    if expects_card:
        # metric-ask -> a real QuickFactAnswer card (the redirect would be a
        # ConversationalAnswer instead).
        assert isinstance(third["answer"], QuickFactAnswer)
    else:
        # narrative-only -> answer cleared, narrate owns the spoken reply. The
        # no-context redirect would leave a ConversationalAnswer here, so
        # answer=None decisively rules it out.
        assert third["answer"] is None
        assert third.get("narrative_substrate") == "prior_answer"
        assert third.get("narrative")  # the narrator actually spoke


def test_metric_ask_followup_then_followup_keeps_prior_card(
    stub_llm: _StubLLM,  # noqa: ARG001
    saver: Any,
) -> None:
    """QNT-349 follow-up: thesis -> metric-ask followup ("elaborate on the RSI",
    which writes a compact QuickFactAnswer) -> "tell me more". The middle turn's
    QuickFactAnswer must not sever the thread: because it was a FOLLOWUP (names no
    ticker, cannot rebase), the third turn still carries the original thesis card
    forward and narrates over it rather than dropping to reports alone."""
    tools = _tools()
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "metric-followup:chain"}}

    first = graph.invoke({"ticker": "NVDA", "question": "give me an NVDA thesis"}, config=config)
    assert first["intent"] == "thesis"
    assert isinstance(first["answer"], Thesis)

    # Turn 2: a metric-ask followup -> QuickFactAnswer, narrating over the thesis.
    second = graph.invoke({"ticker": "NVDA", "question": "elaborate on the RSI"}, config=config)
    assert second["intent"] == "followup"
    assert isinstance(second["answer"], QuickFactAnswer)
    assert isinstance(second.get("prior_answer"), Thesis)

    # Turn 3: another followup. The prior thesis CARD survives the middle metric
    # ask (carried because turn 2 was a followup, not a rebasing quick_fact).
    third = graph.invoke({"ticker": "NVDA", "question": "tell me more"}, config=config)
    assert third["intent"] == "followup"
    assert third["answer"] is None
    assert isinstance(third.get("prior_answer"), Thesis)
    assert third.get("narrative")


class _ClarifyInterludeStubLLM(_StubLLM):
    """``_StubLLM`` plus an IntentDecision arm that labels the turn-2 gesture
    ("compare them") a comparison, so the ambiguity gate -- one resolvable ticker
    on a warm thread -- routes it to clarify. Turns 1/3 resolve via the heuristic
    (thesis / followup) and never reach this arm."""

    def with_structured_output(self, schema: type, **_kwargs: object) -> MagicMock:
        if schema is IntentDecision:
            m = MagicMock()
            m.invoke = MagicMock(return_value=IntentDecision(intent="comparison"))
            m.with_retry.return_value = m
            return m
        return super().with_structured_output(schema, **_kwargs)


def test_clarify_interlude_then_followup_reaches_prior_reports(
    monkeypatch: pytest.MonkeyPatch,
    saver: Any,
) -> None:
    """AC2 (R-1): thesis -> clarify ("compare them", one ticker on the thread) ->
    followup. The clarify route also skips plan/gather, so pre-QNT-349 it wiped
    ``reports`` and the followup degraded to the no-context redirect. The clarify
    route now preserves the substrate like a followup does."""
    stub = _ClarifyInterludeStubLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)

    tools = _tools()
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "interlude:clarify"}}

    first = graph.invoke({"ticker": "NVDA", "question": "give me an NVDA thesis"}, config=config)
    assert first["intent"] == "thesis"
    assert first["reports"]

    # Turn 2: a bare compare gesture with only NVDA on the thread -> clarify.
    second = graph.invoke({"ticker": "NVDA", "question": "compare them"}, config=config)
    assert second["intent"] == "comparison"
    assert second["route"] == "clarify"
    assert second.get("ambiguity_kind") == "needs_second_ticker"
    # R-1: the clarify interlude preserved the substrate.
    assert second["reports"] == first["reports"]

    for t in tools.values():
        t.reset_mock()

    # Turn 3: the followup reaches the prior reports, not the redirect.
    third = graph.invoke({"ticker": "NVDA", "question": "tell me more"}, config=config)
    assert third["intent"] == "followup"
    assert _tool_calls(tools) == 0
    assert third["reports"] == first["reports"]
    assert third["answer"] is None  # not the no-context redirect ConversationalAnswer
    assert third.get("narrative")
    # QNT-349 follow-up: the prior thesis CARD also survives the clarify interlude,
    # so the followup narrates over it, not just the raw reports.
    assert isinstance(third.get("prior_answer"), Thesis)


# ─── QNT-349 R-2: comparison -> followup grounds against the full bundle ─────────


class _ComparisonFollowupStubLLM:
    """Turn 1 returns a real ComparisonAnswer (NVDA vs AAPL) so it is snapshotted
    as ``prior_answer`` and narrated over on turn 2. The narrate stream re-quotes
    a second-ticker (AAPL) figure that lives only in AAPL's report bundle."""

    def __init__(self) -> None:
        self.invoke = MagicMock(return_value=AIMessage(content="technical, fundamental"))
        card = ComparisonAnswer(
            sections=[
                make_comparison_section("NVDA", "Premium", "Uptrend"),
                make_comparison_section("AAPL", "Discounted", "Sideways"),
            ],
            # Card quotes each ticker's RSI; the followup narrative below quotes
            # AAPL's price (180), which lives ONLY in AAPL's technical report.
            differences="NVDA RSI 71 runs hotter than AAPL RSI 44 (source: technical).",
        )

        def make_structured(schema: type) -> MagicMock:
            m = MagicMock()
            m.invoke = MagicMock(return_value=card if schema is ComparisonAnswer else None)
            m.with_retry.return_value = m
            return m

        self._make_structured = make_structured

    def with_structured_output(self, schema: type, **_kwargs: object) -> MagicMock:
        return self._make_structured(schema)

    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        return iter([AIMessage(content="AAPL last changed hands near 180.")])


def _ticker_reports_tools() -> dict[str, MagicMock]:
    """Per-ticker report bodies: AAPL's technical report carries a figure (180)
    that NVDA's does not, so grounding the followup against the primary bundle
    alone would false-flag it."""

    def technical(t: str) -> str:
        rsi = "71" if t == "NVDA" else "44"
        last = "120" if t == "NVDA" else "180"
        return f"## technical\n{t} RSI {rsi}, last {last}\n"

    return {
        "technical": MagicMock(side_effect=technical),
        "fundamental": MagicMock(side_effect=lambda t: f"## fundamental\n{t} P/E 50\n"),
        "company": MagicMock(side_effect=lambda t: f"## company\n{t} business\n"),
        "news": MagicMock(side_effect=lambda t: f"## news\n{t} headline\n"),
    }


def test_comparison_followup_grounds_against_full_bundle(
    monkeypatch: pytest.MonkeyPatch,
    saver: Any,
) -> None:
    """AC3 (R-2): comparison -> followup. The followup narrates over the prior
    ComparisonAnswer (both tickers' numbers). Pre-QNT-349, ``reports_by_ticker``
    was always reset at the boundary, so the grounding check fell back to the
    PRIMARY ticker's reports and false-flagged the second ticker's figures --
    dropping grounding_rate + polluting grounding_unsupported. Preserving
    ``reports_by_ticker`` on followup grounds against the full bundle it narrates
    over, so a faithful second-ticker figure is supported."""
    stub = _ComparisonFollowupStubLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *a, **kw: stub)
    monkeypatch.setattr("agent.intent.get_llm", lambda *a, **kw: stub)

    tools = _ticker_reports_tools()
    graph = build_graph(tools, checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": "cmp-followup:grounding"}}

    first = graph.invoke(
        {"ticker": "NVDA", "question": "Compare NVDA and AAPL on technicals"}, config=config
    )
    assert first["intent"] == "comparison"
    assert set(first["reports_by_ticker"]) == {"NVDA", "AAPL"}
    assert isinstance(first["answer"], ComparisonAnswer)

    # Turn 2: a narrative-only followup whose spoken answer re-quotes AAPL's 180
    # (present in AAPL's preserved report bundle, absent from NVDA's).
    second = graph.invoke({"ticker": "NVDA", "question": "tell me more"}, config=config)
    assert second["intent"] == "followup"
    # R-2: the per-ticker bundle survived the boundary...
    assert set(second["reports_by_ticker"]) == {"NVDA", "AAPL"}
    # ...so the second ticker's figure grounds cleanly rather than false-flagging.
    assert second["grounding_rate"] == 1.0
    assert "180" not in second.get("grounding_unsupported", [])
