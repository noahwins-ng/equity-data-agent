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
    AgentState,
    ThesisPlan,
    ToolFn,
    _composite_confidence,
    _confidence_from_reports,
    _format_search_hits,
    _parse_plan,
    _quick_fact_cited_value_supported,
    _quick_fact_grounding,
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

    def with_structured_output(self, schema: object, **_kwargs: object) -> MagicMock:
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

    assert result["answer"] is expected
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


# ─── QNT-296: runtime numeric grounding for quick_fact (narrate-skip gap) ────


def test_quick_fact_grounding_clean_card_yields_full_confidence() -> None:
    """AC1: a clean QuickFactAnswer -- cited value verbatim in its report --
    yields grounding_rate 1.0 and confidence == coverage."""
    quick = QuickFactAnswer(
        answer="RSI sits at 62 (source: technical).",
        cited_value="62",
        source="technical",
    )
    state: AgentState = {"ticker": "NVDA", "confidence": 1.0, "reports": {"technical": "RSI 62"}}

    result: dict[str, Any] = _quick_fact_grounding(state, quick)

    assert result["grounding_rate"] == 1.0
    assert result["grounding_unsupported"] == []
    assert result["confidence"] == 1.0


def test_quick_fact_grounding_fabricated_number_lowers_confidence() -> None:
    """AC1: a fabricated number in the answer (absent from every gathered
    report) drops grounding_rate below 1.0 and lowers composite confidence."""
    quick = QuickFactAnswer(
        answer="RSI sits at 99 (source: technical).",
        cited_value="99",
        source="technical",
    )
    state: AgentState = {"ticker": "NVDA", "confidence": 1.0, "reports": {"technical": "RSI 62"}}

    result: dict[str, Any] = _quick_fact_grounding(state, quick)

    assert result["grounding_rate"] < 1.0
    assert result["confidence"] < 1.0
    assert "99" in result["grounding_unsupported"]


def test_quick_fact_grounding_valid_cited_value_not_double_counted() -> None:
    """A valid, verbatim cited_value is already embedded in to_markdown()'s
    "**Value:**" line, so the regex check already scored it as one of
    thesis_numbers -- the C-4 pass must not add a second denominator slot
    for the same claim. An unrelated fabricated number elsewhere in the
    answer should score exactly 1 unsupported out of 2 claims (0.5), not
    1 out of 3 (0.67) -- the latter would mean the same clean citation
    silently inflates confidence in mixed-error turns."""
    quick = QuickFactAnswer(
        answer="RSI sits at 62, up from 58 (source: technical).",
        cited_value="62",
        source="technical",
    )
    state: AgentState = {"ticker": "NVDA", "confidence": 1.0, "reports": {"technical": "RSI 62"}}

    result: dict[str, Any] = _quick_fact_grounding(state, quick)

    assert result["grounding_rate"] == 0.5
    assert result["grounding_unsupported"] == ["58"]


def test_quick_fact_grounding_formatted_fabricated_value_stays_in_zero_one_range() -> None:
    """A formatted cited_value (``$1,234.56`` -- a documented example shape
    from the QuickFactAnswer field description) that fails the C-4 check
    must be flagged using its canonicalised form, not the raw string --
    otherwise the same claim lands in ``grounding_unsupported`` twice (once
    canonical from the regex pass, once raw from the C-4 pass), which can
    double-penalise a single hallucination and push grounding_rate below
    0.0 (unclamped, unlike composite confidence)."""
    quick = QuickFactAnswer(
        answer="Market cap sits at $1,234.56 (source: fundamental).",
        cited_value="$1,234.56",
        source="fundamental",
    )
    state: AgentState = {
        "ticker": "NVDA",
        "confidence": 1.0,
        "reports": {"fundamental": "Market cap: $999.00"},
    }

    result: dict[str, Any] = _quick_fact_grounding(state, quick)

    assert result["grounding_rate"] == 0.0
    assert result["grounding_unsupported"] == ["1234.56"]


def test_quick_fact_cited_value_verbatim_in_named_report_passes() -> None:
    """AC4: a cited_value that's a verbatim substring of the report named by
    ``source`` passes -- not flagged, grounding stays 1.0."""
    quick = QuickFactAnswer(
        answer="Sentiment reads overbought (source: technical).",
        cited_value="overbought",
        source="technical",
    )
    state: AgentState = {
        "ticker": "NVDA",
        "confidence": 1.0,
        "reports": {"technical": "RSI regime: overbought"},
    }

    assert _quick_fact_cited_value_supported(quick, state) is True
    result: dict[str, Any] = _quick_fact_grounding(state, quick)
    assert result["grounding_rate"] == 1.0
    assert result["grounding_unsupported"] == []


