"""QNT-282 (item 2): unconstrained-baseline ablation for the README Results table.

For each golden question we score TWO answers against the SAME gathered reports,
with the SAME (unchanged) hallucination scorer:

  constrained    -- the real grounded agent (``build_graph`` -> ``invoke``); the
                    figure the README already reports (numbers copied from reports).
  unconstrained  -- one ungrounded analyst generation: the SAME model, but the
                    reports are never shown to it and the "every number must appear
                    in a report" rule is removed, so it answers from parametric
                    memory. Any figure it emits is scored against reports it never
                    saw.

The delta isolates what the report-grounding architecture buys: the constrained
agent fabricates ~nothing; the unconstrained baseline invents figures against the
very same supported set.

This is a MANUAL / dev sweep -- it makes real (paid) LLM calls and is deliberately
NOT wired into CI. Run (tunnel + LiteLLM proxy + API up)::

    uv run python -m agent.evals.baseline_eval

The scorer, reports, and question set are identical to ``golden_set.py`` -- only
the answer under test differs -- so the two rates are directly comparable.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agent.evals.golden_set import GoldenRecord, load_goldens
from agent.evals.hallucination import check as check_hallucination
from agent.evals.hallucination import extract_numbers
from agent.evals.tool_calls import wrap_with_recorder
from agent.graph import build_graph
from agent.llm import get_llm
from agent.tools import default_report_tools, get_company_report_compact

logger = logging.getLogger(__name__)

# The ablation prompt. Deliberately supplies NO report text and states NO
# "every number must appear in a report" rule -- the two grounding mechanisms
# the constrained agent relies on. The model answers from parametric memory, so
# each figure it emits is scored against reports it never saw.
UNGROUNDED_PROMPT = (
    "You are a senior equity analyst. Answer the user's question about {ticker} in "
    "3-5 sentences, as you would for a client. Reference the specific figures a real "
    "analyst would cite -- price levels, valuation multiples (P/E, P/S), margins, "
    "growth rates, technical readings (RSI, moving averages) -- stated as concrete "
    "numbers.\n\nQuestion: {question}"
)


def build_ungrounded_prompt(ticker: str, question: str) -> str:
    """The ungrounded analyst prompt for one question. Pure -- offline-testable."""
    return UNGROUNDED_PROMPT.format(
        ticker=ticker, question=question.strip() or "Give me an investment thesis."
    )


@dataclass(frozen=True)
class RecordOutcome:
    """One golden scored under both regimes against the same reports."""

    record_id: str
    ticker: str
    constrained_text: str
    unconstrained_text: str
    constrained_unsupported: tuple[str, ...]
    unconstrained_unsupported: tuple[str, ...]
    constrained_numbers: int
    unconstrained_numbers: int

    @property
    def constrained_ok(self) -> bool:
        """No fabricated numbers -- every figure traced to a report."""
        return not self.constrained_unsupported

    @property
    def unconstrained_ok(self) -> bool:
        return not self.unconstrained_unsupported


def score_pair(
    *,
    record_id: str,
    ticker: str,
    constrained_text: str,
    unconstrained_text: str,
    flat_reports: Sequence[str],
) -> RecordOutcome:
    """Score both answers with the SAME unchanged hallucination scorer against the
    SAME reports. Pure -- no I/O, offline-testable."""
    constrained = check_hallucination(constrained_text, flat_reports)
    unconstrained = check_hallucination(unconstrained_text, flat_reports)
    return RecordOutcome(
        record_id=record_id,
        ticker=ticker,
        constrained_text=constrained_text,
        unconstrained_text=unconstrained_text,
        constrained_unsupported=constrained.unsupported,
        unconstrained_unsupported=unconstrained.unsupported,
        constrained_numbers=len(extract_numbers(constrained_text)),
        unconstrained_numbers=len(extract_numbers(unconstrained_text)),
    )


def _flat_reports(state: dict[str, object]) -> list[str]:
    """The report corpus the hallucination scorer scans -- identical flattening to
    ``golden_set.run_record`` (comparison runs gather per ticker)."""
    reports = state.get("reports") or {}
    reports_by_ticker = state.get("reports_by_ticker") or {}
    flat: list[str] = []
    if isinstance(reports_by_ticker, dict) and reports_by_ticker:
        for ticker_reports in reports_by_ticker.values():
            if isinstance(ticker_reports, dict):
                flat.extend(str(value) for value in ticker_reports.values())
        return flat
    if isinstance(reports, dict):
        flat.extend(str(value) for value in reports.values())
    return flat


def run_record(record: GoldenRecord, *, llm: Any | None = None) -> RecordOutcome:
    """Gather reports via the real agent (constrained answer + report corpus), then
    generate one ungrounded answer, and score both against the same reports.

    ``llm`` is injectable so tests can supply a canned generation; production passes
    ``get_llm()`` (the same default alias synthesis uses)."""
    wrapped, recorder = wrap_with_recorder(default_report_tools())
    compact_wrapped, _ = wrap_with_recorder(
        {"company": get_company_report_compact}, recorder=recorder
    )
    graph = build_graph(wrapped, compact_company_tool=compact_wrapped["company"])
    state = graph.invoke({"ticker": record.ticker, "question": record.question})

    answer_obj = state.get("answer")
    to_markdown = getattr(answer_obj, "to_markdown", None)
    constrained_text = str(to_markdown()) if callable(to_markdown) else ""
    flat_reports = _flat_reports(state)

    active_llm = llm if llm is not None else get_llm()
    response = active_llm.invoke(build_ungrounded_prompt(record.ticker, record.question))
    content = getattr(response, "content", response)
    unconstrained_text = content if isinstance(content, str) else str(content)

    return score_pair(
        record_id=record.id,
        ticker=record.ticker,
        constrained_text=constrained_text,
        unconstrained_text=unconstrained_text,
        flat_reports=flat_reports,
    )


@dataclass(frozen=True)
class BaselineSummary:
    """Aggregate over a sweep. ``*_clean`` counts answers with zero fabricated
    numbers; ``*_fabricated`` / ``*_numbers`` drive the fabricated-number rate."""

    n: int
    constrained_clean: int
    unconstrained_clean: int
    constrained_fabricated: int
    unconstrained_fabricated: int
    constrained_numbers: int
    unconstrained_numbers: int

    @staticmethod
    def _rate(fabricated: int, total: int) -> float:
        return fabricated / total if total else 0.0

    @property
    def constrained_rate(self) -> float:
        return self._rate(self.constrained_fabricated, self.constrained_numbers)

    @property
    def unconstrained_rate(self) -> float:
        return self._rate(self.unconstrained_fabricated, self.unconstrained_numbers)

    def render(self) -> str:
        return (
            "Unconstrained-baseline ablation (QNT-282 item 2)\n"
            f"  questions:              {self.n}\n"
            f"  constrained  clean:     {self.constrained_clean}/{self.n} "
            f"({self.constrained_fabricated} fabricated / {self.constrained_numbers} numbers "
            f"= {self.constrained_rate:.1%})\n"
            f"  unconstrained clean:    {self.unconstrained_clean}/{self.n} "
            f"({self.unconstrained_fabricated} fabricated / {self.unconstrained_numbers} numbers "
            f"= {self.unconstrained_rate:.1%})\n"
        )


def summarize(outcomes: Sequence[RecordOutcome]) -> BaselineSummary:
    return BaselineSummary(
        n=len(outcomes),
        constrained_clean=sum(o.constrained_ok for o in outcomes),
        unconstrained_clean=sum(o.unconstrained_ok for o in outcomes),
        constrained_fabricated=sum(len(o.constrained_unsupported) for o in outcomes),
        unconstrained_fabricated=sum(len(o.unconstrained_unsupported) for o in outcomes),
        constrained_numbers=sum(o.constrained_numbers for o in outcomes),
        unconstrained_numbers=sum(o.unconstrained_numbers for o in outcomes),
    )


def run_baseline(
    records: Sequence[GoldenRecord] | None = None, *, llm: Any | None = None
) -> tuple[BaselineSummary, list[RecordOutcome]]:
    """Run the full sweep. One failing record is logged and skipped, not fatal."""
    records = list(records) if records is not None else load_goldens()
    outcomes: list[RecordOutcome] = []
    for record in records:
        try:
            outcome = run_record(record, llm=llm)
        except Exception:  # noqa: BLE001 -- one broken record shouldn't kill the sweep
            logger.exception("baseline %s: run_record raised", record.id)
            continue
        outcomes.append(outcome)
        logger.info(
            "baseline %s: constrained=%s unconstrained=%s",
            record.id,
            "clean" if outcome.constrained_ok else f"{len(outcome.constrained_unsupported)} fab",
            "clean"
            if outcome.unconstrained_ok
            else f"{len(outcome.unconstrained_unsupported)} fab",
        )
    return summarize(outcomes), outcomes


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary, _ = run_baseline()
    print(summary.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
