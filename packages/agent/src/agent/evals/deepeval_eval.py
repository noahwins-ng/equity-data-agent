"""LLM-judged RAG generation eval via DeepEval (QNT-264, eval type (h)).

The deterministic layers measure retrieval (recall@k / MRR / nDCG, eval (f)) and
number-grounding faithfulness (eval (a)); both gate every PR, LLM-free. This is
the *nuance* layer the design doc's two-stage blend calls for: the RAGAS metric
set -- faithfulness, answer relevancy, context precision/recall -- plus one
custom G-Eval, all **LLM-judged**, run through DeepEval (the pytest-native
production CI-gating framework that subsumes RAGAS). See
docs/v2-overall-enhancement.md "RAG eval framework", Track 2 (2.7).

Why off the per-PR hot path:
    DeepEval calls an LLM judge several times per metric per case. ci.yml wires
    NO LLM keys (only ClickHouse) and our free-tier budget (Gemini 20 RPD, Groq
    TPD) plus the clean-window rule make judge-on-every-PR a bad fit. So this
    suite runs nightly / on workflow_dispatch (keys as job-scoped secrets) and
    locally in /sanity-check -- NEVER as a per-PR gate. The per-PR RAG gate is
    the deterministic one (``tests/agent/evals/test_retrieval_eval.py``, QNT-261).

Judge routing + budget (AC2):
    The judge is the SAME free model the dialogue judge already pins
    (``JUDGE_ALIAS`` = ``equity-agent/bench-cerebras-gptoss120b`` ->
    ``cerebras/gpt-oss-120b``), reached through the LiteLLM proxy via
    :func:`agent.llm.get_judge_llm` -- no new provider key. Gated to a SAMPLE
    (``DEEPEVAL_SAMPLE``, default 4 records) on a clean window: each case costs
    ~8-12 judge calls across the five metrics, so a 4-record run is ~32-48 calls
    -- comfortably inside the free tier when the window is clean. Metrics run
    ``async_mode=False`` so calls serialise rather than burst the rate limit.

Coexistence (AC4):
    The in-house number-grounding check (eval (a)) is retained and asserted
    additively here -- it is a stricter, deterministic, verbatim faithfulness
    layer for financial figures than the generic LLM-judged faithfulness. This
    suite is the nuance layer, NOT a replacement.

Recording (AC5):
    Each run appends one ``eval_type="deepeval"`` row to ``history.csv`` (the
    ``deepeval_*`` mean columns + ``deepeval_n``), stamped with the same
    ``git_sha`` + ``prompt_version`` as every other eval type, so a generation
    regression sits in the same reviewable ledger as the IR metrics.

Usage::

    uv run python -m agent.evals.deepeval_eval                 # sample run + record
    uv run python -m agent.evals.deepeval_eval --sample 8
    uv run python -m agent.evals.deepeval_eval --only NVDA --no-record
"""

from __future__ import annotations

import argparse
import logging
import os

# DeepEval phones home to Confident AI (telemetry + error reporting) unless
# opted out. Set BEFORE importing deepeval so the import-time hooks see it -- the
# eval must not exfiltrate run data, and CI/local runs stay offline.
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("ERROR_REPORTING", "0")

import csv  # noqa: E402
import sys  # noqa: E402
import uuid  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, cast  # noqa: E402

import httpx  # noqa: E402
import yaml  # noqa: E402
from shared.config import settings  # noqa: E402
from shared.tickers import TICKERS  # noqa: E402

from agent.evals.golden_set import (  # noqa: E402
    HISTORY_FIELDS,
    HISTORY_PATH,
    EvalOutcome,
    GoldenRecord,
    _git_sha,
    _prompt_version,
    run_record,
)
from agent.evals.hallucination import check as check_hallucination  # noqa: E402
from agent.llm import JUDGE_ALIAS, get_judge_llm  # noqa: E402

logger = logging.getLogger(__name__)

# QNT-275: the recall-appropriate golden set. Distinct from questions.yaml (the
# structured-eval goldens whose reference_thesis describes the answer SHAPE):
# here each ``recall_reference`` states the FACTS the gathered reports carry, so
# every reference statement is attributable to a retrieved report chunk and
# ContextualRecallMetric measures retrieval completeness rather than the 0.29
# structural artifact the shape-references produced. See the file header for the
# full rationale. The set is sized >=50 so a clean-window baseline is
# statistically meaningful (the same 50-record floor as the retrieval eval).
RECALL_GOLDENS_PATH = Path(__file__).parent / "goldens" / "deepeval_recall.yaml"

