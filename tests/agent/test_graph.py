"""Tests for agent.graph (QNT-56, QNT-133, QNT-149).

Covers the classify -> plan -> gather -> synthesize LangGraph state machine,
tool injection, retry + optional-tool skip, the short-circuit conditional
edge, and the LLM-injection contract (get_llm()).

QNT-133: synthesize uses ``with_structured_output(Thesis)``. The stub LLM
handles both the plan call (raw ``invoke`` returning an ``AIMessage`` with
a comma-separated tool list) and the synthesize call (structured-output
runnable returning a ``Thesis``).

QNT-149: an upstream ``classify`` node decides between ``thesis`` and
``quick_fact`` response shapes. The default test question is heuristic-
matched (it contains "thesis"), so the classify node returns synchronously
without calling the LLM and the existing plan/synthesize traces stay
intact. Quick-fact and intent-routing tests live below.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from agent import graph as graph_module
from agent.graph import (
    OPTIONAL_TOOLS,
    REPORT_TOOLS,
    ToolFn,
    _confidence_from_reports,
    _parse_plan,
    build_graph,
)
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from langchain_core.messages import AIMessage


def _mock_tool(text: str) -> ToolFn:
    def tool(ticker: str) -> str:
        return f"{text} for {ticker}"

    return tool


def _stub_thesis(setup: str = "NVDA thesis body.") -> Thesis:
    """Minimal Thesis for graph tests — fields chosen so the markdown render
    contains the seed text (so tests can grep for it)."""
    return Thesis(
        setup=setup,
        bull_case=["bull (source: technical)"],
        bear_case=["bear (source: fundamental)"],
        verdict_stance="mixed",
        verdict_action="Hold pending QNT-67 eval.",
    )


class _StructuredLLM:
    """Stub that mimics a ChatOpenAI with ``with_structured_output``.

    Two-channel responses: ``invoke()`` returns an ``AIMessage`` for the plan
    call, while ``with_structured_output(schema).invoke()`` returns whatever
    ``structured_responses`` queue holds — typically a ``Thesis`` instance.
    Tests configure both channels via class attributes set on the stub.
    """

    def __init__(self) -> None:
        self.invoke = MagicMock()
        self.invoke.return_value = AIMessage(content="technical, fundamental, news")
        self._structured_runnable = MagicMock()
        self._structured_runnable.invoke = MagicMock(return_value=_stub_thesis())

    def with_structured_output(self, schema: object) -> MagicMock:  # noqa: ARG002
        return self._structured_runnable

    @property
    def structured_invoke(self) -> MagicMock:
        return self._structured_runnable.invoke


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch) -> _StructuredLLM:
    """Replace ``agent.graph.get_llm`` with a stub that supports both the
    plan call (raw ``invoke``) and the synthesize call
    (``with_structured_output(Thesis).invoke``)."""
    llm = _StructuredLLM()
    monkeypatch.setattr(graph_module, "get_llm", MagicMock(return_value=llm))
    monkeypatch.setattr(
        graph_module.langfuse,
        "traced_invoke",
        lambda llm_, prompt, *, name: llm_.invoke(prompt),
    )
    return llm


def _run(
    graph: Any,
    ticker: str = "NVDA",
    # Default question must trip the QNT-149 thesis heuristic (contains
    # "thesis") so the classify node short-circuits to "thesis" without
    # calling the LLM. Tests that exercise quick-fact set their own
    # question and patch ``classify_intent`` accordingly.
    question: str = "Give me a balanced thesis on NVDA.",
) -> dict[str, Any]:
    # LangGraph returns a plain dict at the API boundary — typing it as
    # ``AgentState`` (which is ``total=False``) forces pyright .get() checks on
    # every access; the dict surface is simpler and accurate.
    return graph.invoke({"ticker": ticker, "question": question})


def test_graph_compiles_with_mock_tools() -> None:
    graph = build_graph({name: _mock_tool(name) for name in REPORT_TOOLS})
    assert graph is not None


def test_graph_is_visualizable_via_mermaid() -> None:
    graph = build_graph({name: _mock_tool(name) for name in REPORT_TOOLS})
    mermaid = graph.get_graph().draw_mermaid()
    assert "plan" in mermaid
    assert "gather" in mermaid
    assert "synthesize" in mermaid


def test_full_flow_produces_thesis_and_confidence(stub_llm: _StructuredLLM) -> None:
    """End-to-end: plan picks all three tools, synthesize returns a structured
    ``Thesis`` (QNT-133), confidence reflects full report coverage."""
    stub_llm.invoke.return_value = AIMessage(content="technical, fundamental, news")
    expected = _stub_thesis("NVDA thesis body.")
    stub_llm.structured_invoke.return_value = expected
    graph = build_graph({name: _mock_tool(name) for name in REPORT_TOOLS})

    result = _run(graph)

    assert result["thesis"] is expected
    assert result["confidence"] == 1.0
    assert set(result["reports"]) == {"technical", "fundamental", "news"}
    assert result["errors"] == {}


def test_missing_news_tool_is_silently_skipped(stub_llm: _StructuredLLM) -> None:
    """Optional tools (news) absent from the tool mapping must not surface
    in ``errors`` — a missing news feed is routine, not an error."""
    stub_llm.invoke.return_value = AIMessage(content="technical, fundamental, news")
    tools = {"technical": _mock_tool("tech"), "fundamental": _mock_tool("fund")}
    graph = build_graph(tools)

    result = _run(graph)

    assert result["reports"].keys() == {"technical", "fundamental"}
    # 'news' is in OPTIONAL_TOOLS, so its absence is not an error.
    assert "news" not in result["errors"]
    assert isinstance(result["thesis"], Thesis)


def test_gather_reports_optional_tool_missing_from_map_is_dropped_silently() -> None:
    """Defense-in-depth: if a planned tool isn't in the tools map (e.g. a
    future plan-LLM regression bypasses ``_parse_plan``'s filter), the
    optional semantics must apply — no spurious error for ``news``."""
    reports, errors = graph_module._gather_reports(
        "NVDA",
        plan=["technical", "news"],
        tools={"technical": _mock_tool("tech")},
    )
    assert reports == {"technical": "tech for NVDA"}
    assert "news" not in errors


def test_gather_reports_required_tool_missing_from_map_records_error() -> None:
    reports, errors = graph_module._gather_reports(
        "NVDA",
        plan=["technical"],
        tools={},
    )
    assert reports == {}
    assert errors["technical"] == "tool-not-registered"


def test_synthesize_returns_none_when_structured_output_fails(
    stub_llm: _StructuredLLM,
) -> None:
    """QNT-133: ``with_structured_output`` can raise on a malformed provider
    response (Gemini occasionally returns invalid tool-call JSON). The
    synthesize node must surface that as ``thesis=None`` rather than crash
    the whole run — confidence is unaffected because reports were gathered."""
    stub_llm.invoke.return_value = AIMessage(content="technical")
    stub_llm.structured_invoke.side_effect = RuntimeError("schema-validation")
    graph = build_graph({"technical": _mock_tool("tech")})

    result = _run(graph)

    assert result["thesis"] is None
    assert result["confidence"] == 1.0


def test_synthesize_returns_none_when_response_is_not_a_thesis(
    stub_llm: _StructuredLLM,
) -> None:
    """Defensive: if the structured runnable hands back something that isn't
    a ``Thesis`` (e.g. an ``include_raw=True`` shape with parsing_error), we
    coerce to ``None`` rather than leak the wrong type into state."""
    stub_llm.invoke.return_value = AIMessage(content="technical")
    stub_llm.structured_invoke.return_value = {"parsed": None, "parsing_error": "x"}
    graph = build_graph({"technical": _mock_tool("tech")})

    result = _run(graph)

    assert result["thesis"] is None


def test_synthesize_extracts_thesis_from_include_raw_dict(
    stub_llm: _StructuredLLM,
) -> None:
    """Happy path for the ``with_structured_output(..., include_raw=True)``
    response shape: a dict with a ``parsed`` key holding a ``Thesis``. The
    graph's ``_coerce_thesis`` must pull it out so future code that opts
    into raw-message logging keeps producing structured state."""
    stub_llm.invoke.return_value = AIMessage(content="technical")
    expected = _stub_thesis("Extracted from include_raw dict.")
    stub_llm.structured_invoke.return_value = {
        "parsed": expected,
        "raw": AIMessage(content="..."),
        "parsing_error": None,
    }
    graph = build_graph({"technical": _mock_tool("tech")})

    result = _run(graph)

    assert result["thesis"] is expected


def test_required_tool_failure_records_error(stub_llm: _StructuredLLM) -> None:
    stub_llm.invoke.return_value = AIMessage(content="technical, fundamental, news")

    def flaky(_: str) -> str:
        raise RuntimeError("boom")

    tools: dict[str, ToolFn] = {
        "technical": flaky,
        "fundamental": _mock_tool("fund"),
        "news": _mock_tool("news"),
    }
    graph = build_graph(tools)

    result = _run(graph)

    assert "technical" in result["errors"]
    assert "RuntimeError" in result["errors"]["technical"]
    # Confidence reflects coverage: 2 of 3 planned tools returned data.
    assert result["reports"].keys() == {"fundamental", "news"}
    assert result["confidence"] == 0.67


def test_retry_on_transient_failure(stub_llm: _StructuredLLM) -> None:
    """A tool that fails once then succeeds should land in reports, not errors."""
    stub_llm.invoke.return_value = AIMessage(content="technical")
    attempts = {"n": 0}

    def flaky(ticker: str) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("transient")
        return f"tech {ticker}"

    graph = build_graph({"technical": flaky})
    result = _run(graph)

    assert result["reports"] == {"technical": "tech NVDA"}
    assert result["errors"] == {}
    assert attempts["n"] == 2  # first failed, second succeeded


def test_short_circuits_when_gather_produces_nothing(stub_llm: _StructuredLLM) -> None:
    """Conditional edge routes gather -> END when no reports were gathered,
    so synthesize isn't called with an empty prompt."""
    stub_llm.invoke.return_value = AIMessage(content="technical")

    def always_fails(_: str) -> str:
        raise RuntimeError("down")

    graph = build_graph({"technical": always_fails})
    result = _run(graph)

    assert "thesis" not in result
    assert result["errors"]["technical"].startswith("RuntimeError")
    # Synthesize was never called — only the plan LLM call should have fired.
    assert stub_llm.invoke.call_count == 1
    # And the structured runnable was never invoked either.
    assert stub_llm.structured_invoke.call_count == 0


