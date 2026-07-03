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
    CONTAMINATION_LATENCY_MS,
    GOLDEN_FIELDS,
    EvalOutcome,
    GoldenRecord,
    append_history,
    is_failing,
    provider_pressure_warning,
    run_all,
    run_record,
    summarise,
)
from agent.evals.judge import JudgeScore
from agent.evals.spine import ENVELOPE_FIELDS
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis

from .._thesis_factory import make_thesis


def _judge(composite: int = 8) -> JudgeScore:
    """Build a JudgeScore where all axes equal the composite target."""
    return JudgeScore(
        faithfulness=composite,
        structure=composite,
        correctness=composite,
        analyst_logic=composite,
    )


def _record(rid: str = "test-1", ticker: str = "NVDA") -> GoldenRecord:
    return GoldenRecord(
        id=rid,
        ticker=ticker,
        question="Is NVDA a buy?",
        expected_tools=("technical", "fundamental"),
        reference_thesis="Reference thesis with technical and fundamental coverage.",
    )


def _thesis(summary: str, supports: list[str] | None = None) -> Thesis:
    """Build a minimal v2 Thesis whose markdown render contains the seed text.

    ``summary`` lands in the technical aspect's summary so the markdown
    grep target is easy to predict for hallucination tests.
    """
    return make_thesis(
        company_summary=summary,
        supports=supports if supports is not None else [],
        challenges=[],
        verdict="Neutral",
        verdict_rationale="Premium paired with Uptrend (source: technical).",
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

    def fake_build_graph(
        tools: dict[str, Any], **_kwargs: Any
    ) -> MagicMock:  # QNT-220: accepts compact_company_tool kwarg
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
    fake = MagicMock(return_value=_judge(8))
    monkeypatch.setattr(golden_set, "judge_score_fn", fake)
    return fake


class TestRunRecord:
    def test_clean_run_produces_passing_outcome(
        self, stub_graph: Callable[[dict[str, Any]], None], stub_judge: MagicMock
    ) -> None:
        outcome = run_record(_record())
        assert outcome.hallucination_ok
        assert outcome.tool_call_ok
        assert outcome.judge_score is not None
        assert outcome.judge_score.composite == 8
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
        # the graph exception and returns a failing outcome. Accept **kwargs so
        # the stub tolerates build_graph's compact_company_tool= call path
        # regardless of routing (matches the sibling boom stubs below) -- without
        # it the stub raises TypeError instead of the intended RuntimeError when
        # the record routes through the company-report path, a test-order flake.
        def boom(_tools: object, **_kwargs: object) -> object:
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

    def test_forbidden_substring_triggers_hallucination_failure(
        self,
        stub_graph: Callable[[dict[str, Any]], None],
        stub_judge: MagicMock,  # noqa: ARG002
    ) -> None:
        """QNT-184: a thesis that contains a forbidden substring must fail
        hallucination_ok even if the numeric hallucination check passes."""
        # Use a thesis with no numbers so only the forbidden-substring path
        # can fire — prevents the numeric hallucination check from also
        # failing and masking which assertion is actually gating.
        stub_graph(
            {
                "thesis": _thesis("All indicators agreeing (source: technical)."),
                "reports": {"technical": "All indicators in agreement today."},
                "errors": {},
            }
        )
        record = GoldenRecord(
            id="test-forbidden",
            ticker="NVDA",
            question="Is NVDA trending up?",
            expected_tools=("technical",),
            reference_thesis="A useful answer cites RSI and trend.",
            forbidden_substrings=("indicators agree",),
        )
        outcome = run_record(record)
        assert not outcome.hallucination_ok
        assert "forbidden" in outcome.hallucination_reason
        assert "indicators agree" in outcome.hallucination_reason

    def test_forbidden_substring_absent_does_not_fail(
        self,
        stub_graph: Callable[[dict[str, Any]], None],
        stub_judge: MagicMock,  # noqa: ARG002
    ) -> None:
        """A thesis that does NOT contain the forbidden substring must pass."""
        stub_graph(
            {
                "thesis": _thesis(
                    "RSI is 72.5, trend up (source: technical). MACD above signal line.",
                    supports=["RSI 72.5 (source: technical)"],
                ),
                "reports": {"technical": "RSI is 72.5 today. MACD above signal line."},
                "errors": {},
            }
        )
        record = GoldenRecord(
            id="test-forbidden-absent",
            ticker="NVDA",
            question="Is NVDA trending up?",
            expected_tools=("technical",),
            reference_thesis="A useful answer cites RSI.",
            forbidden_substrings=("indicators agree",),
        )
        outcome = run_record(record)
        assert outcome.hallucination_ok, outcome.hallucination_reason

    def test_forbidden_aspect_support_in_supports_fails(
        self,
        stub_graph: Callable[[dict[str, Any]], None],
        stub_judge: MagicMock,  # noqa: ARG002
    ) -> None:
        """QNT-208 (was QNT-183): a thesis whose technical.supports contains
        a forbidden_aspect_support substring must fail hallucination_ok even
        if challenges uses the same term correctly."""
        stub_graph(
            {
                "thesis": make_thesis(
                    supports=[
                        "RSI 71.6 overbought but bullish continuation in uptrend"
                        " (source: technical)"
                    ],
                    challenges=["RSI pulling back from overbought territory (source: technical)"],
                    verdict_rationale="Premium plus Uptrend tension (source: technical).",
                ),
                "reports": {"technical": "RSI is 71.6 overbought territory today"},
                "errors": {},
            }
        )
        record = GoldenRecord(
            id="test-forbidden-supports",
            ticker="NVDA",
            question="Is NVDA overbought?",
            expected_tools=("technical",),
            reference_thesis="Challenges cite overbought RSI; supports omits it.",
            forbidden_aspect_support_substrings={"technical": ("overbought",)},
        )
        outcome = run_record(record)
        assert not outcome.hallucination_ok
        assert "forbidden in supports" in outcome.hallucination_reason
        assert "overbought" in outcome.hallucination_reason

    def test_forbidden_aspect_support_in_challenges_only_passes(
        self,
        stub_graph: Callable[[dict[str, Any]], None],
        stub_judge: MagicMock,  # noqa: ARG002
    ) -> None:
        """QNT-208: the same term appearing only in technical.challenges must
        NOT fail -- challenges is the correct home for overbought RSI."""
        stub_graph(
            {
                "thesis": make_thesis(
                    supports=["Uptrend intact (source: technical)"],
                    challenges=["RSI pulling back from overbought territory (source: technical)"],
                    verdict_rationale="Uptrend label with overbought caution (source: technical).",
                ),
                "reports": {"technical": "RSI is 71.6 overbought territory today"},
                "errors": {},
            }
        )
        record = GoldenRecord(
            id="test-forbidden-supports-absent",
            ticker="NVDA",
            question="Is NVDA overbought?",
            expected_tools=("technical",),
            reference_thesis="Challenges cite overbought RSI; supports omits it.",
            forbidden_aspect_support_substrings={"technical": ("overbought",)},
        )
        outcome = run_record(record)
        assert outcome.hallucination_ok, outcome.hallucination_reason

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
            judge_score=_judge(7),
            cosine=0.42,
            elapsed_ms=123,
        )
        append_history([outcome], run_id="r1", history_path=history)

        rows = list(csv.DictReader(history.open()))
        assert len(rows) == 1
        # QNT-293 follow-up: golden writes its own file -- envelope + golden cols.
        assert set(rows[0].keys()) == set(ENVELOPE_FIELDS) | set(GOLDEN_FIELDS)
        assert rows[0]["suite"] == "golden"
        assert rows[0]["ticker"] == "NVDA"
        assert rows[0]["composite"] == "7"
        assert rows[0]["faithfulness"] == "7"
        assert rows[0]["analyst_logic"] == "7"
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
        rows = list(csv.DictReader((tmp_path / "history.csv").open()))
        assert rows[0]["faithfulness"] == ""
        assert rows[0]["structure"] == ""
        assert rows[0]["correctness"] == ""
        assert rows[0]["analyst_logic"] == ""
        assert rows[0]["composite"] == ""

    def test_verdict_label_consistent_serialises_by_flag(self, tmp_path: Path) -> None:
        """QNT-302: True -> '1', False -> '0', None -> '' — and the value lands
        under its own column name on read (guards the mid-list-insert corruption
        that would shift every later column)."""
        history = tmp_path / "history.csv"

        def _mk(flag: bool | None) -> EvalOutcome:
            return EvalOutcome(
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
                verdict_label_consistent=flag,
            )

        append_history([_mk(True), _mk(False), _mk(None)], run_id="r", history_path=history)
        rows = list(csv.DictReader(history.open()))
        assert [r["verdict_label_consistent"] for r in rows] == ["1", "0", ""]
        # Columns after the new one must still align (eval_type is not shifted).
        assert {r["eval_type"] for r in rows} == {"structured"}


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
            judge_score=_judge(2),  # bad judge but soft signal
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
            judge_score=_judge(10),
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


