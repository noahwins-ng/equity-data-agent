"""QNT-282 (item 2): offline coverage for the unconstrained-baseline ablation.

The live 44-question sweep makes paid LLM calls (run manually). These tests pin
the ablation LOGIC without any network: the ungrounded prompt carries no reports
and no grounding rule, and the SAME hallucination scorer flags the ungrounded
answer's from-memory figures while leaving the grounded answer clean.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.evals import baseline_eval
from agent.evals.baseline_eval import (
    RecordOutcome,
    build_ungrounded_prompt,
    run_record,
    score_pair,
    summarize,
)


def test_ungrounded_prompt_carries_no_reports_or_grounding_rule() -> None:
    prompt = build_ungrounded_prompt("NVDA", "What's the P/E and RSI?")
    assert "NVDA" in prompt
    assert "What's the P/E and RSI?" in prompt
    # The ablation: none of the grounding machinery may leak into the prompt.
    lowered = prompt.lower()
    for banned in ("must appear", "do not invent", "verbatim", "source:", "(source", "report"):
        assert banned not in lowered, f"grounding leaked into ungrounded prompt: {banned!r}"


def test_blank_question_still_produces_a_prompt() -> None:
    prompt = build_ungrounded_prompt("AAPL", "   ")
    assert "AAPL" in prompt
    assert "investment thesis" in prompt.lower()


def test_score_pair_flags_ungrounded_fabrication_but_not_grounded() -> None:
    reports = ["AAPL trades at a P/E of 32 with an RSI of 48 and a 45% gross margin."]
    # Constrained: every figure copied from the report -> clean.
    constrained = "AAPL sits at a P/E of 32 (source: fundamental), RSI 48."
    # Unconstrained: figures invented from memory -> none in the report.
    unconstrained = "AAPL trades at a P/E of 88, RSI 71, and a 61% gross margin."

    outcome = score_pair(
        record_id="aapl-x",
        ticker="AAPL",
        constrained_text=constrained,
        unconstrained_text=unconstrained,
        flat_reports=reports,
    )

    assert outcome.constrained_ok is True
    assert outcome.constrained_unsupported == ()
    assert outcome.unconstrained_ok is False
    # The invented figures are what the scorer flags.
    assert "88" in outcome.unconstrained_unsupported
    assert "71" in outcome.unconstrained_unsupported


def test_run_record_wires_ungrounded_llm_through_the_scorer(monkeypatch) -> None:
    """run_record must (a) take the constrained answer + reports from the graph and
    (b) score the injected LLM's ungrounded text against those reports."""

    class _StubGraph:
        def invoke(self, _inputs: dict[str, object]) -> dict[str, object]:
            return {
                "answer": SimpleNamespace(
                    to_markdown=lambda: "MSFT P/E is 30 (source: fundamental)."
                ),
                "reports": {"fundamental": "MSFT trades at a P/E of 30."},
            }

    # Isolate from the network: stub the tool wiring and the graph build.
    monkeypatch.setattr(baseline_eval, "default_report_tools", lambda: {})
    monkeypatch.setattr(baseline_eval, "get_company_report_compact", lambda _t: "")
    monkeypatch.setattr(baseline_eval, "build_graph", lambda *a, **k: _StubGraph())

    # A canned ungrounded generation with an invented multiple (99, not 30).
    stub_llm = SimpleNamespace(
        invoke=lambda _prompt: SimpleNamespace(content="MSFT trades at a P/E of 99.")
    )
    record = SimpleNamespace(id="msft-x", ticker="MSFT", question="What's the P/E?")

    outcome = run_record(record, llm=stub_llm)  # type: ignore[arg-type]  # stub stands in for GoldenRecord

    assert outcome.constrained_ok is True  # 30 is in the report
    assert outcome.unconstrained_ok is False  # 99 is not
    assert "99" in outcome.unconstrained_unsupported


def test_summarize_counts_and_rates() -> None:
    outcomes = [
        RecordOutcome(
            record_id="a",
            ticker="AAPL",
            constrained_text="",
            unconstrained_text="",
            constrained_unsupported=(),
            unconstrained_unsupported=("88", "71"),
            constrained_numbers=3,
            unconstrained_numbers=4,
        ),
        RecordOutcome(
            record_id="b",
            ticker="MSFT",
            constrained_text="",
            unconstrained_text="",
            constrained_unsupported=(),
            unconstrained_unsupported=("99",),
            constrained_numbers=2,
            unconstrained_numbers=2,
        ),
    ]
    summary = summarize(outcomes)
    assert summary.n == 2
    assert summary.constrained_clean == 2
    assert summary.unconstrained_clean == 0
    assert summary.constrained_fabricated == 0
    assert summary.unconstrained_fabricated == 3
    assert summary.constrained_rate == 0.0
    assert summary.unconstrained_rate == 3 / 6
    # render() is a human summary -- just make sure it interpolates without error.
    assert "unconstrained" in summary.render()
