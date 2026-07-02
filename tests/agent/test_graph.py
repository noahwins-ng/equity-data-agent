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

import json
import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from agent import graph as graph_module
from agent.graph import (
    OPTIONAL_TOOLS,
    REPORT_TOOLS,
    ThesisPlan,
    ToolFn,
    _composite_confidence,
    _confidence_from_reports,
    _format_search_hits,
    _parse_plan,
    _runtime_grounding_check,
    build_graph,
)
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from langchain_core.messages import AIMessage


def _mock_tool(text: str) -> ToolFn:
    def tool(ticker: str) -> str:
        return f"{text} for {ticker}"

    return tool


def _stub_thesis(company_summary: str = "NVDA thesis body.") -> Thesis:
    """Minimal Thesis for graph tests -- fields chosen so the markdown render
    contains the seed text (so tests can grep for it)."""
    from ._thesis_factory import make_thesis

    return make_thesis(
        company_summary=company_summary,
        supports=["bull (source: technical)"],
        challenges=["bear (source: fundamental)"],
        verdict="Neutral",
        verdict_rationale="Premium and Uptrend tension (source: technical).",
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
        self._plan_runnable = MagicMock()
        self._plan_runnable.invoke = MagicMock(
            return_value=ThesisPlan(
                tools=["company", "technical", "fundamental", "news"],
                rationale="Balanced thesis, so all reports are relevant.",
            )
        )
        self._plan_runnable.with_retry.return_value = self._plan_runnable
        self._structured_runnable = MagicMock()
        self._structured_runnable.invoke = MagicMock(return_value=_stub_thesis())
        # with_retry() must return the same mock so .invoke stays configured.
        # The synthesize node calls .with_retry() on the structured runnable;
        # a fresh auto-generated MagicMock would lose the return_value.
        self._structured_runnable.with_retry.return_value = self._structured_runnable

    def with_structured_output(self, schema: object) -> MagicMock:
        if schema is ThesisPlan:
            return self._plan_runnable
        return self._structured_runnable

    @property
    def structured_invoke(self) -> MagicMock:
        return self._structured_runnable.invoke

    @property
    def plan_invoke(self) -> MagicMock:
        return self._plan_runnable.invoke


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch) -> _StructuredLLM:
    """Replace ``agent.graph.get_llm`` and ``agent.intent.get_llm`` with a
    stub that supports both the plan call (raw ``invoke``) and the synthesize
    call (``with_structured_output(Thesis).invoke``).

    QNT-156: ``intent.get_llm`` MUST be patched too — when the classifier
    heuristic returns None, ``classify_intent`` calls its own ``get_llm``
    instance, which would otherwise try the real LiteLLM proxy. CI has no
    proxy, so an unpatched call surfaces as a connection-error → bias-to-
    thesis fallback that masquerades as a different bug. Local dev runs
    with LiteLLM up and would silently pass.
    """
    from agent import intent as intent_module

    llm = _StructuredLLM()
    factory = MagicMock(return_value=llm)
    monkeypatch.setattr(graph_module, "get_llm", factory)
    monkeypatch.setattr(intent_module, "get_llm", factory)
    # QNT-181: nodes call ``llm.invoke(prompt, config=config)`` directly now
    # that traced_invoke is gone. The stub's ``invoke`` is a MagicMock so it
    # accepts the extra ``config=`` kwarg unchanged — no extra patching needed.
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
    # QNT-175: ``company`` is force-included on the thesis path even when the
    # plan LLM omits it, so the gathered set covers every REPORT_TOOLS entry.
    assert set(result["reports"]) == set(REPORT_TOOLS)
    assert result["errors"] == {}


def test_runtime_grounding_rate_lowers_composite_confidence() -> None:
    clean, clean_rate = _runtime_grounding_check("RSI is 62.", ["RSI is 62."])
    miss, miss_rate = _runtime_grounding_check("RSI is 99.", ["RSI is 62."])

    assert clean.ok is True
    assert clean_rate == 1.0
    assert miss.ok is False
    assert miss_rate < 1.0
    assert _composite_confidence(1.0, clean_rate) == 1.0
    assert _composite_confidence(1.0, miss_rate) < 1.0


def test_composite_confidence_covers_each_factor() -> None:
    assert _composite_confidence(1.0, 1.0, 1.0) == 1.0
    assert _composite_confidence(0.5, 1.0, 1.0) == 0.5
    assert _composite_confidence(1.0, 0.5, 1.0) == 0.5
    assert _composite_confidence(1.0, 1.0, 0.5) == 0.5


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


def test_gather_reports_tool_error_string_records_error() -> None:
    reports, errors = graph_module._gather_reports(
        "NVDA",
        plan=["technical", "news"],
        tools={
            "technical": _mock_tool("[error] http-500: unavailable"),
            "news": _mock_tool("[error] http-500: unavailable"),
        },
    )

    assert reports == {}
    assert errors["technical"] == "[error] http-500: unavailable for NVDA"
    assert "news" not in errors


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


def test_thesis_retries_on_validation_error_and_returns_valid_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-196 Phase 2: a ValidationError on the first synthesize attempt must
    trigger a retry via with_retry(); the second attempt's valid Thesis must
    reach the user rather than the domain_redirect fallback."""
    from agent import intent as intent_module
    from langchain_core.runnables import RunnableLambda
    from pydantic import BaseModel, ValidationError

    def _make_validation_error() -> ValidationError:
        class _M(BaseModel):
            x: int

        try:
            _M(x="not-an-int")  # type: ignore[arg-type]
        except ValidationError as exc:
            return exc
        raise AssertionError("unreachable")

    valid_thesis = _stub_thesis("Retry recovery thesis.")
    call_count = [0]

    def _invoke(_input: object) -> object:
        call_count[0] += 1
        if call_count[0] == 1:
            raise _make_validation_error()
        return valid_thesis

    class _RetryCapableLLM:
        """Minimal stub whose with_structured_output returns a real RunnableLambda
        so with_retry() produces an actual RunnableRetry (not another MagicMock)."""

        def __init__(self) -> None:
            self.invoke = MagicMock(return_value=AIMessage(content="technical"))

        def with_structured_output(self, schema: object) -> RunnableLambda:
            if schema is ThesisPlan:
                return RunnableLambda(
                    lambda _input: ThesisPlan(
                        tools=["company", "technical", "fundamental", "news"],
                        rationale="Balanced thesis, so all reports are relevant.",
                    )
                )
            return RunnableLambda(_invoke)

    llm = _RetryCapableLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)
    monkeypatch.setattr(intent_module, "get_llm", lambda *_a, **_kw: llm)

    graph = build_graph({name: _mock_tool(name) for name in REPORT_TOOLS})
    result = _run(graph)

    assert result["thesis"] is valid_thesis, "retry recovery must produce the valid thesis"
    assert result["conversational"] is None, "no fallback redirect when retry succeeds"
    assert call_count[0] == 2, "structured output must be called twice (first fail, second ok)"


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


def test_no_reports_gathered_falls_back_to_conversational_redirect(
    stub_llm: _StructuredLLM,
) -> None:
    """QNT-156: when gather produces nothing on a thesis ask, synthesize
    no longer short-circuits to END. Instead it emits a deterministic
    conversational redirect via ``domain_redirect`` so the panel always
    sees an in-domain reply (cf. ADR-014 §4 — no blank states).

    The structured runnable still must NOT fire (no LLM call wasted on an
    empty prompt) — only the plan LLM call counts."""
    from agent.conversational import ConversationalAnswer

    stub_llm.invoke.return_value = AIMessage(content="technical")

    def always_fails(_: str) -> str:
        raise RuntimeError("down")

    graph = build_graph({"technical": always_fails})
    result = _run(graph)

    assert result["thesis"] is None
    assert result["quick_fact"] is None
    assert result["comparison"] is None
    assert isinstance(result["conversational"], ConversationalAnswer)
    assert result["errors"]["technical"].startswith("RuntimeError")
    # The thesis planner fires once, but synthesize does NOT call the LLM
    # because gather produced no reports and the fallback is deterministic,
    # not generated. So the chat-shape stub never sees an invoke.
    assert stub_llm.invoke.call_count == 0
    assert stub_llm.plan_invoke.call_count == 1
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

    graph = build_graph({"technical": _mock_tool("tech")})
    _run(graph)

    assert factory.call_count >= 1  # plan + synthesize both call get_llm()


def _extract_config_metadata(call_args: Any) -> dict[str, object]:
    """Pull the metadata dict out of an invoke call_args, whether config was
    passed as a keyword or (unlikely) positional second argument."""
    cfg: dict[str, object] = call_args.kwargs.get("config") or (
        call_args.args[1] if len(call_args.args) > 1 else {}
    )
    return cfg.get("metadata") or {}  # type: ignore[return-value]


def test_llm_calls_carry_prompt_version_in_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-187: thesis-path synthesize LLM call must embed prompt_version in
    config metadata so Langfuse traces are filterable by prompt hash."""
    from agent.graph import _PROMPT_VERSION

    llm = _StructuredLLM()
    llm.invoke.return_value = AIMessage(content="technical")
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)

    graph = build_graph({"technical": _mock_tool("tech")})
    _run(graph)

    metadata = _extract_config_metadata(llm._structured_runnable.invoke.call_args)
    assert isinstance(metadata, dict), f"config['metadata'] must be a dict; got {metadata!r}"
    assert "prompt_version" in metadata, f"config metadata missing prompt_version; got {metadata!r}"
    assert metadata["prompt_version"] == _PROMPT_VERSION