def _outcome(
    *,
    rid: str = "rec",
    hallucination_ok: bool = True,
    tool_call_ok: bool = True,
    provider_error: bool = False,
    reason: str = "clean",
    elapsed_ms: int = 1000,
    verdict_label_consistent: bool | None = None,
) -> EvalOutcome:
    return EvalOutcome(
        record=_record(rid),
        thesis="",
        actual_tools=(),
        hallucination_ok=hallucination_ok,
        hallucination_reason=reason,
        tool_call_ok=tool_call_ok,
        tool_call_reason=reason,
        judge_score=None,
        cosine=0.0,
        elapsed_ms=elapsed_ms,
        provider_error=provider_error,
        verdict_label_consistent=verdict_label_consistent,
    )


def test_summarise_reports_verdict_label_consistent_over_thesis_rows_only() -> None:
    """QNT-302: the advisory aggregate counts only structured-thesis rows (flag
    is not None); non-thesis shapes (flag None) are excluded from the ratio."""
    text = summarise(
        [
            _outcome(rid="a", verdict_label_consistent=True),
            _outcome(rid="b", verdict_label_consistent=False),
            _outcome(rid="c", verdict_label_consistent=None),  # non-thesis: excluded
        ]
    )
    assert "verdict_label_consistent: 1/2" in text