def test_quick_fact_cited_value_case_insensitive_match_passes() -> None:
    """A cited_value differing from the report only in case is still a
    verbatim citation of the same fact -- naive case-sensitive matching
    would false-positive-flag harmless case drift as unsupported."""
    quick = QuickFactAnswer(
        answer="Sentiment reads Overbought (source: technical).",
        cited_value="Overbought",
        source="technical",
    )
    state: AgentState = {
        "ticker": "NVDA",
        "confidence": 1.0,
        "reports": {"technical": "RSI regime: overbought"},
    }

    assert _quick_fact_cited_value_supported(quick, state) is True


def test_quick_fact_cited_value_substring_collision_is_rejected() -> None:
    """A garbled citation that happens to be a SUBSTRING of the right word
    (``"sold"`` inside the report's ``"oversold"``) must NOT read as
    supported -- a naive ``in`` check would let this invented/garbled
    citation slip through as fully grounded, exactly the failure mode C-4
    exists to catch."""
    quick = QuickFactAnswer(
        answer="Sentiment reads sold (source: technical).",
        cited_value="sold",
        source="technical",
    )
    state: AgentState = {
        "ticker": "NVDA",
        "confidence": 1.0,
        "reports": {"technical": "RSI regime: oversold"},
    }

    assert _quick_fact_cited_value_supported(quick, state) is False


def test_quick_fact_cited_value_absent_from_named_report_is_flagged() -> None:
    """AC4: a cited_value absent from its named report's text (even if it
    would pass the number-regex check by appearing elsewhere) lowers
    grounding and is flagged -- catches a reformatted/invented non-numeric
    value like a wrong regime word."""
    quick = QuickFactAnswer(
        answer="Sentiment reads oversold (source: technical).",
        cited_value="oversold",
        source="technical",
    )
    state: AgentState = {
        "ticker": "NVDA",
        "confidence": 1.0,
        "reports": {"technical": "RSI regime: overbought"},
    }

    assert _quick_fact_cited_value_supported(quick, state) is False
    result: dict[str, Any] = _quick_fact_grounding(state, quick)
    assert result["grounding_rate"] < 1.0
    assert "oversold" in result["grounding_unsupported"]


def test_quick_fact_empty_cited_value_is_a_noop() -> None:
    """AC4: an empty cited_value (the 'not available' apology shape) never
    triggers the C-4 check and never lowers grounding on its own."""
    quick = QuickFactAnswer(
        answer="RSI not available in the supplied reports.",
        cited_value="",
        source=None,
    )
    state: AgentState = {"ticker": "NVDA", "confidence": 1.0, "reports": {"technical": "RSI 62"}}

    assert _quick_fact_cited_value_supported(quick, state) is True
    result: dict[str, Any] = _quick_fact_grounding(state, quick)
    assert result["grounding_rate"] == 1.0
    assert result["grounding_unsupported"] == []


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
    assert isinstance(result["answer"], Thesis)


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


def test_gather_reports_runs_tools_concurrently() -> None:
    """QNT-300 (B-6): the planned tools are fetched in parallel, so a plan of N
    tools that each block ``d`` seconds gathers in ~``d``, not ~``N*d``.

    Each tool blocks on a shared barrier that only releases once ALL of them
    have entered -- if the gather were serial the first tool would deadlock
    waiting for peers that never start, so passing at all proves concurrency.
    The wall-clock assertion is a loose upper bound (well under the 4x a serial
    loop would cost) to stay robust on a loaded CI box.
    """
    import threading
    import time

    plan = ["company", "technical", "fundamental", "news"]
    barrier = threading.Barrier(len(plan), timeout=5)
    block = 0.15

    def _make(name: str) -> Callable[[str], str]:
        def _tool(ticker: str) -> str:
            barrier.wait()  # deadlocks unless all tools run at once
            time.sleep(block)
            return f"## {name} for {ticker}"

        return _tool

    tools = {name: _make(name) for name in plan}
    start = time.perf_counter()
    reports, errors = graph_module._gather_reports("NVDA", plan=plan, tools=tools)
    elapsed = time.perf_counter() - start

    assert errors == {}
    assert set(reports) == set(plan)
    # Serial would be >= len(plan) * block = 0.6s; concurrent is ~block. Assert
    # comfortably below the serial floor.
    assert elapsed < len(plan) * block, f"gather was not concurrent: {elapsed:.3f}s"