def test_plan_llm_call_passes_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-199: the quick_fact plan-LLM call (non-structured get_llm().invoke)
    uses a dynamically built planning prompt with no named Langfuse equivalent,
    so it passes config= directly without prompt-version injection. The AST gate
    (test_llm_invoke_calls_pass_config_kwarg) enforces config= presence; this
    test verifies the plan-LLM call actually fires on the quick_fact path."""
    from agent import intent as intent_module

    llm = _StructuredLLM()
    llm.invoke.return_value = AIMessage(content="technical")
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)
    monkeypatch.setattr(intent_module, "get_llm", lambda *_a, **_kw: llm)

    graph = build_graph({name: _mock_tool(name) for name in REPORT_TOOLS})
    graph.invoke({"ticker": "NVDA", "question": "What's NVDA's RSI?"})

    # quick_fact fires the plan-LLM call (raw .invoke) then a structured synthesize.
    assert llm.invoke.call_count >= 1, "quick_fact must fire the plan-LLM call"


def test_llm_calls_carry_runtime_config_for_callback_propagation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-181: every LLM call inside a graph node must pass
    ``config=`` so the LangGraph CallbackHandler attached at graph entry
    propagates to LLM-level generation observations. This is the runtime
    counterpart to the AST contract test in test_tracing.py."""
    llm = _StructuredLLM()
    llm.invoke.return_value = AIMessage(content="technical")
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)

    graph = build_graph({"technical": _mock_tool("tech")})
    _run(graph)

    # Thesis uses a structured plan call and a structured synthesize call.
    # Assert both carried a ``config=`` kwarg — the keyword is the
    # CallbackHandler propagation channel.
    assert llm.plan_invoke.call_count == 1
    plan_call = llm.plan_invoke.call_args
    assert "config" in plan_call.kwargs, (
        f"plan llm.invoke must pass config=; got kwargs={plan_call.kwargs!r}"
    )
    assert llm._structured_runnable.invoke.call_count == 1
    synth_call = llm._structured_runnable.invoke.call_args
    assert "config" in synth_call.kwargs, (
        f"synthesize llm.invoke must pass config=; got kwargs={synth_call.kwargs!r}"
    )


def test_no_tools_registered_yields_empty_plan(stub_llm: _StructuredLLM) -> None:  # noqa: ARG001
    """QNT-156: with no tools registered, plan emits an empty list and
    synthesize falls back to a deterministic conversational redirect
    (the panel never sees a blank state)."""
    from agent.conversational import ConversationalAnswer

    graph = build_graph({})
    result = _run(graph)
    assert result.get("plan") == []
    assert result["thesis"] is None
    assert isinstance(result["conversational"], ConversationalAnswer)


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


def test_parse_plan_force_includes_company_for_thesis() -> None:
    """QNT-175: thesis intent always pulls ``company`` even when the plan LLM
    omits it. The plan-prompt bias is enforced as code so a misbehaving LLM
    can't strand the synthesize step without business context."""
    plan = _parse_plan(
        "technical, fundamental",
        ["company", "technical", "fundamental", "news"],
        intent="thesis",
    )
    assert "company" in plan
    # Order follows ``available`` so company appears at the head.
    assert plan == ["company", "technical", "fundamental"]


def test_parse_plan_drops_company_for_quick_fact() -> None:
    """QNT-175: quick_fact intent never fetches the static company report —
    a single-metric ask doesn't benefit from the description / competitor list."""
    plan = _parse_plan(
        "company, technical",
        ["company", "technical", "fundamental", "news"],
        intent="quick_fact",
    )
    assert plan == ["technical"]


def test_thesis_calls_structured_plan_llm_and_fetches_chosen_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-213: thesis intent calls the structured planner, then synthesize."""
    llm = _StructuredLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)

    graph = build_graph({name: _mock_tool(name) for name in REPORT_TOOLS})
    result = _run(graph)

    # Thesis: one structured planner call, one synthesize call. The raw
    # comma-list planner remains reserved for quick_fact below.
    assert llm.invoke.call_count == 0
    assert llm.plan_invoke.call_count == 1
    assert llm._structured_runnable.invoke.call_count == 1
    # The default stub chooses every available tool.
    assert set(result["reports"]) == set(REPORT_TOOLS)


def test_quick_fact_still_calls_plan_llm_to_narrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counterpart to the thesis-skips-plan test: quick_fact KEEPS the plan
    LLM call because narrowing to the single relevant report is the whole
    point of that path. Without this, a future "skip plan everywhere"
    refactor would silently widen quick_fact to 3-4 reports per single-
    metric question, burning provider quota and re-creating the over-fetch
    problem the path was carved out of."""
    llm = _StructuredLLM()
    llm.invoke.return_value = AIMessage(content="technical")
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)

    graph = build_graph({name: _mock_tool(name) for name in REPORT_TOOLS})
    graph.invoke({"ticker": "NVDA", "question": "What's NVDA's RSI?"})

    # quick_fact runs the plan-LLM call (raw .invoke) to narrow the report
    # set, then a structured synthesize call. Both must land for the
    # narrow-on-quick-fact contract.
    assert llm.invoke.call_count == 1
    assert llm._structured_runnable.invoke.call_count == 1


def test_parse_plan_force_includes_company_for_comparison() -> None:
    """QNT-175: comparison intent shares the thesis bias — the static profile
    grounds qualitative contrasts (segment mix, competitive overlap)."""
    plan = _parse_plan(
        "fundamental",
        ["company", "technical", "fundamental", "news"],
        intent="comparison",
    )
    assert "company" in plan
    assert plan == ["company", "fundamental"]


def test_confidence_full_coverage() -> None:
    assert _confidence_from_reports({"a": "", "b": "", "c": ""}, ["a", "b", "c"]) == 1.0