def test_summarise_verdict_label_consistent_na_when_no_thesis_rows() -> None:
    text = summarise([_outcome(rid="a", verdict_label_consistent=None)])
    assert "verdict_label_consistent: n/a" in text


class TestProviderPressure:
    """QNT-234: provider-pressure failures must be distinguished from regressions."""

    def test_provider_pressure_graph_error_is_flagged_not_a_contract_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_judge: MagicMock,  # noqa: ARG002
    ) -> None:
        # A Groq timeout surfacing out of graph.invoke must be tagged
        # provider_error, with a "provider:" reason -- not the old generic
        # "graph error" that read like a code regression.
        class APITimeoutError(Exception):
            pass

        def boom(_tools: object, **_kwargs: object) -> object:
            raise APITimeoutError("Request timed out.")

        monkeypatch.setattr(golden_set, "build_graph", boom)
        monkeypatch.setattr(golden_set, "default_report_tools", lambda: {})
        outcome = run_record(_record())
        assert outcome.provider_error
        assert "provider" in outcome.hallucination_reason
        assert "APITimeoutError" in outcome.hallucination_reason

    def test_real_graph_error_stays_a_contract_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_judge: MagicMock,  # noqa: ARG002
    ) -> None:
        # A genuine bug (not provider pressure) keeps the old behaviour: not a
        # provider_error, still a hard failure with the "graph error" reason.
        def boom(_tools: object, **_kwargs: object) -> object:
            raise RuntimeError("graph broken")

        monkeypatch.setattr(golden_set, "build_graph", boom)
        monkeypatch.setattr(golden_set, "default_report_tools", lambda: {})
        outcome = run_record(_record())
        assert not outcome.provider_error
        assert "graph error" in outcome.hallucination_reason

    def test_is_failing_ignores_provider_error_rows(self) -> None:
        # The AC3 scenario: a provider-pressure failure ALONGSIDE a passing real
        # record must not gate the exit code -- the routing fix isn't blocked by
        # free-tier capacity when real records still measured clean.
        clean = _outcome(rid="clean")
        provider = _outcome(
            rid="prov",
            hallucination_ok=False,
            tool_call_ok=False,
            provider_error=True,
            reason="provider: RateLimitError",
        )
        assert not is_failing([clean, provider])
        # ... but a real contract failure alongside it still gates.
        regression = _outcome(rid="bad", hallucination_ok=False, reason="unsupported: 99")
        assert is_failing([clean, provider, regression])

    def test_is_failing_gates_when_every_record_is_provider_error(self) -> None:
        # A full Groq outage measures zero usable rows -- that's "evaluated
        # nothing", not a clean pass. Gate it like the empty-outcomes case so a
        # total outage can't masquerade as green CI.
        provider_a = _outcome(rid="a", provider_error=True, reason="provider: RateLimitError")
        provider_b = _outcome(rid="b", provider_error=True, reason="provider: timeout")
        assert is_failing([provider_a, provider_b])

    def test_warning_fires_on_provider_error(self) -> None:
        warning = provider_pressure_warning([_outcome(provider_error=True)])
        assert warning is not None
        assert "PROVIDER-PRESSURE" in warning
        assert "1 provider error" in warning

    def test_warning_fires_on_slow_record(self) -> None:
        warning = provider_pressure_warning([_outcome(elapsed_ms=CONTAMINATION_LATENCY_MS + 1)])
        assert warning is not None
        assert "timeout-ceiling floor" in warning

    def test_no_warning_on_clean_run(self) -> None:
        assert provider_pressure_warning([_outcome(elapsed_ms=2000)]) is None

    def test_summarise_leads_with_banner_and_counts_provider_failures(self) -> None:
        text = summarise([_outcome(provider_error=True, reason="provider: RateLimitError")])
        assert text.startswith("PROVIDER-PRESSURE")
        assert "provider_failures: 1/1" in text
        assert "[P" in text  # per-record marker, not [hT]

    def test_run_all_excludes_provider_rows_from_history(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Two records: one clean, one provider error. Only the clean one lands
        # in history.csv so the committed quality trend isn't polluted.
        clean = _outcome(rid="clean")
        provider = _outcome(rid="prov", provider_error=True, reason="provider: timeout")
        outcomes = iter([clean, provider])
        monkeypatch.setattr(golden_set, "run_record", lambda _rec, **_kw: next(outcomes))
        monkeypatch.setattr(
            golden_set,
            "load_goldens",
            lambda: [_record("clean"), _record("prov")],
        )
        history = tmp_path / "history.csv"
        _, returned = run_all(history_path=history)
        # Both outcomes are returned to the caller (summary/gate see them)...
        assert len(returned) == 2
        # ...but only the measured (non-provider) row is committed to history.
        rows = list(csv.DictReader(history.open()))
        assert len(rows) == 1
        assert rows[0]["question_id"] == "clean"
        # The clean row passed, so the mixed run does not gate on the provider row.
        assert not is_failing(returned)