def test_gather_reports_concurrent_preserves_optional_and_error_contract() -> None:
    """QNT-300 (B-6): parallel gather keeps the retry/optional-drop/error-map
    contract -- an optional tool ('news') failing is dropped silently while a
    required tool failing surfaces in ``errors``, regardless of scheduling."""
    tools = {
        "company": _mock_tool("co"),
        "technical": _mock_tool("[error] http-500: down"),
        "news": _mock_tool("[error] http-500: down"),
    }
    reports, errors = graph_module._gather_reports(
        "NVDA", plan=["company", "technical", "news"], tools=tools
    )
    assert reports == {"company": "co for NVDA"}
    assert errors["technical"] == "[error] http-500: down for NVDA"
    assert "news" not in errors  # optional drop preserved under concurrency


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

    assert not isinstance(result["answer"], Thesis)
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

        def with_structured_output(self, schema: object, **_kwargs: object) -> RunnableLambda:
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

    assert result["answer"] is valid_thesis, "retry recovery must produce the valid thesis"
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

    assert not isinstance(result["answer"], Thesis)


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

    assert result["answer"] is expected


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

    assert isinstance(result["answer"], ConversationalAnswer)
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
    assert isinstance(result["answer"], ConversationalAnswer)


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
    assert isinstance(result["answer"], Thesis)


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
    # QNT-296: the runtime grounding check now runs on this narrate-skip
    # path, so the mock report must actually contain the cited value —
    # a bare "tech for NVDA" would (correctly) flag "62" as unsupported.
    graph = build_graph({"technical": MagicMock(return_value="## technical\nRSI 62\n")})

    # QNT-212: the question names no ticker AND there's no prior turn, so
    # the ambiguity detector would short-circuit to clarify. Anchor the
    # question with a ticker so the quick-fact path actually runs.
    result = graph.invoke({"ticker": "NVDA", "question": "What's NVDA's RSI right now?"})

    assert result["intent"] == "quick_fact"
    assert isinstance(result["answer"], QuickFactAnswer)
    # Confidence reflects coverage x grounding — both are clean here.
    assert result["confidence"] == 1.0
    assert result["grounding_rate"] == 1.0
    assert result["grounding_unsupported"] == []


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
    assert isinstance(result["answer"], QuickFactAnswer)


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
    assert isinstance(result["answer"], Thesis)


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
    assert not isinstance(result["answer"], QuickFactAnswer)


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
    assert isinstance(result["answer"], Thesis)


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
    assert isinstance(result["answer"], ComparisonAnswer)
    assert [s.ticker for s in result["answer"].sections] == ["NVDA", "AAPL"]
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
    assert isinstance(result["answer"], ConversationalAnswer)
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
    assert isinstance(result["answer"], ConversationalAnswer)
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
    assert isinstance(result["answer"], ConversationalAnswer)
    # Deterministic redirect mentions covered tickers + suggestions.
    answer = result["answer"].answer
    assert any(t in answer for t in ("NVDA", "AAPL", "MSFT"))
    assert len(result["answer"].suggestions) == 3


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
    assert isinstance(result["answer"], ConversationalAnswer)


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
    assert isinstance(result["answer"], ConversationalAnswer)
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
    assert isinstance(result["answer"], Thesis)


# ─── QNT-298: plan_node / explore_supervisor_node emit plan_rationale ──────


def test_plan_node_emits_plan_rationale_via_event_emitter(stub_llm: _StructuredLLM) -> None:
    """QNT-298: plan_node streams the thesis-plan rationale over SSE as soon
    as it resolves -- BEFORE gather's tool calls fire -- so the panel can
    fill the classify->plan->gather->synthesize dead air with a real
    sentence instead of a generic spinner."""
    emitted: list[tuple[str, dict[str, object]]] = []

    def emit(event: str, data: dict[str, object]) -> None:
        emitted.append((event, dict(data)))

    graph = build_graph({name: _mock_tool(name) for name in REPORT_TOOLS}, event_emitter=emit)
    result = _run(graph)

    rationale_calls = [(e, d) for e, d in emitted if e == "plan_rationale"]
    assert rationale_calls, f"plan_node must emit ('plan_rationale', ...); got {emitted}"
    assert rationale_calls[0][1] == {"text": "Balanced thesis, so all reports are relevant."}
    assert result["plan_rationale"] == "Balanced thesis, so all reports are relevant."


