"""Tests for the golden-set runner (QNT-67 eval (b)).

The runner orchestrates four moving parts (graph, hallucination check,
tool-call check, judge / cosine scoring). Tests stub the graph + judge so
they exercise the orchestration logic offline — the actual graph is
covered by tests/agent/test_graph.py and the live wiring is exercised by
``uv run python -m agent.evals`` against the running CLI.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from agent.evals import golden_set
from agent.evals.golden_set import (
    HISTORY_FIELDS,
    EvalOutcome,
    GoldenRecord,
    append_history,
    is_failing,
    run_record,
    summarise,
)
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis


def _record(rid: str = "test-1", ticker: str = "NVDA") -> GoldenRecord:
    return GoldenRecord(
        id=rid,
        ticker=ticker,
        question="Is NVDA a buy?",
        expected_tools=("technical", "fundamental"),
        reference_thesis="Reference thesis with technical and fundamental coverage.",
    )


def _thesis(setup: str, bull: list[str] | None = None) -> Thesis:
    """Build a minimal Thesis whose markdown render contains the seed text."""
    return Thesis(
        setup=setup,
        bull_case=bull or [],
        bear_case=[],
        verdict_stance="constructive",
        verdict_action="Hold pending eval.",
    )


@pytest.fixture
def stub_graph(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, Any]], None]:
    """Replace the real graph with a configurable mock.

    Returns a setter that updates the mock's invoke return value, so each
    test can dictate the ``state`` the runner sees without rebuilding the
    fixture.
    """
    state: dict[str, Any] = {
        "thesis": _thesis(
            "RSI is 72.5 (source: technical). P/E 25 (source: fundamental).",
        ),
        "reports": {
            "technical": "RSI is 72.5 today",
            "fundamental": "P/E is 25, latest EPS 0.81",
        },
        "errors": {},
    }
    graph = MagicMock()
    graph.invoke.return_value = state

    def fake_build_graph(tools: dict[str, Any]) -> MagicMock:
        # Touch each tool so the recorder records its call — without this
        # the tool-call check would always see an empty recorder.
        for fn in tools.values():
            fn("NVDA")
        return graph

    monkeypatch.setattr(golden_set, "build_graph", fake_build_graph)
    monkeypatch.setattr(
        golden_set,
        "default_report_tools",
        lambda: {
            "technical": lambda _t: "tech",
            "fundamental": lambda _t: "fund",
            "news": lambda _t: "news",
        },
    )

    def setter(new_state: dict[str, Any]) -> None:
        graph.invoke.return_value = new_state

    return setter


@pytest.fixture
def stub_judge(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock judge.score so tests don't need a live LLM."""
    fake = MagicMock(return_value=8)
    monkeypatch.setattr(golden_set, "judge_score_fn", fake)
    return fake


class TestRunRecord:
    def test_clean_run_produces_passing_outcome(
        self, stub_graph: Callable[[dict[str, Any]], None], stub_judge: MagicMock
    ) -> None:
        outcome = run_record(_record())
        assert outcome.hallucination_ok
        assert outcome.tool_call_ok
        assert outcome.judge_score == 8
        assert "technical" in outcome.actual_tools
        assert "fundamental" in outcome.actual_tools
        assert outcome.elapsed_ms >= 0

    def test_unsupported_number_produces_hallucination_failure(
        self,
        stub_graph: Callable[[dict[str, Any]], None],
        stub_judge: MagicMock,  # noqa: ARG002
    ) -> None:
        stub_graph(
            {
                "thesis": _thesis("P/E is 999 (source: fundamental)."),
                "reports": {"fundamental": "P/E is 25"},
                "errors": {},
            }
        )
        outcome = run_record(_record())
        assert not outcome.hallucination_ok
        assert "999" in outcome.hallucination_reason

    def test_graph_exception_produces_failed_outcome_not_raise(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_judge: MagicMock,  # noqa: ARG002
    ) -> None:
        # If one ticker breaks, the sweep must keep going; run_record swallows
        # the graph exception and returns a failing outcome.
        def boom(_tools: object) -> object:
            raise RuntimeError("graph broken")

        monkeypatch.setattr(golden_set, "build_graph", boom)
        monkeypatch.setattr(golden_set, "default_report_tools", lambda: {})
        outcome = run_record(_record())
        assert not outcome.hallucination_ok
        assert not outcome.tool_call_ok
        assert "graph error" in outcome.hallucination_reason

    def test_quick_fact_state_is_rendered_for_scorers(
        self,
        stub_graph: Callable[[dict[str, Any]], None],
        stub_judge: MagicMock,  # noqa: ARG002
    ) -> None:
        """QNT-149: when synthesize populates ``quick_fact`` instead of
        ``thesis``, the eval harness must render that to markdown so the
        hallucination + judge + cosine scorers still see real text. AC:
        QNT-67 hallucination + QNT-128 golden-set evals pass on the new
        paths."""
        stub_graph(
            {
                "thesis": None,
                "quick_fact": QuickFactAnswer(
                    answer="RSI is 72.5 (source: technical).",
                    cited_value="72.5",
                    source="technical",
                ),
                "reports": {"technical": "RSI is 72.5 today"},
                "errors": {},
            }
        )
        outcome = run_record(_record())
        # Hallucination check sees the cited value via to_markdown.
        assert outcome.hallucination_ok, outcome.hallucination_reason
        # Cosine measured the rendered text, not the empty string.
        assert outcome.cosine > 0
        # Tool-call check still runs against the recorder, independent of
        # output shape.
        assert "technical" in outcome.actual_tools

    def test_quick_fact_with_unsupported_number_fails_hallucination(
        self,
        stub_graph: Callable[[dict[str, Any]], None],
        stub_judge: MagicMock,  # noqa: ARG002
    ) -> None:
        """A quick-fact answer that quotes a value not in the reports must
        fail the hallucination scorer the same way a thesis would."""
        stub_graph(
            {
                "thesis": None,
                "quick_fact": QuickFactAnswer(
                    answer="RSI is 999 (source: technical).",
                    cited_value="999",
                    source="technical",
                ),
                "reports": {"technical": "RSI is 72.5"},
                "errors": {},
            }
        )
        outcome = run_record(_record())
        assert not outcome.hallucination_ok
        assert "999" in outcome.hallucination_reason