def test_confidence_ignores_tool_error_reports() -> None:
    assert _confidence_from_reports({"a": "[error] http-500: unavailable"}, ["a"]) == 0.0


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
    # QNT-175 added ``company`` (static business profile) at the head of the tuple.
    assert REPORT_TOOLS == ("company", "technical", "fundamental", "news")


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

    # QNT-212: the question names no ticker AND there's no prior turn, so
    # the ambiguity detector would short-circuit to clarify. Anchor the
    # question with a ticker so the quick-fact path actually runs.
    result = graph.invoke({"ticker": "NVDA", "question": "What's NVDA's RSI right now?"})

    assert result["intent"] == "quick_fact"
    assert isinstance(result["quick_fact"], QuickFactAnswer)
    assert result["thesis"] is None
    # Confidence still reflects coverage so the panel can show a bar.
    assert result["confidence"] == 1.0


def test_question_named_ticker_rebases_quick_fact_run(
    stub_llm: _StructuredLLM,
) -> None:
    """A single question-named ticker beats the URL-context ticker."""
    stub_llm.invoke.return_value = AIMessage(content="fundamental")
    quick = QuickFactAnswer(
        answer="AAPL trades at 31x earnings (source: fundamental).",
        cited_value="31",
        source="fundamental",
    )
    stub_llm.structured_invoke.return_value = quick
    fundamental = MagicMock(return_value="## fundamental\nAAPL P/E 31\n")
    graph = build_graph({"fundamental": fundamental})

    result = graph.invoke({"ticker": "NVDA", "question": "What's AAPL's P/E?"})

    assert result["ticker"] == "AAPL"
    assert result["analysis_ticker"] == "AAPL"
    fundamental.assert_called_once_with("AAPL")
    assert result["intent"] == "quick_fact"
    assert isinstance(result["quick_fact"], QuickFactAnswer)


def test_question_named_ticker_rebases_thesis_run(
    stub_llm: _StructuredLLM,
) -> None:
    company = MagicMock(return_value="## company\nAAPL business\n")
    technical = MagicMock(return_value="## technical\nAAPL RSI 55\n")
    fundamental = MagicMock(return_value="## fundamental\nAAPL P/E 31\n")
    news = MagicMock(return_value="## news\nAAPL headline\n")
    graph = build_graph(
        {
            "company": company,
            "technical": technical,
            "fundamental": fundamental,
            "news": news,
        }
    )

    result = graph.invoke({"ticker": "NVDA", "question": "Give me an AAPL thesis."})

    assert result["ticker"] == "AAPL"
    assert result["analysis_ticker"] == "AAPL"
    for tool in (company, technical, fundamental, news):
        tool.assert_called_once_with("AAPL")
    assert isinstance(result["thesis"], Thesis)


def test_quick_fact_failure_surfaces_as_none_quick_fact(
    stub_llm: _StructuredLLM,
) -> None:
    """A misbehaving provider on the quick-fact path must surface as
    ``quick_fact=None``, not crash the graph — same defense as the
    QNT-133 thesis path."""
    stub_llm.invoke.return_value = AIMessage(content="technical")
    stub_llm.structured_invoke.side_effect = RuntimeError("schema-validation")
    graph = build_graph({"technical": _mock_tool("tech")})

    # QNT-212: ticker named in question so the ambiguity detector doesn't
    # short-circuit to clarify before synthesize gets a chance to fail.
    result = graph.invoke({"ticker": "NVDA", "question": "What's NVDA's P/E?"})

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
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("thesis", "fallback", False, False, ""),
    )
    graph = build_graph({"technical": _mock_tool("tech")})

    # QNT-212: anchor with a ticker so the ambiguity detector doesn't route
    # us to clarify before synthesize runs (the safe-default-to-thesis
    # invariant lives in classify, which still fires the way the test asserts).
    result = graph.invoke(
        {"ticker": "NVDA", "question": "ambiguous mid-length question about NVDA"}
    )

    assert result["intent"] == "thesis"
    assert isinstance(result["thesis"], Thesis)


def test_quick_fact_intent_narrows_plan_prompt(
    stub_llm: _StructuredLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan prompt for quick_fact bias should tell the LLM to fetch only
    the needed report. Direct check on the planner-prompt builder rather
    than asserting the LLM's choice (which is up to the model)."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("quick_fact", "heuristic", False, False, ""),
    )
    stub_llm.invoke.return_value = AIMessage(content="technical")
    graph = build_graph({"technical": _mock_tool("tech")})

    # QNT-212: name the ticker so the ambiguity detector doesn't divert
    # the run to clarify before plan_node ever calls the LLM.
    graph.invoke({"ticker": "NVDA", "question": "What's NVDA's RSI?"})

    # The plan path uses raw .invoke() (not structured_output), so the
    # captured prompt sits on the plan-call args of the stub.
    assert stub_llm.invoke.call_count >= 1, "plan node must call the LLM"
    plan_call = stub_llm.invoke.call_args_list[0]
    plan_prompt = (
        str(plan_call.args[0]) if plan_call.args else str(plan_call.kwargs.get("input", ""))
    )
    assert "single-metric" in plan_prompt.lower() or "directly needed" in plan_prompt.lower()


# ─── QNT-156: comparison + conversational + domain-redirect fallback ─────


def test_classify_node_routes_to_comparison_for_two_ticker_question(
    stub_llm: _StructuredLLM,
) -> None:
    """A multi-ticker comparison ask trips the heuristic to ``comparison``.
    The graph fetches reports for each named ticker and synthesize returns
    a ComparisonAnswer. AC: comparison response shape returns per-ticker
    sections + a differences paragraph."""
    from agent.comparison import ComparisonAnswer

    from ._thesis_factory import make_comparison_section

    stub_llm.invoke.return_value = AIMessage(content="fundamental")
    expected = ComparisonAnswer(
        sections=[
            make_comparison_section("NVDA", "Premium", "Uptrend"),
            make_comparison_section("AAPL", "Inline", "Sideways"),
        ],
        differences="NVDA carries a richer multiple than AAPL (source: fundamental).",
    )
    stub_llm.structured_invoke.return_value = expected
    graph = build_graph({"fundamental": _mock_tool("fund")})

    result = graph.invoke({"ticker": "NVDA", "question": "Compare NVDA vs AAPL on valuation."})

    assert result["intent"] == "comparison"
    assert isinstance(result["comparison"], ComparisonAnswer)
    assert [s.ticker for s in result["comparison"].sections] == ["NVDA", "AAPL"]
    assert result["thesis"] is None
    assert result["quick_fact"] is None
    # Reports were gathered for BOTH tickers.
    assert set(result["reports_by_ticker"].keys()) == {"NVDA", "AAPL"}