# Number of golden records sampled per run -- the budget lever (AC2). Each record
# costs ~8-12 judge calls across the five metrics, so the default keeps a run
# inside the free tier on a clean window. Override with --sample / DEEPEVAL_SAMPLE.
DEFAULT_SAMPLE = int(os.environ.get("DEEPEVAL_SAMPLE", "4"))

# Per-metric pass thresholds -- the design-doc calibration targets (Track 2.7):
# faithfulness/context-precision/context-recall > 0.8, answer-relevancy > 0.75,
# G-Eval > 0.7. These are NOT a per-PR gate (this suite is off the hot path);
# they're the assert floors the nightly/dispatch run surfaces a regression
# against.
#
# QNT-275 status -- floors NOT yet re-derived; enforcement still opt-in. The
# recall-appropriate golden set (deepeval_recall.yaml) is in place and the
# context_recall artifact is FIXED: against the shape-references it read 0.29
# (run 20260621T042449Z-b07f37, n=4); against the recall references it read
# context_recall mean 1.000 / min 1.000, faithfulness 0.980, context_precision
# 0.900 across the first 20 clean records of a baseline sweep. The full >=50-record
# baseline (AC2) did NOT complete: the fixed bench judge
# (bench-cerebras-gptoss120b, no fallback by design) exhausts the Cerebras
# free-tier daily token budget at ~20 judged records, so a >=50 run needs a
# higher-budget judge or multi-day batching. Until that baseline lands, these
# stay at the design-doc aspirations and the assert_test gate stays opt-in
# (DEEPEVAL_ENFORCE_THRESHOLDS). Follow-up (AC2/AC3/AC4): run the >=50 sweep on a
# clean window, set floors ~0.10-0.15 below the measured means (the retrieval-eval
# discipline), and flip enforcement on.
THRESHOLDS: dict[str, float] = {
    "faithfulness": 0.8,
    "answer_relevancy": 0.75,
    "context_precision": 0.8,
    "context_recall": 0.8,
    "geval": 0.7,
}

# The custom G-Eval (AC1: "at least one custom G-Eval"). Domain-specialised and
# additive to the RAGAS set: whether the investment verdict is *justified by* the
# retrieved evidence and doesn't overstate confidence -- a senior-analyst nuance
# the generic relevancy/faithfulness metrics don't capture.
GEVAL_NAME = "VerdictGroundedness"
GEVAL_CRITERIA = (
    "Determine whether the investment verdict or recommendation in the actual "
    "output is justified by the evidence in the retrieval context, and does not "
    "overstate confidence beyond what the cited figures and facts support. A "
    "well-grounded verdict cites specific evidence; an ungrounded one asserts a "
    "rating the context does not support."
)


class LiteLLMJudge:
    """DeepEval custom judge backed by the LiteLLM proxy + the pinned free model.

    Wraps :func:`agent.llm.get_judge_llm` (``JUDGE_ALIAS`` -> ``cerebras/
    gpt-oss-120b``, the SAME fixed judge the dialogue eval uses) so DeepEval's
    RAGAS metrics route through the existing proxy with no new provider key
    (AC2). ``generate`` accepts DeepEval's optional ``schema`` kwarg: when
    present we use LangChain ``with_structured_output`` so the metric gets a
    typed instance back (DeepEval's ``generate_with_schema`` fast path);
    otherwise we return the raw string and DeepEval parses the JSON itself.

    Subclasses ``DeepEvalBaseLLM`` at runtime (imported lazily inside the class
    body so importing this module never hard-requires deepeval on the prod path).
    """

    def __init__(self) -> None:
        self._llm = get_judge_llm()

    def load_model(self) -> Any:
        return self._llm

    def generate(self, prompt: str, schema: type | None = None) -> Any:
        chat = self.load_model()
        if schema is not None:
            return chat.with_structured_output(schema).invoke(prompt)
        content = chat.invoke(prompt).content
        return content if isinstance(content, str) else str(content)

    async def a_generate(self, prompt: str, schema: type | None = None) -> Any:
        chat = self.load_model()
        if schema is not None:
            return await chat.with_structured_output(schema).ainvoke(prompt)
        res = await chat.ainvoke(prompt)
        content = res.content
        return content if isinstance(content, str) else str(content)

    def get_model_name(self) -> str:
        return JUDGE_ALIAS

    def supports_structured_outputs(self) -> bool:
        return True