class TestAppendHistory:
    def test_writes_header_on_first_run(self, tmp_path: Path) -> None:
        history = tmp_path / "history.csv"
        outcome = EvalOutcome(
            record=_record(),
            thesis="t",
            actual_tools=("technical",),
            hallucination_ok=True,
            hallucination_reason="clean",
            tool_call_ok=True,
            tool_call_reason="clean",
            judge_score=7,
            cosine=0.42,
            elapsed_ms=123,
        )
        append_history([outcome], run_id="r1", history_path=history)

        rows = list(csv.DictReader(history.open()))
        assert len(rows) == 1
        assert set(rows[0].keys()) == set(HISTORY_FIELDS)
        assert rows[0]["ticker"] == "NVDA"
        assert rows[0]["judge_score"] == "7"
        assert rows[0]["hallucination_ok"] == "1"

    def test_appends_to_existing_file_without_duplicating_header(self, tmp_path: Path) -> None:
        history = tmp_path / "history.csv"
        outcome = EvalOutcome(
            record=_record(),
            thesis="t",
            actual_tools=(),
            hallucination_ok=True,
            hallucination_reason="clean",
            tool_call_ok=True,
            tool_call_reason="clean",
            judge_score=None,
            cosine=0.0,
            elapsed_ms=1,
        )
        append_history([outcome], run_id="r1", history_path=history)
        append_history([outcome], run_id="r2", history_path=history)
        rows = list(csv.DictReader(history.open()))
        assert len(rows) == 2
        assert {r["run_id"] for r in rows} == {"r1", "r2"}

    def test_judge_score_none_serialises_as_empty_string(self, tmp_path: Path) -> None:
        history = tmp_path / "history.csv"
        outcome = EvalOutcome(
            record=_record(),
            thesis="",
            actual_tools=(),
            hallucination_ok=True,
            hallucination_reason="clean",
            tool_call_ok=True,
            tool_call_reason="clean",
            judge_score=None,
            cosine=0.0,
            elapsed_ms=0,
        )
        append_history([outcome], run_id="rN", history_path=history)
        text = history.read_text()
        # Empty string between commas, NOT the literal "None" — keeps the CSV
        # parseable as a missing-judge row downstream.
        assert ",,0.0," in text


class TestGate:
    def test_is_failing_only_on_hard_contract_violations(self) -> None:
        ok = EvalOutcome(
            record=_record(),
            thesis="",
            actual_tools=(),
            hallucination_ok=True,
            hallucination_reason="clean",
            tool_call_ok=True,
            tool_call_reason="clean",
            judge_score=2,  # bad judge but soft signal
            cosine=0.0,
            elapsed_ms=0,
        )
        bad_halluc = EvalOutcome(
            record=_record(),
            thesis="",
            actual_tools=(),
            hallucination_ok=False,
            hallucination_reason="unsupported: 99",
            tool_call_ok=True,
            tool_call_reason="clean",
            judge_score=10,
            cosine=1.0,
            elapsed_ms=0,
        )
        assert not is_failing([ok])
        assert is_failing([bad_halluc])

    def test_summarise_handles_empty_input(self) -> None:
        assert summarise([]) == "no records evaluated"

    def test_is_failing_treats_empty_outcomes_as_failure(self) -> None:
        # A malformed YAML stub or an --only filter that strips every record
        # would otherwise let any([]) silently pass. Surface zero-records as
        # a hard failure so a broken golden file can't masquerade as clean.
        assert is_failing([])