def test_comparison_with_only_one_resolved_ticker_routes_to_clarify(
    stub_llm: _StructuredLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QNT-212 update: a comparison ask with only one named ticker is now
    caught upstream by the ambiguity detector and routed to clarify --
    the user is asked which second ticker to compare against, instead of
    getting the pre-QNT-212 synthesize-fallback redirect.

    The synthesize comparison-fallback branch still exists (defensive in
    case a comparison slips past classify), but the live path is clarify.
    """
    from agent.conversational import ConversationalAnswer

    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("comparison", "heuristic", False, False, ""),
    )
    stub_llm.invoke.return_value = AIMessage(content="fundamental")
    # Clarify's with_structured_output(ConversationalAnswer) call resolves
    # against the same structured_invoke stub. Return a question shape.
    stub_llm.structured_invoke.return_value = ConversationalAnswer(
        answer="Which second ticker should I compare against?",
        suggestions=["Compare NVDA vs AAPL", "Compare NVDA vs MSFT"],
    )
    graph = build_graph({"fundamental": _mock_tool("fund")})

    # Only one ticker (NVDA) — not enough for a comparison.
    result = graph.invoke({"ticker": "NVDA", "question": "Compare NVDA against the broader market"})

    assert result["intent"] == "comparison"
    assert result.get("ambiguity_kind") == "needs_second_ticker"
    # Clarify never visits synthesize, so comparison is never written --
    # absent or None both satisfy "no comparison payload landed".
    assert result.get("comparison") is None
    assert isinstance(result["conversational"], ConversationalAnswer)
    # Clarify skips plan + gather + synthesize entirely.
    assert result["intent_path"] == ["classify", "clarify", "narrate"]


def test_classify_node_routes_to_conversational_for_off_domain_ask(
    stub_llm: _StructuredLLM,
) -> None:
    """An off-domain question routes to ``conversational``. The synthesize
    node calls the LLM (not the deterministic redirect path) and emits
    a ConversationalAnswer with a redirect + suggestions."""
    from agent.conversational import ConversationalAnswer

    stub_llm.invoke.return_value = AIMessage(content="technical")
    expected = ConversationalAnswer(
        answer="I don't know about the weather — I cover US equities.",
        suggestions=[
            "What's NVDA's RSI right now?",
            "How is MSFT valued relative to its earnings?",
            "Should I be cautious about META?",
        ],
    )
    stub_llm.structured_invoke.return_value = expected
    graph = build_graph({"technical": _mock_tool("tech")})

    result = graph.invoke({"ticker": "NVDA", "question": "What's the weather like today?"})

    assert result["intent"] == "conversational"
    assert isinstance(result["conversational"], ConversationalAnswer)
    assert result["thesis"] is None
    assert result["comparison"] is None
    assert result["quick_fact"] is None
    # QNT-212: conversational now short-circuits classify→synthesize, so
    # plan/gather never run and ``reports`` is never written by the graph.
    # ``get`` defaults to {} for the same "no tool calls fired" assertion.
    assert result.get("reports", {}) == {}
    assert result["intent_path"] == ["classify", "synthesize", "narrate"]


def test_conversational_failure_falls_back_to_deterministic_redirect(
    stub_llm: _StructuredLLM,
) -> None:
    """If the conversational LLM call fails, the synthesize node still emits
    a ConversationalAnswer — built deterministically via ``domain_redirect``
    — so the panel never sees an empty state."""
    from agent.conversational import ConversationalAnswer

    stub_llm.invoke.return_value = AIMessage(content="technical")
    stub_llm.structured_invoke.side_effect = RuntimeError("schema-validation")
    graph = build_graph({"technical": _mock_tool("tech")})

    # "what can you do?" is heuristic-classified as conversational without
    # an LLM call (multi-word phrase in _CONVERSATIONAL_TOKENS) — the test
    # would otherwise depend on whether the local LiteLLM proxy is reachable
    # because intent.classify_intent has its own get_llm code path. CI has
    # no proxy, so a heuristic-only question keeps the test deterministic.
    result = graph.invoke({"ticker": "NVDA", "question": "what can you do?"})

    assert result["intent"] == "conversational"
    assert isinstance(result["conversational"], ConversationalAnswer)
    # Deterministic redirect mentions covered tickers + suggestions.
    answer = result["conversational"].answer
    assert any(t in answer for t in ("NVDA", "AAPL", "MSFT"))
    assert len(result["conversational"].suggestions) == 3


def test_thesis_synthesis_failure_falls_back_to_conversational_redirect(
    stub_llm: _StructuredLLM,
) -> None:
    """QNT-156: structured-output crash on the thesis path no longer leaves
    state['thesis']=None with no replacement — synthesize emits the
    deterministic conversational redirect."""
    from agent.conversational import ConversationalAnswer

    stub_llm.invoke.return_value = AIMessage(content="technical")
    stub_llm.structured_invoke.side_effect = RuntimeError("schema-validation")
    graph = build_graph({"technical": _mock_tool("tech")})

    # Default question is thesis-shaped.
    result = _run(graph)

    assert result["intent"] == "thesis"
    assert result["thesis"] is None
    assert isinstance(result["conversational"], ConversationalAnswer)


def test_comparison_skips_when_one_ticker_has_no_reports(
    stub_llm: _StructuredLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A comparison run where one ticker fails to gather any reports must
    NOT produce a half-comparison — it falls back to a redirect."""
    from agent.conversational import ConversationalAnswer

    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("comparison", "heuristic", False, False, ""),
    )
    stub_llm.invoke.return_value = AIMessage(content="fundamental")

    def aapl_only(ticker: str) -> str:
        if ticker == "AAPL":
            return f"fund for {ticker}"
        raise RuntimeError("nvda fundamentals down")

    graph = build_graph({"fundamental": aapl_only})
    result = graph.invoke({"ticker": "NVDA", "question": "Compare NVDA vs AAPL."})

    assert result["intent"] == "comparison"
    assert result["comparison"] is None
    assert isinstance(result["conversational"], ConversationalAnswer)
    # Structured runnable was NEVER invoked — fallback is deterministic.
    assert stub_llm.structured_invoke.call_count == 0


def test_conversational_intent_skips_plan_and_gather_llm_calls(
    stub_llm: _StructuredLLM,
) -> None:
    """The conversational path must NOT call the plan LLM — there's
    nothing to plan when no tools will fire. Only synthesize fires its
    structured runnable."""
    from agent.conversational import ConversationalAnswer

    stub_llm.invoke.return_value = AIMessage(content="should-not-fire")
    stub_llm.structured_invoke.return_value = ConversationalAnswer(
        answer="hi there", suggestions=[]
    )
    graph = build_graph({"technical": _mock_tool("tech")})

    graph.invoke({"ticker": "NVDA", "question": "hi"})

    # Plan LLM call (raw invoke) must NOT have fired.
    assert stub_llm.invoke.call_count == 0
    # Synthesize structured runnable fires once.
    assert stub_llm.structured_invoke.call_count == 1


# ─── QNT-159: classify_node emits intent via event_emitter ───────────────


def test_classify_node_calls_event_emitter_with_intent_decision(
    stub_llm: _StructuredLLM,
) -> None:
    """QNT-159: classify_node MUST call the event_emitter (when supplied)
    with the resolved intent BEFORE plan/gather/synthesize run. The SSE
    wrapper relies on this to surface the routing decision before the
    first tool_call frame lands; without it, the panel's streaming label
    flickers "streaming thesis…" for the entire tool-gathering phase
    regardless of which intent the classifier picked.

    Reviewer-flagged regression guard (QNT-159 review): the SSE-side
    ordering test in tests/api/test_agent_chat.py uses a stubbed
    build_graph that calls event_emitter itself, so a regression where
    the real classify_node stops calling the emitter would leave that
    test green. This test pins the production code path directly."""
    stub_llm.invoke.return_value = AIMessage(content="technical")
    emitted: list[tuple[str, dict[str, object]]] = []

    def emit(event: str, data: dict[str, object]) -> None:
        emitted.append((event, dict(data)))

    graph = build_graph({"technical": _mock_tool("tech")}, event_emitter=emit)
    # Default question heuristic-classifies as thesis (no LLM classifier call).
    _run(graph)

    # The emitter MUST have been called with an intent event during classify.
    intent_calls = [(e, d) for e, d in emitted if e == "intent"]
    assert intent_calls, f"classify_node must emit ('intent', ...); got {emitted}"
    assert intent_calls[0][1] == {"intent": "thesis"}


def test_classify_node_swallows_event_emitter_exceptions(
    stub_llm: _StructuredLLM,
) -> None:
    """QNT-159: a misbehaving event_emitter (SSE wrapper bug, closed event
    loop on client disconnect, etc.) MUST NOT crash the graph. The
    classify_node wraps the emit call in BLE001 so the safety-net
    post-graph intent yield in agent_chat.py still gets a chance to
    fire."""
    stub_llm.invoke.return_value = AIMessage(content="technical")

    def broken_emit(_event: str, _data: dict[str, object]) -> None:
        raise RuntimeError("Event loop is closed")

    graph = build_graph({"technical": _mock_tool("tech")}, event_emitter=broken_emit)
    # Must not raise — graph completes despite the broken emitter.
    result = _run(graph)
    assert result["intent"] == "thesis"