def test_plan_node_skips_emit_when_rationale_is_none(stub_llm: _StructuredLLM) -> None:
    """quick_fact plans carry no rationale (comma-list planner, not the
    structured ThesisPlan) -- no event should fire, not an empty string."""
    stub_llm.invoke.return_value = AIMessage(content="technical")
    quick = QuickFactAnswer(
        answer="RSI sits at 62 (source: technical).", cited_value="62", source="technical"
    )
    stub_llm.structured_invoke.return_value = quick
    emitted: list[tuple[str, dict[str, object]]] = []

    def emit(event: str, data: dict[str, object]) -> None:
        emitted.append((event, dict(data)))

    graph = build_graph(
        {"technical": MagicMock(return_value="## technical\nRSI 62\n")}, event_emitter=emit
    )
    result = graph.invoke(
        {"ticker": "NVDA", "question": "What's NVDA's RSI right now?"},
    )

    assert result["intent"] == "quick_fact"
    assert result.get("plan_rationale") is None
    assert not [e for e, _ in emitted if e == "plan_rationale"]


def test_plan_node_swallows_event_emitter_exceptions_for_rationale(
    stub_llm: _StructuredLLM,
) -> None:
    """A misbehaving event_emitter must not crash the graph (same contract
    as classify_node's intent emission, see test_classify_node_swallows_
    event_emitter_exceptions)."""

    def broken_emit(_event: str, _data: dict[str, object]) -> None:
        raise RuntimeError("Event loop is closed")

    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS}, event_emitter=broken_emit
    )
    result = _run(graph)
    assert result["plan_rationale"] == "Balanced thesis, so all reports are relevant."


def test_plan_rationale_emission_fires_zero_extra_llm_calls(stub_llm: _StructuredLLM) -> None:
    """AC4: streaming plan_rationale over SSE is a pure string re-emit of a
    value the plan-LLM call already produced -- it must not fire any
    additional LLM call. Pins the exact same call counts a thesis run makes
    with no event_emitter (see test_full_flow_produces_thesis_and_confidence):
    one structured plan call, one structured synthesize call, zero raw
    ``invoke`` calls (classify short-circuits via the heuristic)."""
    emitted: list[tuple[str, dict[str, object]]] = []

    def emit(event: str, data: dict[str, object]) -> None:
        emitted.append((event, dict(data)))

    graph = build_graph({name: _mock_tool(name) for name in REPORT_TOOLS}, event_emitter=emit)
    _run(graph)

    assert [e for e, _ in emitted if e == "plan_rationale"], "rationale event never fired"
    assert stub_llm.invoke.call_count == 0
    assert stub_llm.plan_invoke.call_count == 1
    assert stub_llm.structured_invoke.call_count == 1


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


# ─── QNT-288: declarative per-intent routing policy table ──────────────────


