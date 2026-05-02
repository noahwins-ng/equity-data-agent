"""Tests for the agent chat SSE endpoint (QNT-74, QNT-149).

The endpoint streams Server-Sent Events while a LangGraph run executes. Tests
patch ``build_graph`` + ``default_report_tools`` so the agent never calls the
real LiteLLM proxy or hits ClickHouse — the contract under test is the SSE
event sequence + payload shape, not the agent's reasoning.

QNT-149: the endpoint additionally emits ``intent`` and ``quick_fact``
events when the classify node picks the quick-fact response shape. Tests
below cover both routes.

Each TestClient call buffers the full streaming body, so we re-parse the
``event: …\\ndata: …`` frames to assert the canonical sequence.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import AsyncGenerator, Iterable
from typing import Any
from unittest.mock import MagicMock

import pytest
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from api import main as main_module
from api.routers import agent_chat as chat_module
from fastapi.testclient import TestClient


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Re-parse an SSE response body into a list of ``(event, data)`` tuples."""
    frames: list[tuple[str, dict[str, Any]]] = []
    for raw in body.split("\n\n"):
        if not raw.strip():
            continue
        event = ""
        data = ""
        for line in raw.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        if event:
            frames.append((event, json.loads(data) if data else {}))
    return frames


def _stub_thesis(setup: str = "NVDA framing.", bull: list[str] | None = None) -> Thesis:
    return Thesis(
        setup=setup,
        bull_case=bull if bull is not None else ["RSI 62 (source: technical)"],
        bear_case=["Multiple compression risk (source: fundamental)"],
        verdict_stance="constructive",
        verdict_action="Trim above SMA50 (source: technical).",
    )


@pytest.fixture
def client() -> Iterable[TestClient]:
    with TestClient(main_module.app) as c:
        yield c


