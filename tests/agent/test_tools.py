"""Tests for agent.tools (QNT-57).

Covers the never-raise contract for every failure path (HTTP error,
timeout, unreachable endpoint, unknown ticker, malformed JSON, empty
results) and the happy-path wiring through ``shared.settings.API_BASE_URL``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from agent import graph as graph_module
from agent import tools as tools_module
from agent.tools import (
    default_report_tools,
    get_fundamental_report,
    get_news_report,
    get_summary_report,
    get_technical_report,
    search_news,
)


class _Recorder:
    """Capture ``(url, params)`` pairs for each httpx.get call and return a
    user-supplied ``httpx.Response`` (or raise a user-supplied exception).
    Exists because ``httpx.MockTransport`` only plugs into an ``httpx.Client``
    — the tools call the module-level ``httpx.get`` shortcut, so the test
    patches that directly via ``monkeypatch``."""

    def __init__(self, responder: Callable[[str, dict[str, Any] | None], httpx.Response]) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._responder = responder

    def __call__(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,  # noqa: ARG002 — accepted, unused by the recorder
    ) -> httpx.Response:
        self.calls.append((url, params))
        return self._responder(url, params)


def _install_recorder(
    monkeypatch: pytest.MonkeyPatch,
    responder: Callable[[str, dict[str, Any] | None], httpx.Response],
) -> _Recorder:
    recorder = _Recorder(responder)
    monkeypatch.setattr(tools_module.httpx, "get", recorder)
    return recorder


def _ok(text: str, *, content_type: str = "text/plain") -> httpx.Response:
    return httpx.Response(200, text=text, headers={"content-type": content_type})


def _json_ok(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


@pytest.fixture(autouse=True)
def _pin_api_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the base URL so the recorded URLs are deterministic regardless of
    the developer's local ``.env``."""
    from shared.config import settings

    monkeypatch.setattr(settings, "API_BASE_URL", "http://test-api:8000")


# ───────────────────────── Report tools — happy path ─────────────────────────