def test_every_intent_literal_has_a_fully_populated_policy() -> None:
    """Meta-test: a future intent cannot ship half-configured.

    QNT-263's failure mode was ``quick_fact`` missing from the
    earnings-search intent set because nothing enforced that a new intent
    (or a new flag on an existing one) touched every parallel set. This
    asserts every member of the ``Intent`` literal has an ``INTENT_POLICIES``
    entry with every field populated to a value of the expected type/shape.
    """
    from typing import get_args

    from agent.conversational import _SUGGESTION_BANK
    from agent.graph import INTENT_POLICIES, IntentPolicy
    from agent.intent import Intent

    intents = get_args(Intent)
    assert intents, "Intent literal resolved to no members -- typing changed"

    missing = [intent for intent in intents if intent not in INTENT_POLICIES]
    assert not missing, f"INTENT_POLICIES missing entries for: {missing}"

    extra = set(INTENT_POLICIES) - set(intents)
    assert not extra, f"INTENT_POLICIES has entries for non-Intent values: {extra}"

    bank_labels = {label for label, _ in _SUGGESTION_BANK}

    for intent in intents:
        policy = INTENT_POLICIES[intent]
        assert isinstance(policy, IntentPolicy), f"{intent!r} entry is not an IntentPolicy"
        assert policy.focused_report is None or isinstance(policy.focused_report, str), intent
        assert isinstance(policy.rag_corpora, frozenset), intent
        assert policy.rag_corpora <= {"news", "earnings"}, (
            f"{intent!r} rag_corpora has an unexpected corpus: {policy.rag_corpora}"
        )
        assert isinstance(policy.history_budget, int) and policy.history_budget > 0, intent
        assert policy.company_variant in ("compact", "full"), intent
        assert isinstance(policy.requires_ticker, bool), intent
        assert isinstance(policy.short_circuit, bool), intent
        assert policy.suggestion_hint is None or isinstance(policy.suggestion_hint, str), intent
        # A hint that doesn't resolve to a real bank label silently degrades
        # domain_redirect's suggestion bias at runtime -- catch that here
        # rather than in prod (mirrors test_hint_from_intent_quick_fact_
        # resolves_to_a_real_bank_label above, generalised to every intent).
        if policy.suggestion_hint is not None:
            assert policy.suggestion_hint in bank_labels, (
                f"{intent!r} suggestion_hint {policy.suggestion_hint!r} not in "
                f"suggestion bank labels {bank_labels}"
            )
        # QNT-298: followup_templates is a tuple of (target_intent, template)
        # pairs or None. Every template must carry a ``{ticker}`` and/or
        # ``{partner}`` slot (the comparison shape's second entry names only
        # the partner) so a filled chip always names a resolved ticker.
        assert policy.followup_templates is None or isinstance(policy.followup_templates, tuple), (
            intent
        )
        if policy.followup_templates is not None:
            assert policy.followup_templates, f"{intent!r} followup_templates is an empty tuple"
            for target_intent, template in policy.followup_templates:
                assert target_intent in intents, (
                    f"{intent!r} followup_templates target {target_intent!r} is not an Intent"
                )
                assert "{ticker}" in template or "{partner}" in template, (
                    f"{intent!r} followup_templates entry names no ticker slot: {template!r}"
                )


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
        assert isinstance(result["answer"], FocusedAnalysis)
        assert result["answer"].focus == intent


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
    assert isinstance(result["answer"], FocusedAnalysis)


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

    assert isinstance(result["answer"], ConversationalAnswer)


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

    assert isinstance(result["answer"], ConversationalAnswer)
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

    assert isinstance(result["answer"], FocusedAnalysis)
    assert result["answer"].focus == "technical"


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
    would be wasted (gate scoped to the news-reading intents)."""
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


def test_gather_emits_retrieved_sources_before_narrate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-305 follow-up: gather emits ``retrieved_sources`` through the
    event_emitter, and it lands BEFORE the narrate bubble streams. That early
    row count is what lets the frontend anchor-integrity guard tell an in-range
    id from a fabricated one WHILE the narration is still streaming -- without
    it a hallucinated Rn renders mid-stream and then vanishes on completion (a
    flicker). Pins both that the early emit fires and that it precedes narrate."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("news", "llm", True, False, ""),
    )
    llm = _news_focused_llm()
    # Give narrate a working .stream() so narrative_chunk actually fires and the
    # ordering assertion below is real rather than vacuously skipped.
    llm.stream = lambda *_a, **_kw: iter(  # type: ignore[attr-defined]
        [AIMessage(content="On balance "), AIMessage(content="the read is cautious.")]
    )
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)
    search = _recording_search_news(
        [{"headline": "H", "source": "Reuters", "date": "2026-06-01", "url": "https://ex.com/a"}]
    )
    emitted: list[tuple[str, dict[str, Any]]] = []
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_news_tool=search,
        event_emitter=lambda e, d: emitted.append((e, dict(d))),
    )
    graph.invoke({"ticker": "NVDA", "question": "any news on NVDA and the Micron deal?"})

    names = [e for e, _ in emitted]
    assert "retrieved_sources" in names, f"gather must emit retrieved_sources early; got {names}"
    rs_idx = names.index("retrieved_sources")
    assert emitted[rs_idx][1]["sources"][0]["id"] == "R1"
    # The count must reach the client before the first narrate delta.
    if "narrative_chunk" in names:
        assert rs_idx < names.index("narrative_chunk"), (
            "retrieved_sources must precede narrate so the guard has the row "
            f"count before anchors stream; got {names}"
        )


def test_early_card_emit_strips_out_of_range_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-305 follow-up: the EARLY card emit (synthesize, before narrate) is
    stripped with the same gate as the post-graph emit -- both for an
    out-of-range id (R5, only R1 retrieved) AND a corpus-mismatched one
    (``fundamental R1`` where R1 is a news row). The early-emitted card must
    already carry both de-anchored (kept as the canned ``(source: …)``) so it
    never renders a bad anchor that the stripped post-graph card then removes --
    the card's twin of the narrate flicker."""
    from ._thesis_factory import make_thesis

    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("thesis", "llm", True, False, ""),
    )
    llm = _StructuredLLM()
    llm._structured_runnable.invoke = MagicMock(
        return_value=make_thesis(
            supports=[
                "Buyback expanded (source: news R5)",  # out of range (1 row)
                "Deal closed (source: news R1)",  # in range + right corpus
                "Growth strong (source: fundamental R1)",  # in range, WRONG corpus (R1 is news)
            ],
        )
    )
    llm.stream = lambda *_a, **_kw: iter([AIMessage(content="cautious.")])  # type: ignore[attr-defined]
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)
    # The fold tags each retrieved row corpus="news", so R1 is a news row.
    search = _recording_search_news(
        [{"headline": "H", "source": "Reuters", "date": "2026-06-01", "url": "https://ex.com/a"}]
    )
    emitted: list[tuple[str, dict[str, Any]]] = []
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        search_news_tool=search,
        event_emitter=lambda e, d: emitted.append((e, dict(d))),
    )
    graph.invoke({"ticker": "NVDA", "question": "is NVDA a buy given the latest news?"})

    thesis_ev = next(d for e, d in emitted if e == "thesis")
    supports = thesis_ev["technical"]["supports"]
    assert supports[0] == "Buyback expanded (source: news)"  # R5 out of range -> id stripped
    assert supports[1] == "Deal closed (source: news R1)"  # R1 in range + news corpus -> kept
    assert supports[2] == "Growth strong (source: fundamental)"  # wrong corpus -> id stripped


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
    assert result["answer"] is None
    llm.structured_invoke.assert_not_called()
    # Retrieved hits surfaced as structured provenance.
    sources = result["retrieved_sources"]
    assert sources == [
        {
            # QNT-301: stable claim-anchor id, R1 for the first retrieved hit.
            "id": "R1",
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
    assert isinstance(result["answer"], FocusedAnalysis)
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
    assert isinstance(result["answer"], FocusedAnalysis)
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
            # QNT-301: first kept row gets the R1 anchor id.
            "id": "R1",
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


# ─── QNT-290: followup turns fire RAG retrieval ──────────────────────────────


def test_followup_with_needs_news_search_fires_search_and_folds_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: a followup turn where the classifier set needs_news_search fires
    search_news (using the QNT-289 rewritten query) and folds the hit into the
    prompt substrate (reports["news"]), even though followup never re-runs the
    report plan (zero report-tool calls)."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("followup", "llm", True, False, "NVDA CEO buyback"),
    )
    search = _recording_search_news(
        [{"headline": "NVDA CEO addresses buyback", "source": "Reuters", "date": "2026-06-01"}]
    )
    report_tools = {name: MagicMock(side_effect=_mock_tool(name)) for name in REPORT_TOOLS}
    graph = build_graph(report_tools, search_news_tool=search)

    result = graph.invoke(
        {
            "ticker": "NVDA",
            "question": "and what did the CEO say about it?",
            # Prior-turn anchor so the followup doesn't route to clarify.
            "reports": {"news": "## news\nprior digest\n"},
        }
    )

    search.assert_called_once_with("NVDA", "NVDA CEO buyback")
    assert "NVDA CEO addresses buyback" in result["reports"]["news"]
    # The report PLAN never re-runs on followup.
    assert result["plan"] == []
    assert sum(t.call_count for t in report_tools.values()) == 0
    assert result["retrieved_sources"] == [
        {
            "id": "R1",
            "headline": "NVDA CEO addresses buyback",
            "source": "Reuters",
            "date": "2026-06-01",
            "url": "",
            "corpus": "news",
        }
    ]
    # QNT-290: a flagged followup now visits plan/gather for the RAG branch.
    assert "gather" in result["intent_path"]


def test_followup_without_search_flags_gathers_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: a followup turn with both search flags False gathers nothing --
    zero report-tool calls, zero search calls, hydrated reports reused verbatim
    (today's behaviour, unchanged)."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("followup", "llm", False, False, ""),
    )
    search = MagicMock(return_value="[]")
    report_tools = {name: MagicMock(side_effect=_mock_tool(name)) for name in REPORT_TOOLS}
    graph = build_graph(report_tools, search_news_tool=search)

    result = graph.invoke(
        {
            "ticker": "NVDA",
            "question": "why?",
            "reports": {"news": "## news\nprior digest\n"},
        }
    )

    search.assert_not_called()
    assert sum(t.call_count for t in report_tools.values()) == 0
    assert result["reports"] == {"news": "## news\nprior digest\n"}
    assert not result.get("retrieved_sources")
    # Pure followup keeps the QNT-212 short-circuit: no plan/gather visit.
    assert "gather" not in result["intent_path"]


def test_strip_retrieved_block_survives_embedded_blank_line_in_hit_body() -> None:
    """AC2 regression: a chunk/body with an internal blank line (plausible raw
    8-K filing prose) must not fool ``_strip_retrieved_block``'s boundary
    heuristic into treating the hit's own text as the start of the original
    report. ``_truncate_body`` collapses internal whitespace precisely so the
    retrieved block can never contain a blank line -- verify that guarantee
    holds end to end through ``_format_earnings_hits`` and the strip."""
    raw = json.dumps(
        [
            {
                "title": "NVDA Q1 FY26 earnings release",
                "section": "guidance",
                "date": "2026-05-28",
                "text": (
                    "Management guided Q2 revenue higher on data-center demand.\n\n"
                    "The company also announced a new buyback authorization."
                ),
            }
        ]
    )
    from agent.prompts import RETRIEVED_EARNINGS_HEADING

    hits = graph_module._format_earnings_hits(raw)
    assert "\n\n" not in hits  # the fold-boundary invariant the strip relies on
    assert "buyback authorization" in hits  # nothing was silently dropped

    folded = f"{hits}\n\n## FUNDAMENTAL REPORT\noriginal canned digest\n"
    base = graph_module._strip_retrieved_block(folded, RETRIEVED_EARNINGS_HEADING)
    assert base == "## FUNDAMENTAL REPORT\noriginal canned digest\n"


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
    quick_fact, whose policy now reads the earnings corpus -- so it reaches the 8-K
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
    the earnings-reading intents)."""
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
    # INTENT gate (technical's policy does not read the earnings corpus) skipped the search,
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


def test_registry_entry_alone_drives_gate_call_fold_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-291 AC3: a single FAKE RetrievalSpec exercises the whole
    gate -> call -> fold -> provenance path with NO change to gather_node --
    proving a new retrieval corpus (tool #4: a filings or transcript corpus) is
    one RETRIEVAL_SPECS entry plus wiring its callable through ``retrieval_tools``.

    The fake spec reuses the ``needs_news_search`` gate but points at a wholly
    invented fold target (``reports["fake"]``) and corpus tag
    (``corpus="filings"``), so what determines the fold and the surfaced
    provenance is the registry entry alone -- not any hardcoded branch.
    """
    fold_calls: list[tuple[str, int]] = []

    def fake_fold(
        reports: dict[str, str], raw: str, start_id: int
    ) -> tuple[dict[str, str], list[dict[str, str]]]:
        fold_calls.append((raw, start_id))
        updated = {**reports, "fake": f"FOLDED::{raw}"}
        return updated, [{"id": f"R{start_id}", "headline": "fake hit", "corpus": "filings"}]

    fake_spec = graph_module.RetrievalSpec(
        name="fake_search",
        flag="needs_news_search",
        corpus="news",
        fold=fake_fold,
        hit_noun="things",
    )
    # The ONLY graph-side change to add the tool: swap in the registry entry.
    monkeypatch.setattr(graph_module, "RETRIEVAL_SPECS", (fake_spec,))
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("news", "llm", True, False, ""),
    )
    llm = _news_focused_llm()
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: llm)

    question = "what did NVDA disclose in its latest filing?"
    fake_tool = MagicMock(return_value="RAWPAYLOAD")
    # The ONLY wiring: inject the callable under the spec's name.
    graph = build_graph(
        {name: _mock_tool(name) for name in REPORT_TOOLS},
        retrieval_tools={"fake_search": fake_tool},
    )
    result = graph.invoke({"ticker": "NVDA", "question": question})

    # gate + call: fired exactly once with (ticker, verbatim query).
    fake_tool.assert_called_once_with("NVDA", question)
    # fold: the spec's own fold ran (start_id 1) and merged into its target key.
    assert fold_calls == [("RAWPAYLOAD", 1)]
    assert result["reports"]["fake"] == "FOLDED::RAWPAYLOAD"
    # provenance: surfaced verbatim from the spec's fold, corpus tag intact.
    assert result["retrieved_sources"] == [
        {"id": "R1", "headline": "fake hit", "corpus": "filings"}
    ]


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
            "id": "R1",
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
    assert "- [R1] NVDA and SK Hynix announce memory partnership (Reuters, 2026-06-05)" in block
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