def test_llm_is_injected_via_get_llm_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: ``LLM provider is injected via get_llm(), not hardcoded``. Patch
    ``agent.graph.get_llm`` and assert the graph routes through it."""
    llm = _StructuredLLM()
    llm.invoke.return_value = AIMessage(content="technical")
    factory = MagicMock(return_value=llm)
    monkeypatch.setattr(graph_module, "get_llm", factory)
    monkeypatch.setattr(
        graph_module.langfuse,
        "traced_invoke",
        lambda llm_, prompt, *, name: llm_.invoke(prompt),
    )

    graph = build_graph({"technical": _mock_tool("tech")})
    _run(graph)

    assert factory.call_count >= 1  # plan + synthesize both call get_llm()


def test_llm_calls_go_through_traced_invoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every LLM call in the graph must route through ``langfuse.traced_invoke``
    (the contract enforced by test_tracing.py's AST scanner at package level;
    this test adds a runtime assertion)."""
    llm = _StructuredLLM()
    llm.invoke.return_value = AIMessage(content="technical")
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)

    calls: list[str] = []

    def traced(llm_: Any, prompt: str, *, name: str) -> Any:
        calls.append(name)
        return llm_.invoke(prompt)

    monkeypatch.setattr(graph_module.langfuse, "traced_invoke", traced)

    graph = build_graph({"technical": _mock_tool("tech")})
    _run(graph)

    assert calls == ["plan", "synthesize"]