def test_build_graph_without_event_emitter_remains_no_op(
    stub_llm: _StructuredLLM,
) -> None:
    """QNT-159 backwards compat: callers that don't supply an event_emitter
    (CLI, eval harness, all existing tests) must see no behavior change.
    Default is None, which means no emit call fires from classify_node."""
    stub_llm.invoke.return_value = AIMessage(content="technical")
    # No event_emitter kwarg.
    graph = build_graph({"technical": _mock_tool("tech")})
    result = _run(graph)
    assert result["intent"] == "thesis"
    assert isinstance(result["thesis"], Thesis)


def test_conversational_answer_rejects_digits_in_answer() -> None:
    """QNT-156 guardrail: ``ConversationalAnswer.has_numeric_claims`` flags
    any digit so the hallucination scorer can treat numbers in
    conversational answers as regressions (the path is supposed to stay
    vibes-only — there are no reports to cite)."""
    from agent.conversational import ConversationalAnswer

    ok = ConversationalAnswer(answer="I cover ten US equities.")
    flagged = ConversationalAnswer(answer="I cover 10 US equities.")
    assert not ok.has_numeric_claims()
    assert flagged.has_numeric_claims()


def test_domain_redirect_rejects_digit_in_reason() -> None:
    """Regression (review finding): ``domain_redirect.reason`` is
    interpolated into the user-visible answer, so a caller passing a
    string with a digit (HTTP status code, retry count, year) would
    silently produce a payload that immediately fails the hallucination
    eval. Guard at the boundary so the bug fires loudly at construction
    time instead."""
    import pytest as _pytest
    from agent.conversational import domain_redirect
    from shared.tickers import TICKERS

    with _pytest.raises(ValueError, match="must not contain digits"):
        domain_redirect(
            reason="I had trouble after 3 retries.",
            tickers=TICKERS,
        )

    # Clean reason still works.
    redirect = domain_redirect(
        reason="I couldn't pull a thesis right now.",
        tickers=TICKERS,
    )
    assert not redirect.has_numeric_claims()


def test_hint_from_intent_quick_fact_resolves_to_a_real_bank_label() -> None:
    """Regression (review finding): ``_hint_from_intent`` used to return
    ``"quick_fact"`` which is NOT a label in
    :data:`agent.conversational._SUGGESTION_BANK`, silently degrading
    quick-fact-failure redirects to the unbiased default. The hint MUST
    resolve to a label that actually exists in the bank."""
    from agent.conversational import _SUGGESTION_BANK
    from agent.graph import _hint_from_intent

    bank_labels = {label for label, _ in _SUGGESTION_BANK}

    # Every non-conversational intent must produce a hint that actually
    # appears in the bank — otherwise _pick_suggestions silently falls
    # through to the no-hint default and the bias contract is broken.
    for intent in ("thesis", "quick_fact", "comparison"):
        hint = _hint_from_intent(intent)  # type: ignore[arg-type]
        assert hint is not None, f"intent {intent!r} produced None hint"
        assert hint in bank_labels, (
            f"intent {intent!r} hint {hint!r} not in suggestion bank labels {bank_labels}"
        )

    # Conversational does not invoke the redirect (it IS the redirect).
    assert _hint_from_intent("conversational") is None


# ─── QNT-176: focused-analysis intents ──────────────────────────────────────


def test_focused_intent_narrows_plan_to_company_and_matching_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each focused intent must narrow the plan to ``[company, <report>]``
    deterministically — no plan-LLM call (the user named the domain
    explicitly; nothing to disambiguate)."""
    from agent.focused import FocusedAnalysis
    from agent.intent import Intent

    cases: list[tuple[Intent, str, str]] = [
        ("fundamental", "give me a fundamental analysis of NVDA", "fundamental"),
        ("technical", "technical analysis of NVDA", "technical"),
        ("news", "what is the sentiment on NVDA?", "news"),
    ]

    for intent, question, expected_report in cases:
        llm = _StructuredLLM()
        # Synthesize stub returns a FocusedAnalysis matching the focus.
        llm._structured_runnable.invoke = MagicMock(
            return_value=FocusedAnalysis(
                focus=intent,  # type: ignore[arg-type]
                summary=f"focused {intent} summary",
                key_points=[],
                cited_values=[],
            ),
        )

        monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)
        from agent import intent as intent_module

        monkeypatch.setattr(intent_module, "get_llm", lambda *_a, **_kw: llm)

        graph = build_graph({name: _mock_tool(name) for name in REPORT_TOOLS})
        result = graph.invoke({"ticker": "NVDA", "question": question})

        # No plan-LLM call: focused intents narrow deterministically to
        # ``["company", <matching_report>]`` without consulting the planner.
        assert llm.invoke.call_count == 0, f"unexpected plan call for intent={intent}"
        # Plan is exactly [company, <report>].
        assert result["plan"] == ["company", expected_report], (
            f"plan for intent={intent} was {result['plan']!r}"
        )
        # Reports gathered match the plan.
        assert set(result["reports"]) == {"company", expected_report}
        # Focused payload populated; thesis/quick_fact/comparison are None.
        assert isinstance(result["focused"], FocusedAnalysis)
        assert result["focused"].focus == intent
        assert result["thesis"] is None
        assert result["quick_fact"] is None
        assert result["comparison"] is None


def test_question_named_ticker_rebases_focused_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.focused import FocusedAnalysis

    llm = _StructuredLLM()
    llm._structured_runnable.invoke = MagicMock(
        return_value=FocusedAnalysis(
            focus="technical",
            summary="AAPL technical summary",
            key_points=[],
            cited_values=[],
        )
    )
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)
    from agent import intent as intent_module

    monkeypatch.setattr(intent_module, "get_llm", lambda *_a, **_kw: llm)

    company = MagicMock(return_value="## company\nAAPL business\n")
    technical = MagicMock(return_value="## technical\nAAPL RSI 55\n")
    graph = build_graph({"company": company, "technical": technical})

    result = graph.invoke({"ticker": "NVDA", "question": "technical analysis of AAPL"})

    assert result["ticker"] == "AAPL"
    assert result["analysis_ticker"] == "AAPL"
    company.assert_called_once_with("AAPL")
    technical.assert_called_once_with("AAPL")
    assert isinstance(result["focused"], FocusedAnalysis)


def test_focused_intent_falls_back_to_redirect_when_reports_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the matching report can't be fetched (and ``company`` is also
    absent), the focused branch falls back to a deterministic conversational
    redirect — same contract as every other synthesize path."""
    from agent.conversational import ConversationalAnswer

    llm = _StructuredLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)
    from agent import intent as intent_module

    monkeypatch.setattr(intent_module, "get_llm", lambda *_a, **_kw: llm)

    # No tools registered — gather will return empty reports.
    graph = build_graph({})
    result = graph.invoke({"ticker": "NVDA", "question": "give me a fundamental analysis of NVDA"})

    assert result["focused"] is None
    assert isinstance(result["conversational"], ConversationalAnswer)


def test_focused_intent_requires_matching_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Company context alone is not enough for a focused analysis card."""
    from agent.conversational import ConversationalAnswer

    llm = _StructuredLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)
    from agent import intent as intent_module

    monkeypatch.setattr(intent_module, "get_llm", lambda *_a, **_kw: llm)

    graph = build_graph({"company": _mock_tool("company")})
    result = graph.invoke({"ticker": "NVDA", "question": "give me a fundamental analysis of NVDA"})

    assert result["focused"] is None
    assert isinstance(result["conversational"], ConversationalAnswer)
    assert llm.structured_invoke.call_count == 0


def test_focused_intent_corrects_mismatched_focus_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the LLM hallucinates a different ``focus`` literal than the user
    asked for, the synthesize node re-asserts the value from intent so the
    UI card label matches the request."""
    from agent.focused import FocusedAnalysis

    llm = _StructuredLLM()
    # User asked for technical; LLM emitted fundamental.
    llm._structured_runnable.invoke = MagicMock(
        return_value=FocusedAnalysis(
            focus="fundamental",
            summary="summary",
            key_points=[],
            cited_values=[],
        ),
    )
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)
    from agent import intent as intent_module

    monkeypatch.setattr(intent_module, "get_llm", lambda *_a, **_kw: llm)

    graph = build_graph({name: _mock_tool(name) for name in REPORT_TOOLS})
    result = graph.invoke({"ticker": "NVDA", "question": "technical analysis of NVDA"})

    assert isinstance(result["focused"], FocusedAnalysis)
    assert result["focused"].focus == "technical"