def _judge() -> Any:
    """Build the DeepEval-typed judge, rebinding ``LiteLLMJudge`` to the base.

    deepeval is a dev-only dependency (see pyproject); import it here, not at
    module top, so ``import agent.evals.deepeval_eval`` doesn't pull the judged-
    eval tree on a runtime path. We splice ``DeepEvalBaseLLM`` in as the base so
    the class still satisfies deepeval's isinstance checks.
    """
    from deepeval.models.base_model import DeepEvalBaseLLM

    judge_cls = type("LiteLLMJudge", (LiteLLMJudge, DeepEvalBaseLLM), {})
    return judge_cls()


@dataclass(frozen=True)
class DeepEvalCase:
    """One scored case: the DeepEval metric means + the number-grounding result."""

    record_id: str
    ticker: str
    scores: dict[str, float]
    grounding_ok: bool
    grounding_reason: str


def build_metrics(judge: Any) -> dict[str, Any]:
    """The RAGAS metric set + the custom G-Eval, all routed to the free judge.

    ``async_mode=False`` serialises the judge calls so a sample run can't burst
    past the free-tier rate limit (AC2). Keys match the THRESHOLDS / history
    columns so scoring, gating, and recording stay in lockstep.
    """
    from deepeval.metrics import (
        AnswerRelevancyMetric,
        ContextualPrecisionMetric,
        ContextualRecallMetric,
        FaithfulnessMetric,
        GEval,
    )
    from deepeval.test_case import SingleTurnParams

    return {
        "faithfulness": FaithfulnessMetric(
            threshold=THRESHOLDS["faithfulness"], model=judge, async_mode=False
        ),
        "answer_relevancy": AnswerRelevancyMetric(
            threshold=THRESHOLDS["answer_relevancy"], model=judge, async_mode=False
        ),
        "context_precision": ContextualPrecisionMetric(
            threshold=THRESHOLDS["context_precision"], model=judge, async_mode=False
        ),
        "context_recall": ContextualRecallMetric(
            threshold=THRESHOLDS["context_recall"], model=judge, async_mode=False
        ),
        "geval": GEval(
            name=GEVAL_NAME,
            criteria=GEVAL_CRITERIA,
            evaluation_params=[
                SingleTurnParams.INPUT,
                SingleTurnParams.ACTUAL_OUTPUT,
                SingleTurnParams.RETRIEVAL_CONTEXT,
            ],
            threshold=THRESHOLDS["geval"],
            model=judge,
            async_mode=False,
        ),
    }


def load_recall_goldens(path: Path = RECALL_GOLDENS_PATH) -> list[GoldenRecord]:
    """Parse the recall golden set into ``GoldenRecord``s (QNT-275).

    Each ``recall_reference`` is mapped onto ``reference_thesis`` so the existing
    :func:`build_test_case` picks it up as DeepEval's ``expected_output`` with no
    other change -- the recall reference IS the ground-truth answer the
    ContextualRecallMetric attributes against the gathered reports. ``run_record``
    runs the real agent and captures the retrieval context, exactly as for the
    structured goldens; only the reference differs.

    Validates ticker membership, unique ids, and required-field presence so the
    set fails loudly on a malformed edit rather than silently judging fewer
    records.
    """
    raw = yaml.safe_load(path.read_text())
    rows = raw.get("recall") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"{path}: missing top-level `recall` list")

    records: list[GoldenRecord] = []
    seen: set[str] = set()
    for entry in rows:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each record must be a mapping, got {type(entry)}")
        try:
            rec_id = str(entry["id"])
            ticker = str(entry["ticker"])
            question = str(entry["question"])
            reference = str(entry["recall_reference"]).strip()
        except KeyError as exc:
            raise ValueError(f"{path}: record missing field {exc}") from exc
        if rec_id in seen:
            raise ValueError(f"{path}: duplicate record id {rec_id!r}")
        if ticker not in TICKERS:
            raise ValueError(f"{path}: record {rec_id!r} references unknown ticker {ticker!r}")
        if not reference:
            raise ValueError(f"{path}: record {rec_id!r} has an empty recall_reference")
        seen.add(rec_id)
        records.append(
            GoldenRecord(
                id=rec_id,
                ticker=ticker,
                question=question,
                expected_tools=tuple(str(t) for t in entry.get("expected_tools", [])),
                reference_thesis=reference,
            )
        )
    return records