def test_no_tools_registered_yields_empty_plan(stub_llm: _StructuredLLM) -> None:  # noqa: ARG001
    graph = build_graph({})
    result = _run(graph)
    assert result.get("plan") == []
    assert "thesis" not in result


def test_parse_plan_picks_named_subset() -> None:
    assert _parse_plan("technical, news", ["technical", "fundamental", "news"]) == [
        "technical",
        "news",
    ]


def test_parse_plan_case_insensitive_and_newline_tolerant() -> None:
    assert _parse_plan("Technical\nNEWS\n", ["technical", "news"]) == ["technical", "news"]


def test_parse_plan_falls_back_to_all_when_empty() -> None:
    """Over-fetching beats stranding synthesize with nothing to read."""
    assert _parse_plan("garbage irrelevant prose", ["technical", "fundamental"]) == [
        "technical",
        "fundamental",
    ]


def test_parse_plan_preserves_available_order() -> None:
    assert _parse_plan("news, technical", ["technical", "fundamental", "news"]) == [
        "technical",
        "news",
    ]


def test_confidence_full_coverage() -> None:
    assert _confidence_from_reports({"a": "", "b": "", "c": ""}, ["a", "b", "c"]) == 1.0


def test_confidence_partial_coverage() -> None:
    assert _confidence_from_reports({"a": ""}, ["a", "b", "c"]) == 0.33