def test_hint_from_intent_focused_resolves_to_real_bank_label() -> None:
    """The QNT-176 focused intents must produce hints that exist in the
    suggestion bank — same regression guardrail as the QNT-149 finding."""
    from agent.conversational import _SUGGESTION_BANK
    from agent.graph import _hint_from_intent

    bank_labels = {label for label, _ in _SUGGESTION_BANK}
    for intent in ("fundamental", "technical", "news"):
        hint = _hint_from_intent(intent)  # type: ignore[arg-type]
        assert hint is not None, f"intent {intent!r} produced None hint"
        assert hint in bank_labels, f"intent {intent!r} hint {hint!r} not in {bank_labels}"


# ─── QNT-222: semantic news search (RAG) wiring ──────────────────────────────


def _news_focused_llm() -> _StructuredLLM:
    """An ``_StructuredLLM`` whose structured channel returns a news focused card."""
    from agent.focused import FocusedAnalysis

    llm = _StructuredLLM()
    llm._structured_runnable.invoke = MagicMock(
        return_value=FocusedAnalysis(
            focus="news",
            summary="news summary",
            key_points=[],
            cited_values=[],
            existing_development="running story",
            positive_catalysts=[],
            negative_catalysts=[],
        ),
    )
    return llm


def _recording_search_news(rows: list[dict[str, str]]) -> MagicMock:
    """A 2-arg search_news stub that records (ticker, query) calls."""
    return MagicMock(return_value=json.dumps(rows))


@pytest.mark.parametrize(
    "intent",
    ["news", "quick_fact", "thesis"],
)
def test_needs_news_search_routes_through_search_news_across_intents(
    monkeypatch: pytest.MonkeyPatch, intent: str
) -> None:
    """The classifier's ``needs_news_search`` flag fires the semantic search on
    EVERY news-consuming intent (news / quick_fact / thesis) -- not just a
    literal news intent -- and the retrieved headlines are folded into the news
    report with the question passed verbatim as the query."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: (intent, "llm", True, False, ""),
    )
    llm = _news_focused_llm()
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)

    question = "what did the CEO say about the buyback on NVDA?"
    search = _recording_search_news(
        [{"headline": "NVDA sued over chip patents", "source": "Reuters", "date": "2026-06-01"}]
    )
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_news_tool=search,
    )
    result = graph.invoke({"ticker": "NVDA", "question": question})

    # Called exactly once with the ticker and the verbatim question as query.
    search.assert_called_once_with("NVDA", question)
    # Retrieved headline is folded into the news report the synthesis reads.
    assert "NVDA sued over chip patents" in result["reports"]["news"]


def test_search_query_rewrite_is_used_over_the_raw_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-289: when classify_node resolves a non-empty search_query (the
    warm-thread rewrite), gather queries search_news with the REWRITE, not the
    bare/elliptical raw question."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("quick_fact", "llm", True, False, "NVDA buyback"),
    )
    llm = _news_focused_llm()
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)

    question = "what about the buyback?"
    search = _recording_search_news(
        [{"headline": "NVDA announces buyback", "source": "Reuters", "date": "2026-06-01"}]
    )
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_news_tool=search,
    )
    # Elliptical, tickerless follow-up -- needs prior-turn context (a seeded
    # report) so classify_node's ambiguity gate doesn't route to clarify
    # instead of gather.
    graph.invoke(
        {
            "ticker": "NVDA",
            "question": question,
            "reports": {"news": "## news\nprior digest\n"},
        }
    )

    search.assert_called_once_with("NVDA", "NVDA buyback")


def test_empty_search_query_falls_back_to_raw_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-289: an empty/rejected rewrite falls back to the raw question --
    the recall floor. This is the existing behaviour and must not regress."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("quick_fact", "llm", True, False, ""),
    )
    llm = _news_focused_llm()
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)

    question = "what did the CEO say about the buyback on NVDA?"
    search = _recording_search_news(
        [{"headline": "NVDA sued over chip patents", "source": "Reuters", "date": "2026-06-01"}]
    )
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_news_tool=search,
    )
    graph.invoke({"ticker": "NVDA", "question": question})

    search.assert_called_once_with("NVDA", question)


def test_generic_news_ask_does_not_call_search_news(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic news ask (classifier flag False) keeps the cheap canned report
    and never fires the semantic search path."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("news", "llm", False, False, ""),
    )
    llm = _news_focused_llm()
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)

    search = _recording_search_news([{"headline": "x", "source": "y", "date": "z"}])
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_news_tool=search,
    )
    result = graph.invoke({"ticker": "NVDA", "question": "what's the news on NVDA?"})

    search.assert_not_called()
    # Canned mock report is used untouched.
    assert result["reports"]["news"] == "news for NVDA"


def test_targeted_news_with_empty_hits_keeps_canned_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flagged ask whose search returns no matches leaves the canned news
    digest intact rather than blanking it."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("news", "llm", True, False, ""),
    )
    llm = _news_focused_llm()
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)

    search = MagicMock(return_value="[]")
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_news_tool=search,
    )
    result = graph.invoke({"ticker": "NVDA", "question": "any litigation news on NVDA?"})

    search.assert_called_once()
    assert result["reports"]["news"] == "news for NVDA"


def test_needs_news_search_skipped_for_focused_fundamental_intent(
    monkeypatch: pytest.MonkeyPatch, stub_llm: _StructuredLLM
) -> None:
    """Even with the flag set, a fundamental/technical focused read does not
    fire the search: those focuses are forbidden from citing news, so the fetch
    would be wasted (gate scoped to _NEWS_SEARCH_INTENTS)."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("fundamental", "llm", True, False, ""),
    )
    search = MagicMock(return_value="[]")
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_news_tool=search,
    )
    graph.invoke({"ticker": "NVDA", "question": "is NVDA expensive given the lawsuit?"})

    search.assert_not_called()


def test_flag_false_never_calls_search_even_on_news_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag is the sole trigger: a news intent with needs_news_search False
    stays on the canned digest."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("news", "llm", False, False, ""),
    )
    llm = _news_focused_llm()
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)

    search = MagicMock(return_value="[]")
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_news_tool=search,
    )
    graph.invoke({"ticker": "NVDA", "question": "any litigation news on NVDA?"})

    search.assert_not_called()


def test_targeted_news_drops_focused_card_and_surfaces_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-226 AC3: a targeted news ask (needs_news_search True) WITH retrieved
    hits takes the narrative-only shape -- synthesize sets focused=None and skips
    the focused-card LLM call, narrate owns the spoken answer, and the retrieved
    hits are surfaced as ``retrieved_sources`` for the frontend provenance list."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("news", "llm", True, False, ""),
    )
    # Structured channel returns a focused card -- it must NOT be consumed on
    # the narrative-only path (the assertion below pins that the call is skipped).
    llm = _news_focused_llm()
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)

    search = _recording_search_news(
        [
            {
                "headline": "NVDA strikes Micron HBM4 supply deal",
                "source": "Reuters",
                "date": "2026-06-01",
                "url": "https://ex.com/a",
            }
        ]
    )
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_news_tool=search,
    )
    result = graph.invoke({"ticker": "NVDA", "question": "any news on NVDA and the Micron deal?"})

    # Focused card dropped; the focused-card LLM call was never made.
    assert result["focused"] is None
    llm.structured_invoke.assert_not_called()
    # Retrieved hits surfaced as structured provenance.
    sources = result["retrieved_sources"]
    assert sources == [
        {
            "headline": "NVDA strikes Micron HBM4 supply deal",
            "source": "Reuters",
            "date": "2026-06-01",
            "url": "https://ex.com/a",
            # QNT-263: provenance carries the corpus tag.
            "corpus": "news",
        }
    ]
    # And folded into the news report the narrator speaks from.
    assert "NVDA strikes Micron HBM4 supply deal" in result["reports"]["news"]


