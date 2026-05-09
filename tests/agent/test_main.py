"""Tests for the agent CLI entry point (QNT-60, QNT-133, QNT-149).

Mocks ``build_graph`` so the CLI is tested in isolation from the API layer.
The graph itself is covered by tests/agent/test_graph.py.

QNT-133 changed the contract: the graph state holds a structured ``Thesis``
rather than a flat string. The CLI is responsible for re-rendering it to
markdown for stdout / ``--output``, so the tests stub ``state["thesis"]``
with real ``Thesis`` instances and assert the rendered markdown surfaces
in the expected places.

QNT-149 added a second response shape (quick-fact). The CLI renders
whichever one ``state['intent']`` selects so callers piping to files
don't have to branch.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from agent import __main__ as cli
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis


def _stub_thesis(setup: str = "NVDA looks attractive on momentum.") -> Thesis:
    """Minimal Thesis for CLI tests — only ``setup`` text is asserted on."""
    return Thesis(
        setup=setup,
        bull_case=["RSI 62 (source: technical)"],
        bear_case=[],
        verdict_stance="constructive",
        verdict_action="Trim above SMA50 (source: technical).",
    )


@pytest.fixture
def stub_graph(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    graph = MagicMock()
    graph.invoke.return_value = {
        "thesis": _stub_thesis(),
        "confidence": 0.67,
        "errors": {},
    }
    monkeypatch.setattr(cli, "build_graph", MagicMock(return_value=graph))
    monkeypatch.setattr(cli, "default_report_tools", MagicMock(return_value={}))
    return graph


def test_analyze_unknown_ticker_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["analyze", "ZZZZ"]) == 1
    err = capsys.readouterr().err
    assert "Unknown ticker: ZZZZ" in err


def test_analyze_success_prints_thesis_and_exits_0(
    stub_graph: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["analyze", "NVDA"]) == 0
    out = capsys.readouterr()
    # CLI re-renders the structured Thesis to markdown — assert all four
    # section headings + the seed text from the stub appear on stdout.
    assert "## Setup" in out.out
    assert "NVDA looks attractive on momentum." in out.out
    assert "## Bull Case" in out.out
    assert "## Bear Case" in out.out
    assert "## Verdict" in out.out
    assert "confidence=0.67" in out.err
    # QNT-181: CLI now passes config={"callbacks": [handler]} when Langfuse
    # is enabled, or config={} when disabled. Tests run with keys stripped
    # so config defaults to {}; assert the state shape and accept any config.
    stub_graph.invoke.assert_called_once()
    call = stub_graph.invoke.call_args
    assert call.args == ({"ticker": "NVDA"},)
    # config kwarg present (empty when disabled, populated when enabled).
    assert "config" in call.kwargs


def test_analyze_quick_fact_intent_prints_short_answer(
    stub_graph: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    """QNT-149: when the graph picks intent=quick_fact, the CLI renders the
    QuickFactAnswer markdown — short answer + Value line — and skips the
    four-section thesis output entirely."""
    stub_graph.invoke.return_value = {
        "intent": "quick_fact",
        "thesis": None,
        "quick_fact": QuickFactAnswer(
            answer="RSI sits at 62 (source: technical).",
            cited_value="62",
            source="technical",
        ),
        "confidence": 1.0,
        "errors": {},
    }
    assert cli.main(["analyze", "NVDA"]) == 0
    out = capsys.readouterr()
    assert "RSI sits at 62" in out.out
    assert "**Value:** 62 (source: technical)" in out.out
    # No thesis sections rendered.
    assert "## Setup" not in out.out
    assert "## Bull Case" not in out.out
    assert "intent=quick_fact" in out.err


def test_analyze_missing_thesis_exits_1(
    stub_graph: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Short-circuit case: gather produced nothing, no Thesis in state."""
    stub_graph.invoke.return_value = {"confidence": 0.0, "errors": {}}
    assert cli.main(["analyze", "NVDA"]) == 1
    assert "No answer produced" in capsys.readouterr().err


def test_analyze_none_thesis_exits_1(
    stub_graph: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Defensive: a structured-output failure surfaces as ``thesis=None``;
    CLI must treat that the same as the missing-key case."""
    stub_graph.invoke.return_value = {"thesis": None, "confidence": 0.0, "errors": {}}
    assert cli.main(["analyze", "NVDA"]) == 1
    assert "No answer produced" in capsys.readouterr().err


@pytest.mark.usefixtures("stub_graph")
def test_analyze_writes_output_file(tmp_path: Path) -> None:
    out_file = tmp_path / "thesis.md"
    assert cli.main(["analyze", "NVDA", "--output", str(out_file)]) == 0
    written = out_file.read_text()
    # The file holds the same markdown the CLI prints — section headings + body.
    assert "## Setup" in written
    assert "NVDA looks attractive on momentum." in written
    assert "## Verdict" in written


def test_analyze_surfaces_tool_errors_to_stderr(
    stub_graph: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_graph.invoke.return_value = {
        "thesis": _stub_thesis("Partial framing."),
        "confidence": 0.33,
        "errors": {"technical": "tool-not-registered"},
    }
    assert cli.main(["analyze", "NVDA"]) == 0
    err = capsys.readouterr().err
    assert "[warn] technical: tool-not-registered" in err


def test_unhandled_exception_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(tools: object) -> object:
        raise RuntimeError(f"graph compilation failed (tools={type(tools).__name__})")

    monkeypatch.setattr(cli, "build_graph", boom)
    assert cli.main(["analyze", "NVDA"]) == 1


@pytest.mark.usefixtures("stub_graph")
def test_analyze_unwritable_output_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad_path = tmp_path / "does-not-exist" / "thesis.md"
    assert cli.main(["analyze", "NVDA", "--output", str(bad_path)]) == 1
    err = capsys.readouterr().err
    assert "[error] cannot write" in err
    assert str(bad_path) in err
