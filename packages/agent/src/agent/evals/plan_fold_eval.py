"""Plan-pick fold agreement eval (QNT-327, v3 G-6 spike).

QNT-327 folds the thesis plan pick into the classify call: ``IntentDecision``
gained optional ``report_picks`` / ``plan_rationale`` fields (produced only for a
``thesis`` intent), and plan_node consumes them -- skipping the dedicated
``ThesisPlan`` call -- so a thesis turn drops from four sequential LLM calls to
three. That is only safe to SHIP if the folded picks reproduce what the standalone
planner would have chosen; otherwise the fold trades a round trip for worse plans.

This harness measures that. For each golden question the LIVE classifier routes to
``thesis``, it runs BOTH resolvers over the same available report set:

* the FOLDED pick carried on ``IntentDecision.report_picks`` (one classify call),
  resolved through ``_tools_from_folded_picks`` -- the exact plan_node path; and
* the standalone ``ThesisPlan`` planner (the call QNT-327 skips), resolved through
  ``_tools_from_thesis_plan`` -- the exact fallback path.

It reports the plan-pick AGREEMENT rate (folded plan == planner plan) plus the
resolved intent per fixture, so an intent-label drift from the fatter schema shows
up alongside the agreement number. This is the "picks match the current planner's
on the fixture set" half of AC2; ``routing_eval`` covers the "intent accuracy
unchanged" half.

Because it fires two live small structured calls per thesis fixture, it needs
LiteLLM (not Qdrant -- no retrieval here) and is NOT collected by pytest; the
offline seams (``_tools_from_folded_picks`` filtering, the classify schema) are
unit-tested in tests/agent. Respect the clean-rate-limit-window rule (Groq TPD for
the small model) before publishing baseline numbers -- contamination is flagged
via per-fixture latency, mirroring routing_eval.

Example::

    uv run python -m agent.evals.plan_fold_eval
    uv run python -m agent.evals.plan_fold_eval --only nvda-technical
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass

import httpx
from shared.config import settings

from agent import graph
from agent.evals.golden_set import GoldenRecord, load_goldens
from agent.intent import classify_intent_with_source

logger = logging.getLogger(__name__)

# Agreement floor: the fraction of thesis fixtures where the folded plan must equal
# the standalone planner's plan for the fold to be shippable. Tuned to leave
# headroom for small-model variance on the two independent structured calls while
# still catching a real divergence (the fold systematically over- or under-fetching
# vs the planner). The SHIP / measured-and-declined decision is recorded in the v3
# doc either way (AC3); this floor is the machine-checkable half.
AGREEMENT_FLOOR = 0.80

# A live classify + planner pair returns in a few seconds on a clean window. A
# fixture clearing this floor means a call ran to its timeout ceiling -- the
# Groq-throttle signature. Mirrors routing_eval / news_search_eval.
CONTAMINATION_LATENCY_MS = int(settings.LLM_REQUEST_TIMEOUT * 1000)


@dataclass(frozen=True)
class FoldOutcome:
    """Folded-vs-planner result for one thesis fixture."""

    record: GoldenRecord
    resolved_intent: str
    classifier_source: str
    folded_plan: tuple[str, ...] | None
    planner_plan: tuple[str, ...] | None
    elapsed_ms: int

    @property
    def scored(self) -> bool:
        """True when both resolvers produced a plan and can be compared.

        A fixture that did not route to thesis, or where the folded pick was empty
        / degenerate (``folded_plan is None`` -> plan_node would fall back), or
        where the planner call failed, is UNSCORED: it is not a fold disagreement,
        it is a fixture the fold never claims to serve.
        """
        return self.folded_plan is not None and self.planner_plan is not None

    @property
    def agree(self) -> bool:
        return self.scored and self.folded_plan == self.planner_plan


def _available_tools() -> list[str]:
    """The report set both resolvers pick from -- all registered REPORT_TOOLS.

    plan_node narrows this to the tools actually wired into ``deps``; the eval
    assumes the full production registry so folded and planner picks are compared
    over an identical option set.
    """
    return list(graph.REPORT_TOOLS)


def evaluate(record: GoldenRecord) -> FoldOutcome:
    """Resolve the folded pick and the standalone planner pick for one fixture."""
    available = _available_tools()
    started = time.perf_counter()
    intent, source, _news, _earn, _query, report_picks, _rationale = classify_intent_with_source(
        record.question
    )
    folded_plan: tuple[str, ...] | None = None
    planner_plan: tuple[str, ...] | None = None
    if intent == "thesis":
        folded = graph._tools_from_folded_picks(list(report_picks), available)
        folded_plan = tuple(folded) if folded is not None else None
        # Run the standalone planner regardless of whether the fold produced a
        # plan -- we want the planner baseline for every thesis fixture so an
        # empty fold (which falls back in production) is visible as UNSCORED, not
        # silently counted as agreement.
        prompt = graph._build_thesis_plan_prompt(record.ticker, record.question, available)
        thesis_plan = graph._structured_call(
            graph.ThesisPlan,
            prompt,
            {},
            f"plan {record.ticker}",
            llm=graph.get_llm(temperature=0.0, model_alias=graph.SMALL_NODE_ALIAS),
            linked=False,
        )
        if thesis_plan is not None:
            planner_plan = tuple(graph._tools_from_thesis_plan(thesis_plan, available))
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return FoldOutcome(
        record=record,
        resolved_intent=intent,
        classifier_source=source,
        folded_plan=folded_plan,
        planner_plan=planner_plan,
        elapsed_ms=elapsed_ms,
    )


def precheck_environment(*, timeout: float = 5.0) -> None:
    """Raise if the LiteLLM proxy is unreachable (both live calls need it)."""
    base_url = settings.LITELLM_BASE_URL
    try:
        httpx.get(base_url, timeout=timeout)
    except httpx.HTTPError as exc:
        raise RuntimeError(
            "plan-fold eval precheck failed -- start the LiteLLM proxy first "
            f"(make dev-litellm): LiteLLM proxy unreachable at {base_url} "
            f"({type(exc).__name__})"
        ) from exc


@dataclass(frozen=True)
class FoldReport:
    """Aggregate of one run."""

    outcomes: tuple[FoldOutcome, ...]

    @property
    def scored(self) -> list[FoldOutcome]:
        return [o for o in self.outcomes if o.scored]

    @property
    def agreement(self) -> float:
        scored = self.scored
        if not scored:
            return 0.0
        return sum(1 for o in scored if o.agree) / len(scored)

    @property
    def disagreements(self) -> list[FoldOutcome]:
        return [o for o in self.scored if not o.agree]


def run_all(*, only: str | None = None, skip_precheck: bool = False) -> FoldReport:
    """Run both resolvers over the golden set and return the aggregate."""
    if not skip_precheck:
        precheck_environment()
    records = load_goldens()
    if only is not None:
        records = [r for r in records if r.id == only]
        if not records:
            raise ValueError(f"no golden record with id {only!r}")
    return FoldReport(outcomes=tuple(evaluate(r) for r in records))


def contamination_warning(report: FoldReport) -> str | None:
    """Flag a run contaminated by Groq throttling (latency signal)."""
    slow = [o for o in report.outcomes if o.elapsed_ms >= CONTAMINATION_LATENCY_MS]
    if not slow:
        return None
    return (
        f"CONTAMINATED RUN -- do not trust this aggregate. {len(slow)} fixture(s) "
        f"over the {CONTAMINATION_LATENCY_MS}ms timeout-ceiling floor "
        "(likely Groq throttling): "
        + ", ".join(f"{o.record.id}={o.elapsed_ms}ms" for o in slow)
        + ". Re-run on a clean rate-limit window before publishing baseline numbers."
    )


def is_failing(report: FoldReport) -> bool:
    """Hard gate: agreement below the floor. Empty scored set fails too -- a run
    where no fixture routed to thesis with a usable fold proves nothing."""
    if not report.scored:
        return True
    return report.agreement < AGREEMENT_FLOOR


def _fmt_plan(plan: tuple[str, ...] | None) -> str:
    return ",".join(plan) if plan else "(none)"


def summarise(report: FoldReport) -> str:
    """Human-readable per-fixture + aggregate scorecard for stdout / the PR."""
    lines: list[str] = []
    warning = contamination_warning(report)
    if warning is not None:
        lines += [warning, ""]

    scored = report.scored
    agree = sum(1 for o in scored if o.agree)
    floor_mark = "PASS" if report.agreement >= AGREEMENT_FLOOR else "BELOW FLOOR"
    lines += [
        "PLAN-FOLD EVAL (folded classify report_picks vs standalone ThesisPlan planner)",
        f"  agreement: {agree}/{len(scored)} ({report.agreement:.0%})  "
        f"floor: {AGREEMENT_FLOOR:.0%} [{floor_mark}]  "
        f"disagreements: {len(report.disagreements)}  "
        f"unscored: {len(report.outcomes) - len(scored)}",
    ]
    for o in report.outcomes:
        if not o.scored:
            reason = (
                "not-thesis"
                if o.resolved_intent != "thesis"
                else ("empty-fold" if o.folded_plan is None else "planner-failed")
            )
            mark = f"UNSCORED/{reason}"
        else:
            mark = "agree" if o.agree else "DISAGREE"
        lines.append(
            f"    [{mark:20s}] {o.record.id:26s} "
            f"intent={o.resolved_intent}/{o.classifier_source:9s} "
            f"folded={_fmt_plan(o.folded_plan):24s} "
            f"planner={_fmt_plan(o.planner_plan):24s} {o.elapsed_ms}ms"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.evals.plan_fold_eval")
    parser.add_argument("--only", help="Run only one golden record id")
    parser.add_argument(
        "--skip-precheck",
        action="store_true",
        help="Skip the LiteLLM reachability precheck (offline/testing only).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    try:
        report = run_all(only=args.only, skip_precheck=args.skip_precheck)
    except RuntimeError as exc:
        print(f"SKIPPED: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"SKIPPED: {exc}", file=sys.stderr)
        return 2

    print(summarise(report))
    return 1 if is_failing(report) else 0


__all__ = [
    "AGREEMENT_FLOOR",
    "CONTAMINATION_LATENCY_MS",
    "FoldOutcome",
    "FoldReport",
    "contamination_warning",
    "evaluate",
    "is_failing",
    "precheck_environment",
    "run_all",
    "summarise",
]


if __name__ == "__main__":
    sys.exit(main())
