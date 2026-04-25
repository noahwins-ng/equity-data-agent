"""Tests for the agent CLI entry point (QNT-60).

Mocks ``build_graph`` so the CLI is tested in isolation from the API layer.
The graph itself is covered by tests/agent/test_graph.py.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from agent import __main__ as cli


@pytest.fixture
def stub_graph(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    graph = MagicMock()
    graph.invoke.return_value = {
        "thesis": "NVDA looks attractive on momentum.",
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
    assert "NVDA looks attractive on momentum." in out.out
    assert "confidence=0.67" in out.err
    stub_graph.invoke.assert_called_once_with({"ticker": "NVDA"})


def test_analyze_empty_thesis_exits_1(
    stub_graph: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_graph.invoke.return_value = {"thesis": "", "confidence": 0.0, "errors": {}}
    assert cli.main(["analyze", "NVDA"]) == 1
    assert "No thesis produced" in capsys.readouterr().err


@pytest.mark.usefixtures("stub_graph")
def test_analyze_writes_output_file(tmp_path: Path) -> None:
    out_file = tmp_path / "thesis.md"
    assert cli.main(["analyze", "NVDA", "--output", str(out_file)]) == 0
    assert out_file.read_text().strip() == "NVDA looks attractive on momentum."


def test_analyze_surfaces_tool_errors_to_stderr(
    stub_graph: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_graph.invoke.return_value = {
        "thesis": "Partial thesis.",
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