@pytest.fixture
def stub_graph(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch ``build_graph`` so it returns a graph whose ``invoke`` runs each
    instrumented tool once (so the SSE event sequence exercises the wrapper
    code path) and returns a canned final state.

    The instrumentation lives inside the production code, so the test needs a
    graph that actually calls the tool functions; otherwise no ``tool_call``
    events would fire.
    """
    thesis = _stub_thesis()

    def _fake_build(tools: dict[str, Any], **_kwargs: Any) -> Any:
        graph = MagicMock()

        def invoke(state: dict[str, Any]) -> dict[str, Any]:
            ticker = state["ticker"]
            reports: dict[str, str] = {}
            for name, fn in tools.items():
                reports[name] = fn(ticker)
            return {
                "ticker": ticker,
                "intent": "thesis",
                "plan": list(tools.keys()),
                "reports": reports,
                "errors": {},
                "thesis": thesis,
                "quick_fact": None,
                "confidence": 0.67,
            }

        graph.invoke.side_effect = invoke
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(
        chat_module,
        "default_report_tools",
        lambda: {
            "technical": lambda t: f"## technical {t}\n- line 1\n- line 2\n- line 3\n",
            "fundamental": lambda t: f"## fundamental {t}\n- a\n- b\n",
            "news": lambda t: f"## news {t}\n- headline 1\n- headline 2\n- headline 3\n",
        },
    )
    return MagicMock(thesis=thesis)


def test_unknown_ticker_emits_error_then_done(client: TestClient) -> None:
    r = client.post("/api/v1/agent/chat", json={"ticker": "ZZZZ", "message": "hi"})
    assert r.status_code == 200
    frames = _parse_sse(r.text)
    events = [name for name, _ in frames]
    assert events == ["error", "done"]
    _, err_data = frames[0]
    assert err_data["code"] == "unknown-ticker"
    assert "ZZZZ" in err_data["detail"]


def test_happy_path_emits_canonical_sequence(
    client: TestClient,
    stub_graph: MagicMock,
) -> None:
    r = client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": "thesis?"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse(r.text)
    events = [name for name, _ in frames]

    # Each tool fires exactly one tool_call + one tool_result, paired up.
    tool_calls = [data for name, data in frames if name == "tool_call"]
    tool_results = [data for name, data in frames if name == "tool_result"]
    assert {tc["name"] for tc in tool_calls} == {"technical", "fundamental", "news"}
    assert {tr["name"] for tr in tool_results} == {"technical", "fundamental", "news"}

    # Tool labels are human-readable, not function names.
    assert {tc["label"] for tc in tool_calls} == {
        "Reading technicals",
        "Checking fundamentals",
        "Scanning news",
    }

    # Each result carries a real latency_ms + summary derived from the report.
    for tr in tool_results:
        assert isinstance(tr["latency_ms"], int) and tr["latency_ms"] >= 0
        assert tr["summary"]
        assert tr["ok"] is True

    # News summary uses the headline-bullet count, not raw line count.
    news_result = next(tr for tr in tool_results if tr["name"] == "news")
    assert news_result["summary"] == "3 headlines"

    # prose_chunk → thesis → done arrive after the tool events.
    assert "prose_chunk" in events
    assert "thesis" in events
    assert events[-1] == "done"

    # Final done payload carries real stats.
    done_data = frames[-1][1]
    assert done_data["confidence"] == 0.67
    assert done_data["tools_count"] == 3
    # The stub thesis has citations in setup (no), bull (1), bear (1), verdict (1).
    assert done_data["citations_count"] == 3


def test_thesis_event_payload_matches_pydantic_dump(
    client: TestClient,
    stub_graph: MagicMock,
) -> None:
    r = client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": ""})
    frames = _parse_sse(r.text)
    thesis_data = next(data for name, data in frames if name == "thesis")
    # Match the structured-thesis schema exactly so the frontend can deserialize
    # against the Pydantic shape without surprises.
    expected = stub_graph.thesis.model_dump()
    assert thesis_data == expected


def test_prose_chunks_split_setup_into_clauses(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The setup paragraph is chunked at sentence boundaries so the panel can
    render it progressively. Two-sentence setup → two prose_chunk events."""
    thesis = _stub_thesis(setup="First clause. Second clause.")

    def _fake_build(tools: dict[str, Any], **_kwargs: Any) -> Any:
        graph = MagicMock()
        graph.invoke.return_value = {
            "ticker": "NVDA",
            "intent": "thesis",
            "plan": [],
            "reports": {"technical": "stub"},  # non-empty so prose path runs
            "errors": {},
            "thesis": thesis,
            "quick_fact": None,
            "confidence": 0.5,
        }
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(chat_module, "default_report_tools", lambda: {})

    r = client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": ""})
    frames = _parse_sse(r.text)
    chunks = [data["delta"] for name, data in frames if name == "prose_chunk"]
    assert len(chunks) == 2
    assert chunks[0].startswith("First clause.")
    assert chunks[1].startswith("Second clause.")


def test_thesis_with_empty_bull_emits_full_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asymmetric thesis (empty bull or bear) must still serialize cleanly so
    the panel can render the asymmetry without a parse error."""
    thesis = Thesis(
        setup="One-sided framing.",
        bull_case=[],  # asymmetric — no bull case
        bear_case=["Bear concern (source: fundamental)"],
        verdict_stance="negative",
        verdict_action="Avoid; revisit on quarterly miss (source: fundamental).",
    )

    def _fake_build(tools: dict[str, Any], **_kwargs: Any) -> Any:
        graph = MagicMock()
        graph.invoke.return_value = {
            "ticker": "NVDA",
            "plan": [],
            "reports": {"technical": "stub"},
            "errors": {},
            "thesis": thesis,
            "confidence": 0.5,
        }
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(chat_module, "default_report_tools", lambda: {})

    r = client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": ""})
    frames = _parse_sse(r.text)
    thesis_data = next(data for name, data in frames if name == "thesis")
    assert thesis_data["bull_case"] == []
    assert thesis_data["bear_case"] == ["Bear concern (source: fundamental)"]
    assert thesis_data["verdict_stance"] == "negative"


def test_no_thesis_when_reports_empty_emits_done_with_zero(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Graph short-circuit (no reports gathered) → no thesis event, done has zeros.

    QNT-150: also asserts the surfaced ``tool-failed`` error carries a
    stable user-facing string (no raw graph-recorded detail) — the
    in-memory ``errors`` dict can hold arbitrary upstream strings (HTTP
    error bodies, internal URLs from agent.tools' ``[error] kind: detail``
    format) that must never reach the SSE client.
    """

    leaky_detail = "[error] http: 500 internal server error at http://api.internal:8000/v1/reports/technical/NVDA"

    def _fake_build(tools: dict[str, Any], **_kwargs: Any) -> Any:
        graph = MagicMock()
        graph.invoke.return_value = {
            "ticker": "NVDA",
            "intent": "thesis",
            "plan": [],
            "reports": {},
            "errors": {"technical": leaky_detail},
            "thesis": None,
            "quick_fact": None,
            "confidence": 0.0,
        }
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(chat_module, "default_report_tools", lambda: {})

    with caplog.at_level("WARNING", logger=chat_module.__name__):
        r = client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": ""})
    frames = _parse_sse(r.text)
    events = [name for name, _ in frames]
    # Required-tool failure surfaces as an error event before done.
    assert "error" in events
    assert "thesis" not in events
    # The error event detail is a stable user-facing string — the leaky
    # internal URL never reaches the SSE client.
    err = next(data for name, data in frames if name == "error")
    assert err["code"] == "tool-failed"
    assert "api.internal" not in err["detail"]
    assert "[error]" not in err["detail"]
    assert err["detail"] == "Reading technicals failed."
    # Server-side log captured the raw detail for debuggability.
    assert "api.internal" in caplog.text
    done_data = frames[-1][1]
    assert events[-1] == "done"
    assert done_data["tools_count"] == 0
    assert done_data["citations_count"] == 0
    assert done_data["confidence"] == 0.0


def test_agent_crash_emits_sanitized_error_event(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """QNT-150: a graph exception surfaces a stable user-facing string;
    raw exception details (class name, message, stack) only appear in
    server logs. The panel must not see internal LiteLLM auth tokens,
    URLs, or stack context that the SDK might attach to the exception."""

    def _fake_build(tools: dict[str, Any], **_kwargs: Any) -> Any:
        graph = MagicMock()
        # Realistic-looking sensitive content that must NOT leak to the SSE
        # client: API key tokens, internal hostnames, full traceback hints.
        graph.invoke.side_effect = RuntimeError(
            "401 Unauthorized; api_key=sk-secret-XYZ; "
            "url=http://litellm.internal:4000/v1/chat/completions"
        )
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(chat_module, "default_report_tools", lambda: {})

    with caplog.at_level("ERROR", logger=chat_module.__name__):
        r = client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": ""})
    frames = _parse_sse(r.text)
    err = next(data for name, data in frames if name == "error")
    assert err["code"] == "agent-failed"
    # User-facing detail is the stable sanitized string — no exception
    # class name, no token, no internal URL.
    assert err["detail"] == chat_module._ERROR_DETAIL_AGENT_FAILED
    assert "sk-secret" not in err["detail"]
    assert "litellm.internal" not in err["detail"]
    assert "RuntimeError" not in err["detail"]
    # Server-side log captured the full detail for debuggability —
    # ``logger.exception`` formats the traceback into ``caplog.text``.
    assert "sk-secret" in caplog.text
    assert frames[-1][0] == "done"


def test_optional_tool_failure_does_not_emit_error_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """News (in OPTIONAL_TOOLS) failures are silent — the agent's contract
    treats Qdrant/news outages as non-events, and the SSE stream must match."""
    thesis = _stub_thesis()

    def _fake_build(tools: dict[str, Any], **_kwargs: Any) -> Any:
        graph = MagicMock()
        graph.invoke.return_value = {
            "ticker": "NVDA",
            "intent": "thesis",
            "plan": ["technical", "news"],
            "reports": {"technical": "stub"},
            "errors": {"news": "qdrant-down"},  # optional — should be filtered
            "thesis": thesis,
            "quick_fact": None,
            "confidence": 0.5,
        }
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(chat_module, "default_report_tools", lambda: {})

    r = client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": ""})
    frames = _parse_sse(r.text)
    error_events = [data for name, data in frames if name == "error"]
    # No error events: optional-tool failures don't surface to the panel.
    assert error_events == []


def test_tools_disabled_skips_tool_calls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``tools_enabled: false`` must not invoke any report tool — the SSE
    stream contains zero tool_call events."""
    thesis = _stub_thesis(setup="Tools-off framing.")

    def _fake_build(tools: dict[str, Any], **_kwargs: Any) -> Any:
        # Assert the request really did skip tool registration.
        assert tools == {}
        graph = MagicMock()
        graph.invoke.return_value = {
            "ticker": "NVDA",
            "intent": "thesis",
            "plan": [],
            "reports": {"technical": "stub"},
            "errors": {},
            "thesis": thesis,
            "quick_fact": None,
            "confidence": 0.5,
        }
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(
        chat_module,
        "default_report_tools",
        lambda: {"technical": lambda t: "stub"},
    )

    r = client.post(
        "/api/v1/agent/chat",
        json={"ticker": "NVDA", "message": "", "tools_enabled": False},
    )
    frames = _parse_sse(r.text)
    events = [name for name, _ in frames]
    assert "tool_call" not in events
    assert "thesis" in events


def test_summary_uses_error_string_when_tool_returns_error_marker() -> None:
    """``[error] <kind>: <detail>`` from agent.tools surfaces verbatim in the
    tool_result summary so the panel can render the failure rather than a
    fake "0 lines"."""
    summary = chat_module._summarise_report(
        "technical",
        "[error] timeout: connection refused at http://api/api/v1/reports/technical/NVDA",
    )
    assert summary.startswith("[error]")


def test_quick_fact_intent_emits_quick_fact_event_not_thesis(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QNT-149: when the graph returns intent=quick_fact, the SSE stream
    emits an ``intent`` preamble and a ``quick_fact`` event; the thesis
    card is intentionally absent. AC: chat panel renders both shapes;
    thesis card hidden when absent."""
    quick_fact = QuickFactAnswer(
        answer="RSI sits at 62 (source: technical).",
        cited_value="62",
        source="technical",
    )

    def _fake_build(tools: dict[str, Any], **_kwargs: Any) -> Any:
        graph = MagicMock()
        graph.invoke.return_value = {
            "ticker": "NVDA",
            "intent": "quick_fact",
            "plan": ["technical"],
            "reports": {"technical": "## technical NVDA\nRSI: 62"},
            "errors": {},
            "thesis": None,
            "quick_fact": quick_fact,
            "confidence": 1.0,
        }
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(chat_module, "default_report_tools", lambda: {})

    r = client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": "What's the RSI?"})
    frames = _parse_sse(r.text)
    events = [name for name, _ in frames]

    # intent preamble fires before any payload
    assert "intent" in events
    intent_data = next(data for name, data in frames if name == "intent")
    assert intent_data["intent"] == "quick_fact"

    # The quick-fact payload arrives, the thesis card does NOT
    assert "quick_fact" in events
    assert "thesis" not in events
    qf_data = next(data for name, data in frames if name == "quick_fact")
    assert qf_data == quick_fact.model_dump()

    # Done payload carries intent + a non-zero citations count from the
    # inline (source: technical) cite in the answer prose.
    done_data = frames[-1][1]
    assert events[-1] == "done"
    assert done_data["intent"] == "quick_fact"
    assert done_data["citations_count"] >= 1
    assert done_data["confidence"] == 1.0


def test_quick_fact_failure_emits_conversational_redirect(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QNT-156: when the quick-fact synthesize path fails (provider
    misbehaved, structured-output crash), the graph populates
    ``state['conversational']`` with a deterministic ``domain_redirect``
    payload. The SSE stream surfaces it as a ``conversational`` event so
    the panel renders the redirect card instead of an error / blank
    panel. The OLD ``quick-fact-empty`` error code no longer fires —
    every synthesize-path failure goes through the conversational
    fallback."""
    from agent.conversational import domain_redirect
    from shared.tickers import TICKERS

    fallback = domain_redirect(
        reason="I had trouble pulling a single answer to that.",
        tickers=TICKERS,
        hint="quick_fact",
    )

    def _fake_build(tools: dict[str, Any], **_kwargs: Any) -> Any:
        graph = MagicMock()
        graph.invoke.return_value = {
            "ticker": "NVDA",
            "intent": "quick_fact",
            "plan": ["technical"],
            "reports": {"technical": "stub"},
            "errors": {},
            "thesis": None,
            "quick_fact": None,
            "comparison": None,
            "conversational": fallback,
            "confidence": 1.0,
        }
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(chat_module, "default_report_tools", lambda: {})

    r = client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": "What's the RSI?"})
    frames = _parse_sse(r.text)
    event_names = [name for name, _ in frames]
    # The conversational fallback is delivered, NOT a quick-fact-empty error.
    assert "conversational" in event_names
    assert not any(f.get("code") == "quick-fact-empty" for name, f in frames if name == "error")
    payload = next(data for name, data in frames if name == "conversational")
    assert payload["answer"]
    assert len(payload["suggestions"]) == 3


def test_quick_fact_citations_count_matches_helper() -> None:
    """``_count_quick_fact_citations`` honours inline (source: …) parens
    over the structured ``source`` field — same chip vocabulary the panel
    renders for the thesis path."""
    qf_inline = QuickFactAnswer(
        answer="RSI is 62 (source: technical).",
        cited_value="62",
        source="technical",
    )
    assert chat_module._count_quick_fact_citations(qf_inline) == 1

    qf_structured_only = QuickFactAnswer(
        answer="RSI is 62.",  # no inline cite
        cited_value="62",
        source="technical",
    )
    assert chat_module._count_quick_fact_citations(qf_structured_only) == 1

    qf_unsupported = QuickFactAnswer(
        answer="RSI not available in the supplied reports.",
        cited_value="",
        source=None,
    )
    assert chat_module._count_quick_fact_citations(qf_unsupported) == 0


def test_count_citations_matches_source_pattern() -> None:
    """``citations_count`` counts ``(source: …)`` parens across all sections —
    the canonical citation shape the synthesis prompt enforces."""
    thesis = Thesis(
        setup="Setup with no cite.",
        bull_case=[
            "First (source: technical)",
            "Second (source: fundamental|news)",
        ],
        bear_case=["Third (source: fundamental)"],
        verdict_stance="mixed",
        verdict_action="Levels (source: technical).",
    )
    assert chat_module._count_citations(thesis) == 4


def test_message_length_capped_at_validation_layer(client: TestClient) -> None:
    """Defensive cap on user message — Pydantic 422s a 5000-char prompt."""
    big = "x" * 5000
    r = client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": big})
    assert r.status_code == 422


# ─── QNT-159: intent event ordering (must precede tool_call) ─────────────


def test_intent_event_arrives_before_first_tool_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QNT-159: classify_node now emits the ``intent`` event via the SSE
    event_emitter the wrapper provides, so the panel sees the routing
    decision BEFORE the first tool_call frame. Without this fix, the
    streaming label flickered "streaming thesis…" for the entire
    tool-gathering phase regardless of which intent the classifier
    actually picked.

    The test fakes a build_graph whose invoke calls event_emitter first,
    then runs the wrapped tools (mirroring the real classify -> plan ->
    gather order). It asserts the SSE frame index of the first ``intent``
    event is less than the first ``tool_call`` index.
    """

    def _fake_build(tools: dict[str, Any], *, event_emitter: Any = None) -> Any:
        graph = MagicMock()

        def invoke(state: dict[str, Any]) -> dict[str, Any]:
            ticker = state["ticker"]
            # Mirror real classify_node behavior: emit intent BEFORE any
            # tool runs. The SSE wrapper's emitter posts onto the asyncio
            # queue ahead of the tool-call wrappers' posts.
            if event_emitter is not None:
                event_emitter("intent", {"intent": "thesis"})
            reports = {name: fn(ticker) for name, fn in tools.items()}
            return {
                "ticker": ticker,
                "intent": "thesis",
                "plan": list(tools.keys()),
                "reports": reports,
                "errors": {},
                "thesis": _stub_thesis(),
                "quick_fact": None,
                "comparison": None,
                "conversational": None,
                "confidence": 1.0,
            }

        graph.invoke.side_effect = invoke
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(
        chat_module,
        "default_report_tools",
        lambda: {
            "technical": lambda t: f"# tech {t}\n",
            "fundamental": lambda t: f"# fund {t}\n",
            "news": lambda t: f"# news {t}\n",
        },
    )

    r = client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": "Should I buy NVDA?"})
    frames = _parse_sse(r.text)
    events = [name for name, _ in frames]

    intent_indices = [i for i, (name, _) in enumerate(frames) if name == "intent"]
    tool_call_indices = [i for i, (name, _) in enumerate(frames) if name == "tool_call"]

    assert intent_indices, f"expected an intent event, got {events}"
    assert tool_call_indices, f"expected a tool_call event, got {events}"
    # First intent event must precede the first tool_call. The post-graph
    # safety-net intent emission may add a SECOND intent frame later in the
    # stream — that's fine; what matters is that the FIRST intent arrives
    # early so the streaming label is correct from frame 0.
    assert intent_indices[0] < tool_call_indices[0], (
        f"intent (index {intent_indices[0]}) must arrive BEFORE first "
        f"tool_call (index {tool_call_indices[0]}); event order was {events}"
    )


# ─── QNT-156: comparison + conversational SSE events ─────────────────────


def test_comparison_intent_emits_comparison_event_not_thesis(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QNT-156: when the graph returns intent=comparison, the SSE stream
    emits an ``intent`` preamble and a ``comparison`` event; the thesis
    + quick-fact cards are intentionally absent."""
    from agent.comparison import ComparisonAnswer, ComparisonSection, ComparisonValue

    comparison = ComparisonAnswer(
        sections=[
            ComparisonSection(
                ticker="NVDA",
                summary="NVDA trades at a premium (source: fundamental).",
                key_values=[ComparisonValue(label="P/E", value="50.0", source="fundamental")],
            ),
            ComparisonSection(
                ticker="AAPL",
                summary="AAPL trades closer to the market (source: fundamental).",
                key_values=[ComparisonValue(label="P/E", value="32.0", source="fundamental")],
            ),
        ],
        differences="NVDA carries a richer multiple than AAPL (source: fundamental).",
    )

    def _fake_build(tools: dict[str, Any], **_kwargs: Any) -> Any:  # noqa: ARG001
        graph = MagicMock()
        graph.invoke.return_value = {
            "ticker": "NVDA",
            "intent": "comparison",
            "plan": ["fundamental"],
            "reports": {"fundamental": "stub"},
            "reports_by_ticker": {"NVDA": {"fundamental": "stub"}, "AAPL": {"fundamental": "stub"}},
            "errors": {},
            "thesis": None,
            "quick_fact": None,
            "comparison": comparison,
            "conversational": None,
            "confidence": 1.0,
        }
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(chat_module, "default_report_tools", lambda: {})

    r = client.post(
        "/api/v1/agent/chat",
        json={"ticker": "NVDA", "message": "Compare NVDA vs AAPL on valuation."},
    )
    frames = _parse_sse(r.text)
    events = [name for name, _ in frames]

    assert "intent" in events
    intent_data = next(data for name, data in frames if name == "intent")
    assert intent_data["intent"] == "comparison"

    assert "comparison" in events
    assert "thesis" not in events
    assert "quick_fact" not in events
    cmp_data = next(data for name, data in frames if name == "comparison")
    assert [s["ticker"] for s in cmp_data["sections"]] == ["NVDA", "AAPL"]

    done_data = frames[-1][1]
    assert events[-1] == "done"
    assert done_data["intent"] == "comparison"
    # 2 cited values + 3 inline (source: fundamental) parens = 5.
    assert done_data["citations_count"] >= 2


def test_conversational_intent_emits_conversational_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QNT-156: a conversational intent emits a ``conversational`` event
    with the prose answer + suggestion list. No thesis / quick-fact /
    comparison cards fire. Citations count is 0 by contract."""
    from agent.conversational import ConversationalAnswer

    conversational = ConversationalAnswer(
        answer="I cover US equities — try one of the suggestions below.",
        suggestions=[
            "What's NVDA's RSI right now?",
            "How is MSFT valued relative to its earnings?",
            "Compare NVDA vs AAPL on valuation.",
        ],
    )

    def _fake_build(tools: dict[str, Any], **_kwargs: Any) -> Any:  # noqa: ARG001
        graph = MagicMock()
        graph.invoke.return_value = {
            "ticker": "NVDA",
            "intent": "conversational",
            "plan": [],
            "reports": {},
            "errors": {},
            "thesis": None,
            "quick_fact": None,
            "comparison": None,
            "conversational": conversational,
            "confidence": 0.0,
        }
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(chat_module, "default_report_tools", lambda: {})

    r = client.post(
        "/api/v1/agent/chat",
        json={"ticker": "NVDA", "message": "What can you do?"},
    )
    frames = _parse_sse(r.text)
    events = [name for name, _ in frames]

    assert "conversational" in events
    assert "thesis" not in events
    assert "quick_fact" not in events
    assert "comparison" not in events

    payload = next(data for name, data in frames if name == "conversational")
    assert payload["answer"]
    assert len(payload["suggestions"]) == 3

    done_data = frames[-1][1]
    assert done_data["intent"] == "conversational"
    # Conversational answers carry no citations by design.
    assert done_data["citations_count"] == 0


def test_thesis_failure_falls_back_to_conversational_redirect(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QNT-156: when the thesis synthesize path fails (structured-output
    crash, no reports gathered), the graph populates
    ``state['conversational']`` with a deterministic redirect. The SSE
    stream surfaces it via the ``conversational`` event so the panel
    renders the redirect card instead of an error / blank panel."""
    from agent.conversational import domain_redirect
    from shared.tickers import TICKERS

    fallback = domain_redirect(
        reason="I had trouble pulling a thesis together for that.",
        tickers=TICKERS,
        hint="thesis",
    )

    def _fake_build(tools: dict[str, Any], **_kwargs: Any) -> Any:  # noqa: ARG001
        graph = MagicMock()
        graph.invoke.return_value = {
            "ticker": "NVDA",
            "intent": "thesis",
            "plan": ["technical"],
            "reports": {"technical": "stub"},
            "errors": {},
            "thesis": None,
            "quick_fact": None,
            "comparison": None,
            "conversational": fallback,
            "confidence": 1.0,
        }
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(chat_module, "default_report_tools", lambda: {})

    r = client.post(
        "/api/v1/agent/chat",
        json={"ticker": "NVDA", "message": "Should I buy NVDA?"},
    )
    frames = _parse_sse(r.text)
    events = [name for name, _ in frames]

    assert "conversational" in events
    assert "thesis" not in events
    payload = next(data for name, data in frames if name == "conversational")
    assert payload["answer"]
    assert len(payload["suggestions"]) == 3


# ─── QNT-150: production-hardening (disconnect, timeout, race) ────────────


async def test_client_disconnect_cancels_runner_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-150: when the SSE consumer disconnects mid-stream, the
    generator's finally clause must cancel the runner_task so the worker
    coroutine is released. ``asyncio.to_thread`` cannot kill the
    underlying thread, but cancelling the asyncio task releases the queue
    + emitter callbacks so no further frames pile up.

    Test strategy: drive the async generator directly. Use a slow ``invoke``
    that blocks on a threading.Event so the runner task is still running
    when we ``aclose()`` the generator. Capture the task via a helper that
    overrides ``asyncio.create_task`` for the duration of the test.
    """
    invoke_started = threading.Event()
    invoke_unblock = threading.Event()

    def _slow_invoke(state: dict[str, Any]) -> dict[str, Any]:
        invoke_started.set()
        # Block until either the test releases us or 5s elapses (failsafe).
        # If the runner_task is properly cancelled, the asyncio side stops
        # waiting for us; the thread eventually exits via the timeout.
        invoke_unblock.wait(timeout=5)
        return {
            "ticker": state["ticker"],
            "intent": "thesis",
            "plan": [],
            "reports": {},
            "errors": {},
            "thesis": None,
            "quick_fact": None,
            "comparison": None,
            "conversational": None,
            "confidence": 0.0,
        }

    def _fake_build(tools: dict[str, Any], **_kwargs: Any) -> Any:
        graph = MagicMock()
        graph.invoke.side_effect = _slow_invoke
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(chat_module, "default_report_tools", lambda: {})

    # Capture the runner_task by intercepting asyncio.create_task while the
    # _stream coroutine runs. The test reads the captured handle to assert
    # the task was cancelled.
    captured: list[asyncio.Task[Any]] = []
    real_create_task = asyncio.create_task

    def _spy_create_task(coro: Any, **kw: Any) -> asyncio.Task[Any]:
        task = real_create_task(coro, **kw)
        captured.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", _spy_create_task)

    request = chat_module.ChatRequest(ticker="NVDA", message="")
    gen: AsyncGenerator[str, None] = chat_module._stream(request, "127.0.0.1")  # type: ignore[assignment]
    pull_task: asyncio.Task[str] | None = None
    try:
        # Pull frames until the runner thread is in flight. The drain loop
        # times out every 100ms, so we won't wait forever for a frame.
        pull_task = asyncio.create_task(gen.__anext__())
        # Wait for the worker thread to start (so the runner_task is "live").
        for _ in range(50):
            if invoke_started.is_set():
                break
            await asyncio.sleep(0.02)
        assert invoke_started.is_set(), "runner thread never started"
    finally:
        # Disconnect: close the generator. This raises GeneratorExit into
        # the drain loop, which must hit the finally clause and cancel the
        # runner_task.
        if pull_task is not None:
            pull_task.cancel()
            with contextlib.suppress(BaseException):
                await pull_task
        await gen.aclose()
        invoke_unblock.set()  # release the worker thread so pytest can shut down

    # The runner_task must have been cancelled (or completed). If the
    # finally clause never ran, the task would still be pending.
    runner_task = next(
        (t for t in captured if t is not pull_task and "to_thread" in repr(t.get_coro())),
        None,
    )
    assert runner_task is not None, "did not capture a runner_task"
    # Allow a brief moment for cancellation to land
    for _ in range(20):
        if runner_task.cancelled() or runner_task.done():
            break
        await asyncio.sleep(0.05)
    assert runner_task.cancelled() or runner_task.done(), (
        "runner_task was not cancelled / completed after generator close — "
        "the finally cleanup did not run"
    )


async def test_run_timeout_emits_agent_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-150: when the graph exceeds CHAT_RUN_TIMEOUT, the SSE stream
    surfaces a stable user-facing timeout error and a done frame, then
    cleanly tears down — without leaking the underlying exception."""
    from shared.config import settings

    # Override timeout to a short window so the test finishes fast — keep
    # comfortably above the drain loop's 0.1s queue.get cycle so the
    # deadline check fires reliably under load (CI box, GIL contention).
    monkeypatch.setattr(settings, "CHAT_RUN_TIMEOUT", 1.0)

    invoke_unblock = threading.Event()

    def _slow_invoke(state: dict[str, Any]) -> dict[str, Any]:
        # Block past CHAT_RUN_TIMEOUT
        invoke_unblock.wait(timeout=3)
        return {
            "ticker": state["ticker"],
            "intent": "thesis",
            "plan": [],
            "reports": {},
            "errors": {},
            "thesis": None,
            "quick_fact": None,
            "comparison": None,
            "conversational": None,
            "confidence": 0.0,
        }

    def _fake_build(tools: dict[str, Any], **_kwargs: Any) -> Any:
        graph = MagicMock()
        graph.invoke.side_effect = _slow_invoke
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(chat_module, "default_report_tools", lambda: {})

    with TestClient(main_module.app) as c:
        try:
            r = c.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": ""})
        finally:
            invoke_unblock.set()

    frames = _parse_sse(r.text)
    err = next((data for name, data in frames if name == "error"), None)
    assert err is not None, f"expected an error frame, got events {[n for n, _ in frames]}"
    assert err["code"] == "agent-timeout"
    assert err["detail"] == chat_module._ERROR_DETAIL_AGENT_TIMEOUT
    assert frames[-1][0] == "done"


def test_streaming_race_async_worker_drains_cleanly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QNT-150 AC6: introduce a small delay inside the worker between
    posting a tool_call and the tool_result. The drain loop's
    ``runner_task.done()`` check must NOT exit before the loop has
    drained the queue, otherwise late tool_result events would be lost.

    Without the ``or not queue.empty()`` clause in the drain condition,
    or with a worker that posts after ``runner_task`` is done, the race
    drops events. This test asserts both tool_call and tool_result land
    even when the worker posts the result after a small sleep.
    """

    def _delaying_tool(_t: str) -> str:
        # Force the wrapped tool to take measurable time so the
        # tool_result event lands after a non-trivial delay. The drain
        # loop must absorb this and still emit the result frame.
        time.sleep(0.05)
        return "## technical NVDA\nline\n"

    def _fake_build(tools: dict[str, Any], **_kwargs: Any) -> Any:
        graph = MagicMock()

        def invoke(state: dict[str, Any]) -> dict[str, Any]:
            ticker = state["ticker"]
            reports = {name: fn(ticker) for name, fn in tools.items()}
            return {
                "ticker": ticker,
                "intent": "thesis",
                "plan": list(tools.keys()),
                "reports": reports,
                "errors": {},
                "thesis": _stub_thesis(),
                "quick_fact": None,
                "comparison": None,
                "conversational": None,
                "confidence": 1.0,
            }

        graph.invoke.side_effect = invoke
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(
        chat_module,
        "default_report_tools",
        lambda: {"technical": _delaying_tool},
    )

    r = client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": ""})
    frames = _parse_sse(r.text)
    events = [name for name, _ in frames]
    # Both tool_call and tool_result must land; the drain loop's
    # ``or not queue.empty()`` clause is what guarantees this in the
    # current implementation, even when the result post races with
    # ``runner_task.done()``.
    assert events.count("tool_call") == 1
    assert events.count("tool_result") == 1
    assert events[-1] == "done"


def test_cors_post_allowed(client: TestClient) -> None:
    """CORS preflight for POST must succeed from the dev origin.

    QNT-161: the default CORS allowlist is dev-only (localhost:3001); prod
    sets ``CORS_ALLOWED_ORIGINS`` and ``CORS_ALLOWED_ORIGIN_REGEX`` to
    permit specific Vercel deploys. The Vercel-domain regex behaviour is
    covered by ``test_security.py`` which constructs a fresh app with the
    pinned regex set; this test just guards the dev path.
    """
    r = client.options(
        "/api/v1/agent/chat",
        headers={
            "Origin": "http://localhost:3001",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    allowed = r.headers["access-control-allow-methods"]
    assert "POST" in allowed