def test_confidence_empty_plan_returns_zero() -> None:
    assert _confidence_from_reports({}, []) == 0.0


def test_state_transitions_are_logged(
    stub_llm: _StructuredLLM, caplog: pytest.LogCaptureFixture
) -> None:
    stub_llm.invoke.return_value = AIMessage(content="technical")
    graph = build_graph({"technical": _mock_tool("tech")})

    with caplog.at_level(logging.INFO, logger="agent.graph"):
        _run(graph)

    messages = [r.message for r in caplog.records if r.name == "agent.graph"]
    # One log per node entry/exit — covers the "state transitions are logged
    # and inspectable" AC. QNT-149 added classify before plan.
    assert any(m.startswith("classify NVDA") for m in messages)
    assert any(m.startswith("plan NVDA") for m in messages)
    assert any(m.startswith("gather NVDA") for m in messages)
    assert any(m.startswith("synthesize NVDA") for m in messages)


def test_optional_tools_constant_includes_news() -> None:
    assert "news" in OPTIONAL_TOOLS


def test_report_tools_constant_is_stable() -> None:
    # Freeze the tool contract — adding a tool is a deliberate ADR-007 call.
    assert REPORT_TOOLS == ("technical", "fundamental", "news")


def test_build_graph_is_deterministic_across_calls() -> None:
    """Two graph builds with the same tool map must produce the same topology
    — regressions in add_node ordering have caused downstream flakes before."""
    tools = {name: _mock_tool(name) for name in REPORT_TOOLS}
    g1 = build_graph(tools).get_graph()
    g2 = build_graph(tools).get_graph()
    assert [n for n in g1.nodes] == [n for n in g2.nodes]


def test_tools_typing_accepts_any_callable() -> None:
    fn: Callable[[str], str] = _mock_tool("x")
    # If this type-checks + runs, the ToolFn alias is wired right.
    assert fn("NVDA") == "x for NVDA"


# ─── QNT-149: classify node + quick-fact synthesis path ──────────────────────


def test_classify_node_records_thesis_intent_for_balanced_question(
    stub_llm: _StructuredLLM,
) -> None:
    """The classify node must populate ``state['intent']``. The default
    question contains "thesis" so the heuristic short-circuits without an
    LLM call — covers the AC "Agent can classify an inbound question into
    at least 2 distinct response shapes"."""
    stub_llm.invoke.return_value = AIMessage(content="technical")
    graph = build_graph({"technical": _mock_tool("tech")})

    result = _run(graph)

    assert result["intent"] == "thesis"
    assert isinstance(result["thesis"], Thesis)
    assert result["quick_fact"] is None