def build_test_case(outcome: EvalOutcome) -> Any:
    """Map an agent run onto a DeepEval ``LLMTestCase``.

    ``retrieval_context`` is the report strings the agent gathered -- the
    CONTEXT the RAGAS metrics score the thesis against. ``expected_output`` is
    the golden reference thesis (context recall is the one RAGAS metric that
    strictly needs ground truth).
    """
    from deepeval.test_case import LLMTestCase

    return LLMTestCase(
        input=outcome.record.question,
        actual_output=outcome.thesis,
        expected_output=outcome.record.reference_thesis,
        retrieval_context=list(outcome.reports),
    )


def score_outcome(outcome: EvalOutcome, metrics: dict[str, Any]) -> DeepEvalCase:
    """Measure every metric on one case + run the deterministic grounding check.

    A metric that raises (malformed judge JSON, transient proxy error) records a
    NaN for that axis rather than crashing the sweep -- mirrors how the golden
    judge degrades to ``None``. The number-grounding result is computed here so
    the two faithfulness layers (LLM-judged + deterministic) sit side by side on
    the same case (AC4).
    """
    test_case = build_test_case(outcome)
    scores: dict[str, float] = {}
    for name, metric in metrics.items():
        try:
            metric.measure(test_case)
            scores[name] = float(metric.score) if metric.score is not None else float("nan")
        except Exception as exc:  # noqa: BLE001 -- one bad metric must not drop the run
            logger.warning("deepeval metric %s failed on %s: %s", name, outcome.record.id, exc)
            scores[name] = float("nan")

    hresult = check_hallucination(outcome.thesis, list(outcome.reports))
    return DeepEvalCase(
        record_id=outcome.record.id,
        ticker=outcome.record.ticker,
        scores=scores,
        grounding_ok=hresult.ok,
        grounding_reason=hresult.reason(),
    )


def _mean(values: list[float]) -> float:
    """Mean over the non-NaN values, or NaN when every measurement failed."""
    clean = [v for v in values if v == v]  # drop NaN
    return sum(clean) / len(clean) if clean else float("nan")


def aggregate(cases: list[DeepEvalCase]) -> dict[str, float]:
    """Per-axis mean across the sampled cases -- what lands in history.csv."""
    return {name: _mean([c.scores.get(name, float("nan")) for c in cases]) for name in THRESHOLDS}


def precheck_environment(*, timeout: float = 5.0) -> None:
    """Fail fast if the LiteLLM proxy or report API is unreachable (QNT-218).

    The DeepEval sample runs the real agent live, so an unreachable report API
    would score the judge on empty reports. A reachable HTTP response (any
    status) clears the check; a connection error fails it before a token is
    spent. Mirrors ``dialogue_eval.precheck_environment``.
    """
    targets = {
        "LiteLLM proxy": settings.LITELLM_BASE_URL,
        "report API": settings.API_BASE_URL,
    }
    unreachable: list[str] = []
    for name, base_url in targets.items():
        try:
            httpx.get(base_url, timeout=timeout)
        except httpx.HTTPError as exc:
            unreachable.append(f"{name} unreachable at {base_url} ({type(exc).__name__})")
    if unreachable:
        raise RuntimeError(
            "deepeval precheck failed -- start the dev stack first "
            "(make dev-litellm / make dev-api / make tunnel):\n  " + "\n  ".join(unreachable)
        )


def stack_reachable(*, timeout: float = 3.0) -> bool:
    """Best-effort liveness probe for the pytest skip guard (no raise)."""
    try:
        precheck_environment(timeout=timeout)
    except RuntimeError:
        return False
    return True


def run_sample(
    *, sample: int = DEFAULT_SAMPLE, only: str | None = None, judge: Any | None = None
) -> list[DeepEvalCase]:
    """Run the agent on a sample of golden records and score each via DeepEval.

    ``only`` filters to one ticker; ``sample`` caps the record count (the budget
    lever). Provider-error records (Groq quota / timeout) are skipped -- they
    carry no thesis to judge. Returns one :class:`DeepEvalCase` per scored record.

    Reads the recall-appropriate golden set (QNT-275), NOT the structured
    questions.yaml: the references here are attributable to the gathered reports,
    so context_recall measures retrieval completeness instead of the shape-
    reference artifact.
    """
    records = load_recall_goldens()
    if only is not None:
        wanted = only.upper()
        records = [r for r in records if r.ticker == wanted]
        if not records:
            raise ValueError(f"no golden records for ticker {wanted!r}")
    records = records[: max(1, sample)]

    metrics = build_metrics(judge if judge is not None else _judge())
    cases: list[DeepEvalCase] = []
    for record in records:
        outcome = run_record(record)
        if outcome.provider_error:
            logger.warning(
                "skipping %s: provider error (%s)", record.id, outcome.hallucination_reason
            )
            continue
        cases.append(score_outcome(outcome, metrics))
    return cases