def test_broad_news_keeps_focused_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """QNT-226 AC3: a broad news read (needs_news_search False) keeps the full
    focused-news card and surfaces no retrieved sources."""
    from agent.focused import FocusedAnalysis

    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("news", "llm", False, False, ""),
    )
    llm = _news_focused_llm()
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)

    search = MagicMock(return_value="[]")
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_news_tool=search,
    )
    result = graph.invoke({"ticker": "NVDA", "question": "give me a news read on NVDA"})

    search.assert_not_called()
    assert isinstance(result["focused"], FocusedAnalysis)
    llm.structured_invoke.assert_called_once()
    assert not result.get("retrieved_sources")


def test_targeted_news_empty_hits_keeps_focused_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """QNT-226 AC3 safety net: when the flag is set but search returns NO hits
    (Qdrant down / no matches), the narrative-only shape is NOT taken -- the full
    focused card renders so the canned digest still has a structured surface and
    the run can never go blank."""
    from agent.focused import FocusedAnalysis

    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("news", "llm", True, False, ""),
    )
    llm = _news_focused_llm()
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)

    search = MagicMock(return_value="[]")
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_news_tool=search,
    )
    result = graph.invoke({"ticker": "NVDA", "question": "any litigation news on NVDA?"})

    search.assert_called_once()
    assert isinstance(result["focused"], FocusedAnalysis)
    assert not result.get("retrieved_sources")


def test_parse_search_sources_extracts_fields_and_degrades() -> None:
    """QNT-226: structured provenance parse keeps headline/source/date/url and
    degrades to [] on every bad-payload path (mirrors _format_search_hits)."""
    raw = json.dumps(
        [
            {
                "headline": "AAPL antitrust probe",
                "source": "Bloomberg",
                "date": "2026-05-02",
                "url": "https://ex.com/x",
                "score": 0.6,
                "body": "ignored",
            },
            {"headline": "", "source": "WSJ"},  # no headline -> skipped
        ]
    )
    sources = graph_module._parse_search_sources(raw)
    assert sources == [
        {
            "headline": "AAPL antitrust probe",
            "source": "Bloomberg",
            "date": "2026-05-02",
            "url": "https://ex.com/x",
            # QNT-263: provenance carries the corpus so the frontend distinguishes
            # a news hit from an earnings-release hit.
            "corpus": "news",
        }
    ]
    assert graph_module._parse_search_sources("[]") == []
    assert graph_module._parse_search_sources("not json") == []
    assert graph_module._parse_search_sources("[1, 2, 3]") == []


# ─── QNT-263: multi-corpus routing (news + 8-K earnings) ─────────────────────


def _earnings_rows(*, n: int = 1) -> str:
    """A search_earnings stub payload: earnings-chunk display rows."""
    return json.dumps(
        [
            {
                "title": "NVDA Q1 FY26 earnings release",
                "section": "guidance",
                "date": "2026-05-28",
                "url": "https://sec.gov/ex99-1",
                "text": "Management guided Q2 revenue to a record on data-center demand.",
                "score": 0.7,
            }
            for _ in range(n)
        ]
    )


def test_needs_earnings_search_routes_through_search_earnings(
    monkeypatch: pytest.MonkeyPatch, stub_llm: _StructuredLLM
) -> None:
    """QNT-263 AC1/AC2: an earnings-narrative ask (deterministic
    needs_earnings_search) on a fundamental-report-consuming intent fires the
    earnings search, folds the release excerpt into the fundamental report, and
    surfaces corpus-tagged provenance."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("thesis", "llm", False, True, ""),
    )
    # Names the ticker so the ask reaches gather (a tickerless analysis ask
    # routes to clarify, like the other search tests).
    question = "what did NVDA management say about guidance and the outlook?"
    search_earnings = MagicMock(return_value=_earnings_rows())
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_earnings_tool=search_earnings,
    )
    result = graph.invoke({"ticker": "NVDA", "question": question})

    # Fired once with the ticker + verbatim question as the query.
    search_earnings.assert_called_once_with("NVDA", question)
    # Folded into the fundamental report the thesis synthesis reads.
    assert "Management guided Q2 revenue" in result["reports"]["fundamental"]
    # Provenance distinguishes the corpus (AC2).
    sources = result["retrieved_sources"]
    assert sources and all(s["corpus"] == "earnings" for s in sources)
    assert sources[0]["headline"] == "NVDA Q1 FY26 earnings release"


def test_quick_fact_earnings_ask_routes_through_search_earnings(
    monkeypatch: pytest.MonkeyPatch, stub_llm: _StructuredLLM
) -> None:
    """QNT-263 follow-up: the natural single-fact earnings phrasing classifies as
    quick_fact, which is now in _EARNINGS_SEARCH_INTENTS -- so it reaches the 8-K
    corpus (build_quick_fact_prompt renders the fundamental report it folds into)
    instead of only the news headlines, mirroring quick_fact in the news gate."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("quick_fact", "llm", False, True, ""),
    )
    question = "what did NVDA management say about guidance in the latest earnings?"
    search_earnings = MagicMock(return_value=_earnings_rows())
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_earnings_tool=search_earnings,
    )
    result = graph.invoke({"ticker": "NVDA", "question": question})

    search_earnings.assert_called_once_with("NVDA", question)
    assert "Management guided Q2 revenue" in result["reports"]["fundamental"]
    assert all(s["corpus"] == "earnings" for s in result["retrieved_sources"])


def test_earnings_search_skipped_for_non_consuming_intent(
    monkeypatch: pytest.MonkeyPatch, stub_llm: _StructuredLLM
) -> None:
    """A technical focused read does not gather the fundamental report, so even
    with the earnings flag set the search must not fire (gate scoped to
    _EARNINGS_SEARCH_INTENTS)."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("technical", "llm", False, True, ""),
    )
    search_earnings = MagicMock(return_value=_earnings_rows())
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_earnings_tool=search_earnings,
    )
    # Names the ticker so it reaches gather -- the assertion then proves the
    # INTENT gate (technical not in _EARNINGS_SEARCH_INTENTS) skipped the search,
    # not that an earlier clarify gate did.
    graph.invoke({"ticker": "NVDA", "question": "what did NVDA management say about guidance?"})

    search_earnings.assert_not_called()


def test_both_corpora_route_and_tag_distinct_provenance(
    monkeypatch: pytest.MonkeyPatch, stub_llm: _StructuredLLM
) -> None:
    """QNT-263: a query spanning a named news event AND an earnings ask reaches
    BOTH corpora, and the merged provenance keeps each hit's corpus tag."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("thesis", "llm", True, True, ""),
    )
    question = "what did NVDA's CEO say about guidance?"
    search_news = _recording_search_news(
        [{"headline": "NVDA CEO comments at conference", "source": "Reuters", "date": "2026-06-01"}]
    )
    search_earnings = MagicMock(return_value=_earnings_rows())
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_news_tool=search_news,
        search_earnings_tool=search_earnings,
    )
    result = graph.invoke({"ticker": "NVDA", "question": question})

    search_news.assert_called_once_with("NVDA", question)
    search_earnings.assert_called_once_with("NVDA", question)
    corpora = {s["corpus"] for s in result["retrieved_sources"]}
    assert corpora == {"news", "earnings"}