@pytest.mark.parametrize(
    ("tool", "endpoint"),
    [
        (get_summary_report, "/api/v1/reports/summary/NVDA"),
        (get_technical_report, "/api/v1/reports/technical/NVDA"),
        (get_fundamental_report, "/api/v1/reports/fundamental/NVDA"),
        (get_news_report, "/api/v1/reports/news/NVDA"),
    ],
)
def test_report_tool_returns_body_on_200(
    tool: Callable[[str], str],
    endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _install_recorder(monkeypatch, lambda _u, _p: _ok("NVDA report body"))
    assert tool("NVDA") == "NVDA report body"
    url, params = recorder.calls[0]
    assert url == f"http://test-api:8000{endpoint}"
    assert params is None


def test_report_tool_uppercases_ticker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ticker is validated case-insensitively and the upstream URL uses the
    canonical uppercase form — symmetric with the report endpoints'
    ``ticker.upper()`` in api/routers/reports.py."""
    recorder = _install_recorder(monkeypatch, lambda _u, _p: _ok("ok"))
    assert get_technical_report("nvda") == "ok"
    assert recorder.calls[0][0].endswith("/NVDA")


# ───────────────────────── Report tools — failure paths ──────────────────────


def test_unknown_ticker_returns_error_string_without_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-validation avoids a 404 round-trip for bogus tickers."""
    recorder = _install_recorder(
        monkeypatch, lambda _u, _p: pytest.fail("httpx should not be called")
    )
    result = get_technical_report("XXXX")
    assert result.startswith("[error] unknown-ticker")
    assert "XXXX" in result
    assert recorder.calls == []


def test_http_4xx_returns_error_string(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_recorder(monkeypatch, lambda _u, _p: httpx.Response(404, text="not found"))
    result = get_technical_report("NVDA")
    assert result.startswith("[error] http-404")
    assert "not found" in result


def test_http_5xx_returns_error_string(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_recorder(
        monkeypatch, lambda _u, _p: httpx.Response(500, text="upstream failure\nline2")
    )
    result = get_fundamental_report("NVDA")
    assert result.startswith("[error] http-500")
    # Only the first non-empty line of the body is echoed back — keeps the
    # error short and prevents LLM prompt stuffing.
    assert "upstream failure" in result
    assert "line2" not in result


def test_http_error_with_empty_body_falls_back_to_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_recorder(monkeypatch, lambda _u, _p: httpx.Response(503, text=""))
    result = get_news_report("NVDA")
    assert result.startswith("[error] http-503")
    assert "/api/v1/reports/news/NVDA" in result


def test_timeout_returns_error_string(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_url: str, _params: Any) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout")

    _install_recorder(monkeypatch, _raise)
    result = get_technical_report("NVDA")
    assert result.startswith("[error] timeout")
    assert "read timeout" in result


def test_connection_error_returns_error_string(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_url: str, _params: Any) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_recorder(monkeypatch, _raise)
    result = get_summary_report("NVDA")
    assert result.startswith("[error] unreachable")
    assert "ConnectError" in result


def test_remote_protocol_error_returns_unreachable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``RemoteProtocolError`` is an ``HTTPError`` subclass, so the
    ``unreachable`` branch owns it."""

    def _raise(_url: str, _params: Any) -> httpx.Response:
        raise httpx.RemoteProtocolError("server closed connection")

    _install_recorder(monkeypatch, _raise)
    result = get_news_report("NVDA")
    assert result.startswith("[error] unreachable")
    assert "RemoteProtocolError" in result


def test_non_http_error_is_caught_and_converted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``httpx.InvalidURL`` and friends sit OUTSIDE the ``HTTPError`` tree —
    the specific ``except HTTPError`` branches don't cover them, so the
    blanket fallback has to. Without this, a misconfigured ``API_BASE_URL``
    would raise through the tool and burn both graph retry attempts on what
    is actually a config bug."""

    def _raise(_url: str, _params: Any) -> httpx.Response:
        raise httpx.InvalidURL("malformed URL")

    _install_recorder(monkeypatch, _raise)
    result = get_news_report("NVDA")
    assert result.startswith("[error] unexpected")
    assert "InvalidURL" in result


def test_report_tool_never_raises_on_arbitrary_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-braces: a plain ``Exception`` from anywhere below httpx.get
    (e.g. a future client-middleware crash) must still come back as a
    descriptive string — the never-raise contract is absolute."""

    def _raise(_url: str, _params: Any) -> httpx.Response:
        raise RuntimeError("something weird")

    _install_recorder(monkeypatch, _raise)
    result = get_summary_report("NVDA")
    assert result.startswith("[error] unexpected")
    assert "RuntimeError" in result


def test_search_news_degrades_on_non_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same InvalidURL gap applies to search_news; its never-raise contract
    degrades to ``"[]"`` for every failure mode, including ones outside the
    HTTPError tree."""

    def _raise(_url: str, _params: Any) -> httpx.Response:
        raise httpx.InvalidURL("malformed URL")

    _install_recorder(monkeypatch, _raise)
    assert search_news("NVDA", "earnings") == "[]"


# ───────────────────────── search_news — happy path ──────────────────────────


def test_search_news_returns_pretty_json(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [
        {
            "headline": "NVDA hits record",
            "source": "Reuters",
            "date": "2026-04-20",
            "score": 0.91,
            "url": "https://example.com/a",
        }
    ]
    recorder = _install_recorder(monkeypatch, lambda _u, _p: _json_ok(payload))

    result = search_news("NVDA", "earnings surprise")

    assert result == json.dumps(payload, indent=2)
    url, params = recorder.calls[0]
    assert url == "http://test-api:8000/api/v1/search/news"
    assert params == {"ticker": "NVDA", "query": "earnings surprise", "limit": 5}


def test_search_news_uppercases_ticker_in_query(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _install_recorder(monkeypatch, lambda _u, _p: _json_ok([{"x": 1}]))
    search_news("nvda", "ai chips")
    assert recorder.calls[0][1] == {"ticker": "NVDA", "query": "ai chips", "limit": 5}


# ───────────────────────── search_news — degraded paths ──────────────────────


def test_search_news_empty_results_returns_empty_array_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_recorder(monkeypatch, lambda _u, _p: _json_ok([]))
    assert search_news("NVDA", "no matches") == "[]"


def test_search_news_http_error_degrades_to_empty_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_recorder(monkeypatch, lambda _u, _p: httpx.Response(500, text="boom"))
    assert search_news("NVDA", "earnings") == "[]"


def test_search_news_timeout_degrades_to_empty_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_url: str, _params: Any) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    _install_recorder(monkeypatch, _raise)
    assert search_news("NVDA", "earnings") == "[]"


def test_search_news_malformed_json_degrades_to_empty_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_recorder(
        monkeypatch,
        lambda _u, _p: httpx.Response(200, text="<!doctype html>not json"),
    )
    assert search_news("NVDA", "earnings") == "[]"


def test_search_news_unknown_ticker_returns_empty_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _install_recorder(
        monkeypatch, lambda _u, _p: pytest.fail("httpx should not be called")
    )
    assert search_news("XXXX", "anything") == "[]"
    assert recorder.calls == []


@pytest.mark.parametrize("query", ["", "x" * 513])
def test_search_news_invalid_query_returns_empty_array(
    query: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _install_recorder(
        monkeypatch, lambda _u, _p: pytest.fail("httpx should not be called")
    )
    assert search_news("NVDA", query) == "[]"
    assert recorder.calls == []


# ───────────────────────── Graph integration ─────────────────────────────────


def test_default_report_tools_registers_three_planable_tools() -> None:
    """ADR-007 / QNT-56 constant REPORT_TOOLS = (technical, fundamental, news).
    The default tool mapping must cover all three — if it drifts, plan-node
    surface contracts break."""
    tools = default_report_tools()
    assert set(tools) == set(graph_module.REPORT_TOOLS)
    for tool in tools.values():
        assert callable(tool)


def test_default_report_tools_compose_with_build_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: build_graph(default_report_tools()) compiles and the
    gather node calls each tool with the run's ticker."""

    from agent.thesis import Thesis

    expected_thesis = Thesis(
        setup="Setup (source: technical).",
        bull_case=["bull"],
        bear_case=[],
        verdict_stance="constructive",
        verdict_action="Hold.",
    )

    class _StubLLM:
        """Two channels: ``invoke`` for plan, ``with_structured_output`` for
        the synthesize call (QNT-133)."""

        def __init__(self, plan_response: str, thesis: Thesis) -> None:
            from langchain_core.messages import AIMessage

            self._plan_response = AIMessage(content=plan_response)
            self._thesis = thesis

        def invoke(self, _prompt: str) -> Any:
            return self._plan_response

        def with_structured_output(self, _schema: object) -> Any:
            outer = self

            class _StructuredRunnable:
                def invoke(self, _prompt: object) -> Any:
                    return outer._thesis

            return _StructuredRunnable()

    stub = _StubLLM("technical, fundamental, news", expected_thesis)
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: stub)
    monkeypatch.setattr(
        graph_module.langfuse,
        "traced_invoke",
        lambda llm_, prompt, *, name: llm_.invoke(prompt),
    )
    _install_recorder(monkeypatch, lambda url, _p: _ok(f"body for {url}"))

    graph = graph_module.build_graph(default_report_tools())
    result = graph.invoke({"ticker": "NVDA", "question": "Is NVDA a buy?"})

    assert set(result["reports"]) == {"technical", "fundamental", "news"}
    # Each gather call hit the API_BASE_URL + /api/v1/reports/<kind>/NVDA path.
    for kind, body in result["reports"].items():
        assert body.endswith(f"/api/v1/reports/{kind}/NVDA")
    assert result["thesis"] is expected_thesis
    assert result["confidence"] == 1.0