def append_deepeval_history(
    means: dict[str, float],
    *,
    n_cases: int,
    run_id: str | None = None,
    history_path: Path = HISTORY_PATH,
) -> str:
    """Append one aggregate ``eval_type="deepeval"`` row to history.csv (AC5).

    Mirrors the retrieval aggregate row (QNT-261): one row per run, the
    ``deepeval_*`` columns filled and everything else blank, stamped with the
    same git_sha + prompt_version as the other eval types so a generation
    regression is bisectable against the same commits as the IR metrics.
    """

    def _fmt(value: float) -> str:
        return "" if value != value else str(round(value, 4))  # blank on NaN

    rid = run_id or uuid.uuid4().hex[:8]
    new_file = not history_path.exists()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
        if new_file:
            writer.writeheader()
        row: dict[str, Any] = {field: "" for field in HISTORY_FIELDS}
        row.update(
            {
                "run_id": rid,
                "git_sha": _git_sha(),
                "prompt_version": _prompt_version(),
                "eval_type": "deepeval",
                "deepeval_faithfulness": _fmt(means.get("faithfulness", float("nan"))),
                "deepeval_answer_relevancy": _fmt(means.get("answer_relevancy", float("nan"))),
                "deepeval_context_precision": _fmt(means.get("context_precision", float("nan"))),
                "deepeval_context_recall": _fmt(means.get("context_recall", float("nan"))),
                "deepeval_geval": _fmt(means.get("geval", float("nan"))),
                "deepeval_n": n_cases,
            }
        )
        writer.writerow(cast(Any, row))
    return rid


def summarise(cases: list[DeepEvalCase], means: dict[str, float]) -> str:
    """Human-readable scorecard + threshold verdict for stdout / the README."""
    n = len(cases)
    grounding_ok = sum(1 for c in cases if c.grounding_ok)
    lines = [
        f"DEEPEVAL GENERATION EVAL ({n} sampled records, judge={JUDGE_ALIAS}, LLM-judged)",
    ]
    for name in THRESHOLDS:
        value = means.get(name, float("nan"))
        floor = THRESHOLDS[name]
        shown = "n/a" if value != value else f"{value:.4f}"
        verdict = "" if value != value else ("PASS" if value >= floor else "BELOW")
        lines.append(f"  {name:<18} {shown:>6}  (floor {floor})  {verdict}")
    lines.append(f"  number-grounding (deterministic, AC4): {grounding_ok}/{n} clean")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.evals.deepeval_eval")
    parser.add_argument(
        "--sample", type=int, default=DEFAULT_SAMPLE, help="Records to judge (budget lever)."
    )
    parser.add_argument("--only", default=None, help="Filter to a single ticker.")
    parser.add_argument(
        "--no-record", action="store_true", help="Skip the history.csv append (dry run)."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    precheck_environment()

    cases = run_sample(sample=args.sample, only=args.only)
    if not cases:
        print(
            "no records scored (all provider errors?) -- re-run on a clean window", file=sys.stderr
        )
        return 2
    means = aggregate(cases)
    print(summarise(cases, means))
    if not args.no_record:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        rid = f"{timestamp}-{uuid.uuid4().hex[:6]}-deepeval"
        append_deepeval_history(means, n_cases=len(cases), run_id=rid)
        print(f"\nrecorded run_id: {rid}")
    return 0


__all__ = [
    "DEFAULT_SAMPLE",
    "GEVAL_CRITERIA",
    "GEVAL_NAME",
    "RECALL_GOLDENS_PATH",
    "THRESHOLDS",
    "DeepEvalCase",
    "LiteLLMJudge",
    "aggregate",
    "append_deepeval_history",
    "build_metrics",
    "build_test_case",
    "load_recall_goldens",
    "precheck_environment",
    "run_sample",
    "score_outcome",
    "stack_reachable",
    "summarise",
]


if __name__ == "__main__":
    sys.exit(main())
