"""Tests for the agent chat SSE endpoint (QNT-74).

The endpoint streams Server-Sent Events while a LangGraph run executes. Tests
patch ``build_graph`` + ``default_report_tools`` so the agent never calls the
real LiteLLM proxy or hits ClickHouse — the contract under test is the SSE
event sequence + payload shape, not the agent's reasoning.

Each TestClient call buffers the full streaming body, so we re-parse the
``event: …\\ndata: …`` frames to assert the canonical sequence.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any
from unittest.mock import MagicMock

import pytest
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

    def _fake_build(tools: dict[str, Any]) -> Any:
        graph = MagicMock()

        def invoke(state: dict[str, Any]) -> dict[str, Any]:
            ticker = state["ticker"]
            reports: dict[str, str] = {}
            for name, fn in tools.items():
                reports[name] = fn(ticker)
            return {
                "ticker": ticker,
                "plan": list(tools.keys()),
                "reports": reports,
                "errors": {},
                "thesis": thesis,
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

    def _fake_build(tools: dict[str, Any]) -> Any:
        graph = MagicMock()
        graph.invoke.return_value = {
            "ticker": "NVDA",
            "plan": [],
            "reports": {"technical": "stub"},  # non-empty so prose path runs
            "errors": {},
            "thesis": thesis,
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

    def _fake_build(tools: dict[str, Any]) -> Any:
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
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Graph short-circuit (no reports gathered) → no thesis event, done has zeros."""

    def _fake_build(tools: dict[str, Any]) -> Any:
        graph = MagicMock()
        graph.invoke.return_value = {
            "ticker": "NVDA",
            "plan": [],
            "reports": {},
            "errors": {"technical": "tool-not-registered"},
            "thesis": None,
            "confidence": 0.0,
        }
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(chat_module, "default_report_tools", lambda: {})

    r = client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": ""})
    frames = _parse_sse(r.text)
    events = [name for name, _ in frames]
    # Required-tool failure surfaces as an error event before done.
    assert "error" in events
    assert "thesis" not in events
    done_data = frames[-1][1]
    assert events[-1] == "done"
    assert done_data["tools_count"] == 0
    assert done_data["citations_count"] == 0
    assert done_data["confidence"] == 0.0


def test_agent_crash_emits_error_event(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A graph exception (e.g. LLM provider failure) surfaces as an SSE error
    event followed by done — the panel must not crash on a crashed agent."""

    def _fake_build(tools: dict[str, Any]) -> Any:
        graph = MagicMock()
        graph.invoke.side_effect = RuntimeError("boom")
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(chat_module, "default_report_tools", lambda: {})

    r = client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": ""})
    frames = _parse_sse(r.text)
    err = next(data for name, data in frames if name == "error")
    assert err["code"] == "agent-failed"
    assert "boom" in err["detail"]
    assert frames[-1][0] == "done"


def test_optional_tool_failure_does_not_emit_error_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """News (in OPTIONAL_TOOLS) failures are silent — the agent's contract
    treats Qdrant/news outages as non-events, and the SSE stream must match."""
    thesis = _stub_thesis()

    def _fake_build(tools: dict[str, Any]) -> Any:
        graph = MagicMock()
        graph.invoke.return_value = {
            "ticker": "NVDA",
            "plan": ["technical", "news"],
            "reports": {"technical": "stub"},
            "errors": {"news": "qdrant-down"},  # optional — should be filtered
            "thesis": thesis,
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

    def _fake_build(tools: dict[str, Any]) -> Any:
        # Assert the request really did skip tool registration.
        assert tools == {}
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


def test_cors_post_allowed(client: TestClient) -> None:
    """CORS preflight for POST must succeed (Vercel → Hetzner cross-origin)."""
    r = client.options(
        "/api/v1/agent/chat",
        headers={
            "Origin": "https://equity-data-agent-git-feat.vercel.app",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    allowed = r.headers["access-control-allow-methods"]
    assert "POST" in allowed