def test_retrieved_hit_ids_align_across_block_and_sources() -> None:
    """QNT-301 AC1: folded news + earnings hits carry stable ``R{n}`` ids that
    stay aligned between the prompt block (``[Rn]`` bullet tags) and the
    retrieved_sources rows, and the combined list is R1..Rn gap-free across both
    corpora (news folds first, earnings offsets past it)."""
    news_raw = json.dumps(
        [
            {"headline": "NVDA sued over patents", "source": "Reuters", "date": "2026-06-01"},
            {"headline": "NVDA buyback expanded", "source": "WSJ", "date": "2026-06-02"},
        ]
    )
    earnings_raw = json.dumps(
        [
            {
                "title": "NVDA Q1 FY26 release",
                "section": "guidance",
                "date": "2026-05-28",
                "text": "guided higher",
            },
            {
                "title": "NVDA Q2 FY26 release",
                "section": "risk factors",
                "date": "2026-05-28",
                "text": "supply risk",
            },
        ]
    )
    reports: dict[str, str] = {}
    reports, news_sources = graph_module._fold_news_hits(reports, news_raw)
    combined = list(news_sources)
    # Mirror the gather call-site: earnings ids continue past the news hits.
    reports, earnings_sources = graph_module._fold_earnings_hits(
        reports, earnings_raw, len(combined) + 1
    )
    combined += earnings_sources

    # Combined provenance ids are R1..R4, gap-free, news-then-earnings order.
    assert [s["id"] for s in combined] == ["R1", "R2", "R3", "R4"]
    # Each id's [Rn] tag appears on a bullet in the corpus block it belongs to,
    # so a claim citing that id can be traced to the exact folded row.
    for src in news_sources:
        assert f"- [{src['id']}] {src['headline']}" in reports["news"]
    for src in earnings_sources:
        assert f"- [{src['id']}] {src['headline']}" in reports["fundamental"]