def test_format_earnings_hits_and_parse_sources_degrade() -> None:
    """QNT-263: earnings render + provenance parse mirror the news helpers and
    degrade to ''/[] on every bad-payload path."""
    rows = _earnings_rows()
    block = graph_module._format_earnings_hits(rows)
    assert "Earnings-release excerpts" in block
    assert "NVDA Q1 FY26 earnings release" in block
    assert "Management guided Q2 revenue" in block
    assert graph_module._format_earnings_hits("[]") == ""
    assert graph_module._format_earnings_hits("not json") == ""

    sources = graph_module._parse_earnings_sources(rows)
    assert sources == [
        {
            "headline": "NVDA Q1 FY26 earnings release",
            "source": "guidance",
            "date": "2026-05-28",
            "url": "https://sec.gov/ex99-1",
            "corpus": "earnings",
        }
    ]
    assert graph_module._parse_earnings_sources("[]") == []
    assert graph_module._parse_earnings_sources("[1, 2, 3]") == []


def test_format_search_hits_renders_rows_and_degrades_to_empty() -> None:
    rows = json.dumps(
        [
            {"headline": "AAPL faces antitrust probe", "source": "Bloomberg", "date": "2026-05-02"},
            {"headline": "buyback expanded", "source": "WSJ", "date": "2026-05-03"},
        ]
    )
    block = _format_search_hits(rows)
    assert "AAPL faces antitrust probe" in block
    assert "Bloomberg, 2026-05-02" in block
    # One "- " bullet per row so _summarise_report counts them as headlines.
    assert block.count("\n- ") == 2
    # Degraded paths return "" so the caller skips the merge.
    assert _format_search_hits("[]") == ""
    assert _format_search_hits("not json") == ""
    assert _format_search_hits("[1, 2, 3]") == ""


def test_format_search_hits_renders_body_under_headline() -> None:
    """QNT-225: the article summary (body) is rendered, indented, under its
    headline so the synthesis reads the story -- and a row with an empty body
    still renders the headline alone."""
    rows = json.dumps(
        [
            {
                "headline": "NVDA and SK Hynix announce memory partnership",
                "source": "Reuters",
                "date": "2026-06-05",
                "body": "Nvidia and SK Hynix signed a multi-year deal to co-develop "
                "next-generation HBM for AI data centers.",
            },
            {"headline": "Micron mentioned in memory-shortage note", "source": "WSJ", "body": ""},
        ]
    )
    block = _format_search_hits(rows)
    assert "- NVDA and SK Hynix announce memory partnership (Reuters, 2026-06-05)" in block
    assert "  Nvidia and SK Hynix signed a multi-year deal" in block
    # Empty-body row renders the headline only -- no stray indented blank line.
    assert "  \n" not in block


def test_format_search_hits_truncates_long_body() -> None:
    """QNT-225: an over-long body is cut on a word boundary with an ellipsis to
    bound the prompt cost."""
    long_body = "word " * 200  # ~1000 chars
    rows = json.dumps([{"headline": "h", "source": "s", "date": "d", "body": long_body}])
    block = _format_search_hits(rows)
    body_line = next(line for line in block.splitlines() if line.startswith("  "))
    assert body_line.endswith("...")
    assert len(body_line.strip()) <= 284  # 280 + ellipsis, word-boundary cut


def test_format_earnings_hits_preserves_full_chunk() -> None:
    """QNT-276 AC3: an earnings chunk is preserved close to its full ~900-char
    length -- the old global 280 cap (still used for news) gutted the 8-K
    guidance paragraph that is the whole reason the earnings corpus exists."""
    # ~850 chars: well past the 280 news budget, inside the 900 earnings budget.
    text = "word " * 170
    rows = json.dumps(
        [
            {
                "title": "NVDA Q1 FY26 earnings release",
                "section": "guidance",
                "date": "2026-05-28",
                "url": "https://sec.gov/x",
                "text": text,
            }
        ]
    )
    block = graph_module._format_earnings_hits(rows)
    body_line = next(line for line in block.splitlines() if line.startswith("  "))
    # Full chunk survives -- not truncated at the 280-char news budget.
    assert "..." not in body_line
    assert len(body_line.strip()) > 280

    # A chunk past the earnings budget cuts near ~900, still far above 280.
    long_text = "word " * 250  # ~1250 chars
    long_rows = json.dumps(
        [{"title": "t", "section": "guidance", "date": "d", "url": "u", "text": long_text}]
    )
    long_block = graph_module._format_earnings_hits(long_rows)
    long_body = next(line for line in long_block.splitlines() if line.startswith("  "))
    assert long_body.endswith("...")
    assert 280 < len(long_body.strip()) <= 904  # 900 + ellipsis, word-boundary cut


def test_news_fold_orders_retrieved_hits_ahead_of_canned_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-276 AC1: the retrieved-hits block LEADS the news report, ahead of the
    canned digest, so the synthesis prompt never sees it demoted below the
    generic headlines."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("news", "llm", True, False, ""),
    )
    llm = _news_focused_llm()
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)

    search = _recording_search_news(
        [{"headline": "NVDA sued over chip patents", "source": "Reuters", "date": "2026-06-01"}]
    )
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_news_tool=search,
    )
    report = graph.invoke({"ticker": "NVDA", "question": "any litigation news on NVDA?"})[
        "reports"
    ]["news"]

    # Both blocks present, retrieved hit ordered ahead of the canned digest.
    assert "NVDA sued over chip patents" in report
    assert "news for NVDA" in report
    assert report.index("NVDA sued over chip patents") < report.index("news for NVDA")


def test_earnings_fold_orders_retrieved_hits_ahead_of_canned_digest(
    monkeypatch: pytest.MonkeyPatch, stub_llm: _StructuredLLM
) -> None:
    """QNT-276 AC1: the retrieved earnings excerpts LEAD the fundamental report,
    ahead of the canned fundamental digest -- same foregrounding as the news
    fold."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("thesis", "llm", False, True, ""),
    )
    search_earnings = MagicMock(return_value=_earnings_rows())
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_earnings_tool=search_earnings,
    )
    report = graph.invoke(
        {"ticker": "NVDA", "question": "what did NVDA management say about guidance?"}
    )["reports"]["fundamental"]

    assert "Management guided Q2 revenue" in report
    assert "fundamental for NVDA" in report
    assert report.index("Management guided Q2 revenue") < report.index("fundamental for NVDA")


def test_targeted_earnings_drops_focused_card_and_surfaces_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-276: the news narrative-only pattern extends to the fundamental focus.
    An earnings-narrative ask (intent=fundamental, needs_earnings_search) WITH
    retrieved hits drops the focused-card LLM call and lets narrate own the
    answer, foregrounding the retrieved 8-K excerpt; the hits surface as
    corpus-tagged ``retrieved_sources``."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("fundamental", "llm", False, True, ""),
    )
    llm = _StructuredLLM()
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)

    search_earnings = MagicMock(return_value=_earnings_rows())
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_earnings_tool=search_earnings,
    )
    question = "what did NVDA management say about guidance and the outlook?"
    result = graph.invoke({"ticker": "NVDA", "question": question})

    search_earnings.assert_called_once_with("NVDA", question)
    # Focused card dropped; the focused-card LLM call was never made.
    assert result["focused"] is None
    llm.structured_invoke.assert_not_called()
    # Retrieved earnings hits surfaced as corpus-tagged provenance.
    sources = result["retrieved_sources"]
    assert sources and all(s["corpus"] == "earnings" for s in sources)
    # And folded into the fundamental report the narrator speaks from.
    assert "Management guided Q2 revenue" in result["reports"]["fundamental"]