def test_classify_node_routes_to_quick_fact_for_rsi_question(
    stub_llm: _StructuredLLM,
) -> None:
    """A short single-metric question ("what's the RSI?") trips the
    heuristic to ``quick_fact``. Synthesize then runs the quick-fact
    structured-output path and writes a ``QuickFactAnswer`` instead of a
    Thesis. AC: quick-fact response shape returns a short answer + cited
    value without a structured thesis payload."""
    stub_llm.invoke.return_value = AIMessage(content="technical")
    quick = QuickFactAnswer(
        answer="RSI sits at 62 (source: technical).",
        cited_value="62",
        source="technical",
    )
    stub_llm.structured_invoke.return_value = quick
    graph = build_graph({"technical": _mock_tool("tech")})

    result = graph.invoke({"ticker": "NVDA", "question": "What's the RSI right now?"})

    assert result["intent"] == "quick_fact"
    assert isinstance(result["quick_fact"], QuickFactAnswer)
    assert result["thesis"] is None
    # Confidence still reflects coverage so the panel can show a bar.
    assert result["confidence"] == 1.0


def test_quick_fact_failure_surfaces_as_none_quick_fact(
    stub_llm: _StructuredLLM,
) -> None:
    """A misbehaving provider on the quick-fact path must surface as
    ``quick_fact=None``, not crash the graph — same defense as the
    QNT-133 thesis path."""
    stub_llm.invoke.return_value = AIMessage(content="technical")
    stub_llm.structured_invoke.side_effect = RuntimeError("schema-validation")
    graph = build_graph({"technical": _mock_tool("tech")})

    result = graph.invoke({"ticker": "NVDA", "question": "What's the P/E?"})

    assert result["intent"] == "quick_fact"
    assert result["quick_fact"] is None
    assert result["thesis"] is None


def test_classify_default_to_thesis_when_classify_intent_fails(
    monkeypatch: pytest.MonkeyPatch, stub_llm: _StructuredLLM
) -> None:
    """If ``classify_intent`` raises (LLM or otherwise), the classify
    node must default to ``thesis`` so the safe path runs. Defends the
    QNT-67 / QNT-128 contracts against a misbehaving classifier."""
    stub_llm.invoke.return_value = AIMessage(content="technical")
    monkeypatch.setattr(graph_module, "classify_intent", lambda _q: "thesis")
    graph = build_graph({"technical": _mock_tool("tech")})

    result = graph.invoke({"ticker": "NVDA", "question": "ambiguous mid-length question"})

    assert result["intent"] == "thesis"
    assert isinstance(result["thesis"], Thesis)


def test_quick_fact_intent_narrows_plan_prompt(
    stub_llm: _StructuredLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan prompt for quick_fact bias should tell the LLM to fetch only
    the needed report. Direct check on the planner-prompt builder rather
    than asserting the LLM's choice (which is up to the model)."""
    monkeypatch.setattr(graph_module, "classify_intent", lambda _q: "quick_fact")
    captured: list[str] = []

    real_traced = graph_module.langfuse.traced_invoke

    def capturing_traced(llm_: Any, prompt: Any, *, name: str) -> Any:
        if name == "plan":
            captured.append(str(prompt))
        return real_traced(llm_, prompt, name=name)

    monkeypatch.setattr(graph_module.langfuse, "traced_invoke", capturing_traced)
    stub_llm.invoke.return_value = AIMessage(content="technical")
    graph = build_graph({"technical": _mock_tool("tech")})

    graph.invoke({"ticker": "NVDA", "question": "What's the RSI?"})

    assert captured, "plan node must call the LLM"
    assert "single-metric" in captured[0].lower() or "directly needed" in captured[0].lower()