def test_retrieved_ids_stay_aligned_when_a_row_has_no_headline() -> None:
    """QNT-301 AC1 (regression): a null/missing headline must be skipped
    IDENTICALLY by the block formatter and the source parser, so the ``[Rn]``
    tag on each kept bullet still matches the id on its source row. Before the
    fix the formatter used ``row.get("headline", "")`` (which returns None for an
    explicit JSON null -> ``str(None)`` == truthy "None"), keeping a row the
    parser skipped and drifting every later id by one."""
    raw = json.dumps(
        [
            {"headline": None, "source": "X", "date": "d"},  # null -> skip in both
            {"headline": "Real headline", "source": "Y", "date": "d2"},
        ]
    )
    block = graph_module._format_search_hits(raw)
    sources = graph_module._parse_search_sources(raw)
    # The null-headline row is dropped by both; the surviving hit is R1 in the
    # block AND R1 in the sources -- no "None" bullet, no id drift.
    assert "None" not in block
    assert sources == [
        {
            "id": "R1",
            "headline": "Real headline",
            "source": "Y",
            "date": "d2",
            "url": "",
            "corpus": "news",
        }
    ]
    assert f"- [{sources[0]['id']}] {sources[0]['headline']}" in block


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
    assert result["answer"] is None
    llm.structured_invoke.assert_not_called()
    # Retrieved earnings hits surfaced as corpus-tagged provenance.
    sources = result["retrieved_sources"]
    assert sources and all(s["corpus"] == "earnings" for s in sources)
    # And folded into the fundamental report the narrator speaks from.
    assert "Management guided Q2 revenue" in result["reports"]["fundamental"]
