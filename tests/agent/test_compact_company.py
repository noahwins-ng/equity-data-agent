"""QNT-220 (#8): the agent consumes the compact company report on the
thesis/comparison/exploration hot path and the full report on focused asks.

These assert the *agent path* (AC1) -- that ``build_graph(..., compact_company_tool=...)``
swaps the compact callable into the ``company`` slot for the force-include
intents (QNT-175) while focused fundamental/technical/news keep the full report.
The compact rendering itself is covered by ``tests/api/test_templates.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from agent import graph as graph_module
from agent.focused import FocusedAnalysis
from agent.graph import ThesisPlan, build_graph
from agent.thesis import Thesis
from langchain_core.messages import AIMessage

from ._thesis_factory import make_thesis


class _Runnable:
    def __init__(self, result: object | None = None) -> None:
        self.invoke = MagicMock(return_value=result)
        self.with_retry = MagicMock(return_value=self)


class _LLM:
    """Returns a structured payload per schema; streams for narrate."""

    def __init__(self) -> None:
        self.focused = FocusedAnalysis(
            focus="fundamental",
            summary="Valuation read (source: fundamental).",
            key_points=["P/E anchors the read (source: fundamental)."],
            cited_values=[],
            verdict=None,
            existing_development=None,
            positive_catalysts=[],
            negative_catalysts=[],
        )

    def with_structured_output(self, schema: type) -> _Runnable:
        if schema is ThesisPlan:
            return _Runnable(
                ThesisPlan(
                    tools=["company", "fundamental", "technical", "news"],
                    rationale="Broad thesis needs every report.",
                )
            )
        if schema is Thesis:
            return _Runnable(make_thesis())
        if schema is FocusedAnalysis:
            return _Runnable(self.focused)
        return _Runnable(None)  # comparison -> fallback redirect (tool calls still happen)

    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        return iter([AIMessage(content="Narrative.")])

    def invoke(self, *_args: Any, **_kwargs: Any) -> Any:
        return AIMessage(content="fundamental")


def _full_and_compact() -> tuple[MagicMock, MagicMock]:
    return (
        MagicMock(return_value="## COMPANY (full)\nBusiness + competitors + watch"),
        MagicMock(return_value="## COMPANY (compact)\nBusiness + risks"),
    )


def _other_tools() -> dict[str, MagicMock]:
    return {
        "technical": MagicMock(return_value="## technical\n"),
        "fundamental": MagicMock(return_value="## fundamental\n"),
        "news": MagicMock(return_value="## news\n"),
    }


def _patch(monkeypatch: pytest.MonkeyPatch, intent: str) -> None:
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda *_a, **_k: (intent, "heuristic", False, False, ""),
    )
    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_k: _LLM())


def test_thesis_consumes_compact_company(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, "thesis")
    full, compact = _full_and_compact()
    tools = {"company": full, **_other_tools()}

    result = build_graph(tools, compact_company_tool=compact).invoke(
        {"ticker": "AAPL", "question": "Give me an AAPL thesis."}
    )

    assert compact.call_count == 1
    assert full.call_count == 0
    assert "compact" in result["reports"]["company"]


def test_comparison_consumes_compact_company_for_both_tickers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, "comparison")
    full, compact = _full_and_compact()
    tools = {"company": full, **_other_tools()}

    build_graph(tools, compact_company_tool=compact).invoke(
        {"ticker": "AAPL", "question": "Compare AAPL and MSFT."}
    )

    # One compact company fetch per compared ticker; full never used.
    assert compact.call_count == 2
    assert full.call_count == 0


def test_focused_fundamental_keeps_full_company(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, "fundamental")
    full, compact = _full_and_compact()
    tools = {"company": full, **_other_tools()}

    result = build_graph(tools, compact_company_tool=compact).invoke(
        {"ticker": "AAPL", "question": "What does AAPL's fundamental picture look like?"}
    )

    assert full.call_count == 1
    assert compact.call_count == 0
    assert "full" in result["reports"]["company"]


def test_exploration_uses_compact_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-220 follow-up: a broad scan always renders as the exploration card,
    and ``exploration`` is in _COMPACT_COMPANY_INTENTS, so a non-news-led
    [company, news] scan gets the COMPACT company report (the lever #8 token
    savings now extend to the exploration hot path)."""
    _patch(monkeypatch, "news")
    full, compact = _full_and_compact()
    tools = {"company": full, **_other_tools()}

    result = build_graph(tools, compact_company_tool=compact).invoke(
        {"ticker": "AAPL", "question": "What stands out on AAPL?"}
    )

    assert "explore_supervisor" in result["intent_path"]
    assert result["plan"] == ["company", "news"]
    assert result["intent"] == "exploration"
    assert compact.call_count == 1
    assert full.call_count == 0


def test_no_compact_tool_uses_full_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing CLI / eval / test callers pass no compact tool -> full report
    is used on every path (backwards compatible)."""
    _patch(monkeypatch, "thesis")
    full, _compact = _full_and_compact()
    tools = {"company": full, **_other_tools()}

    result = build_graph(tools).invoke({"ticker": "AAPL", "question": "Give me an AAPL thesis."})

    assert full.call_count == 1
    assert "full" in result["reports"]["company"]
