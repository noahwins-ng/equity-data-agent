"""Golden-set regression harness (QNT-67, eval type (b)).

For each record in ``goldens/questions.yaml``:
    1. Build the agent graph with recording-wrapped tools.
    2. Invoke ``graph.invoke({"ticker": ticker, "question": question})``.
    3. Run the hallucination, tool-call, and similarity / judge scorers
       against the resulting state.
    4. Append one row to ``history.csv``.

In-process invocation vs subprocess CLI:
    The original ticket says "invoke the agent CLI". We invoke ``build_graph``
    in-process instead so we can inspect the ``reports`` dict (needed for the
    hallucination scorer) and the recorded tool-call list (needed for the
    tool-call scorer) without re-parsing stdout. Functionally identical:
    ``__main__`` only adds an arg-parser and stdout printing on top of the
    same graph.

History is the source of truth:
    Committing ``history.csv`` to git makes prompt-version quality reviewable
    in the diff (``git log -p evals/history.csv``). Each run appends; we
    never rewrite past rows so a regression shows up as worse numbers below
    a commit, not as a vanishing comparison point.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from shared.config import settings
from shared.tickers import TICKERS

from agent.comparison import ComparisonAnswer
from agent.conversational import ConversationalAnswer
from agent.evals.hallucination import check as check_hallucination
from agent.evals.judge import JudgeScore
from agent.evals.judge import score as judge_score_fn
from agent.evals.provider_errors import is_provider_pressure_error, provider_error_label
from agent.evals.similarity import cosine
from agent.evals.spine import (
    HISTORY_FIELDS,
    HISTORY_PATH,
    append_suite_history,
    git_sha,
    prompt_version,
    suite_history_path,
    threshold_from_env,
)
from agent.evals.tool_calls import check as check_tool_calls
from agent.evals.tool_calls import wrap_with_recorder
from agent.exploration import ExplorationAnswer
from agent.focused import FocusedAnalysis
from agent.graph import build_graph
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from agent.tools import default_report_tools, get_company_report_compact

logger = logging.getLogger(__name__)

GOLDENS_PATH = Path(__file__).parent / "goldens" / "questions.yaml"

# QNT-234: a single structured record never approaches the per-call LLM timeout
# in a clean window -- gather is local HTTP and each LLM call returns in a few
# seconds. A record whose wall time clears one full ``LLM_REQUEST_TIMEOUT`` means
# at least one call ran to its timeout ceiling: the signature of provider
# throttling (QNT-233 saw comparison records ~186s == 3x the 60s ceiling), not a
# slow prompt. Derive the floor from the timeout so the two stay in lockstep.
# NB: this tracks LLM_REQUEST_TIMEOUT (default 60s). If that is bumped (e.g. for
# a slower model) the floor rises with it -- recalibrate against a fresh clean-run
# baseline if a contaminated run stops being flagged.
CONTAMINATION_LATENCY_MS = int(settings.LLM_REQUEST_TIMEOUT * 1000)

# HISTORY_FIELDS / HISTORY_PATH now live in agent.evals.spine (QNT-293) so the
# shared history envelope can't drift between the eight suites. Re-exported above
# and in __all__ for back-compat -- suites still import them from here today.

# QNT-293 follow-up: the golden set writes its own per-suite history file. Its
# columns are exactly the golden metrics (the envelope -- run_id/git_sha/
# prompt_version/suite -- is added by append_suite_history). eval_type stays
# "structured" for every golden row; it is kept as a column so the migrated
# historical rows carry it and a future golden sub-variant has somewhere to land.
GOLDEN_HISTORY_PATH = suite_history_path("golden")
GOLDEN_FIELDS = (
    "eval_type",
    "ticker",
    "question_id",
    "question",
    "faithfulness",
    "structure",
    "correctness",
    "analyst_logic",
    "composite",
    "cosine",
    "tool_call_ok",
    "hallucination_ok",
    "verdict_label_consistent",
    "elapsed_ms",
)


@dataclass(frozen=True)
class GoldenRecord:
    """One row from goldens/questions.yaml.

    ``expected_intent`` defaults to "auto" — the harness lets the
    classifier decide and just scores whatever shape comes out. Setting it
    explicitly is informational; the conversational shape additionally
    permits an empty ``expected_tools`` list (the path skips gather).

    ``forbidden_substrings`` (QNT-184): if non-empty, any substring present
    in the rendered thesis output (case-insensitive) is treated as a hard
    contract violation and folds into ``hallucination_ok=False``. Use for
    anti-pattern regression tests (e.g. "indicators agree" guards the
    anti-SIGNAL-footer rule).

    ``forbidden_aspect_support_substrings`` (QNT-208, renamed from
    ``forbidden_bull_substrings``): mapping ``{aspect_name: (substring,
    ...)}`` checked against the ``supports`` list of the named aspect on a
    Thesis response. Aspect names are ``company`` / ``fundamental`` /
    ``technical`` / ``news``. Use when a term is correct in challenges but
    forbidden in supports (e.g. "overbought" must not appear in
    ``technical.supports`` even though it belongs in ``technical.challenges``).
    No-op for non-Thesis response shapes.
    """

    id: str
    ticker: str
    question: str
    expected_tools: tuple[str, ...]
    reference_thesis: str
    expected_intent: str = "auto"
    forbidden_substrings: tuple[str, ...] = ()
    forbidden_aspect_support_substrings: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalOutcome:
    """Per-record result captured from one run."""

    record: GoldenRecord
    thesis: str
    actual_tools: tuple[str, ...]
    hallucination_ok: bool
    hallucination_reason: str
    tool_call_ok: bool
    tool_call_reason: str
    judge_score: JudgeScore | None
    cosine: float
    elapsed_ms: int
    # QNT-234: True when graph.invoke raised a provider-pressure error (Groq
    # quota / TPM-TPD / request timeout / upstream 5xx) rather than producing a
    # real contract result. Such rows are excluded from the exit gate and from
    # history.csv -- they measure free-tier capacity, not the agent code.
    provider_error: bool = False
    # QNT-302: verdict-vs-labels consistency for structured-thesis outcomes.
    # None for every non-thesis shape (quick_fact / comparison / focused / ...).
    # Advisory only -- never folded into hallucination_ok or the exit gate.
    verdict_label_consistent: bool | None = None
    # QNT-264: the flattened report strings the agent gathered for this record --
    # the retrieval CONTEXT the DeepEval RAGAS metrics score the thesis against
    # (faithfulness / context precision / recall). Empty on the provider-error
    # path (no reports gathered); the default keeps that construction untouched.
    reports: tuple[str, ...] = ()


def load_goldens(path: Path = GOLDENS_PATH) -> list[GoldenRecord]:
    """Parse the YAML registry into typed records.

    Validates ticker membership and required-field presence here so every
    downstream consumer (eval loop, ticker-coverage test, future report
    generators) reads from the same authority.
    """
    raw = yaml.safe_load(path.read_text())
    questions = raw.get("questions") if isinstance(raw, dict) else None
    if not isinstance(questions, list):
        raise ValueError(f"{path}: missing top-level `questions` list")

    records: list[GoldenRecord] = []
    seen_ids: set[str] = set()
    for entry in questions:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each question must be a mapping, got {type(entry)}")
        try:
            rec_id = str(entry["id"])
            ticker = str(entry["ticker"])
            question = str(entry["question"])
            expected = tuple(str(t) for t in entry["expected_tools"])
            reference = str(entry["reference_thesis"]).strip()
        except KeyError as exc:
            raise ValueError(f"{path}: question missing field {exc}") from exc
        expected_intent = str(entry.get("expected_intent", "auto"))
        forbidden = tuple(str(s) for s in entry.get("forbidden_substrings", []))
        raw_aspect_forbidden = entry.get("forbidden_aspect_support_substrings", {})
        if not isinstance(raw_aspect_forbidden, dict):
            raise ValueError(
                f"{path}: question {rec_id!r} forbidden_aspect_support_substrings "
                "must be a mapping of aspect_name -> list[str]"
            )
        forbidden_aspect: dict[str, tuple[str, ...]] = {}
        for aspect_name, subs in raw_aspect_forbidden.items():
            if aspect_name not in {"company", "fundamental", "technical", "news"}:
                raise ValueError(
                    f"{path}: question {rec_id!r} forbidden_aspect_support_substrings "
                    f"aspect {aspect_name!r} must be one of company/fundamental/technical/news"
                )
            forbidden_aspect[str(aspect_name)] = tuple(str(s) for s in subs)
        if rec_id in seen_ids:
            raise ValueError(f"{path}: duplicate question id {rec_id!r}")
        if ticker not in TICKERS:
            raise ValueError(f"{path}: question {rec_id!r} references unknown ticker {ticker!r}")
        seen_ids.add(rec_id)
        records.append(
            GoldenRecord(
                id=rec_id,
                ticker=ticker,
                question=question,
                expected_tools=expected,
                reference_thesis=reference,
                expected_intent=expected_intent,
                forbidden_substrings=forbidden,
                forbidden_aspect_support_substrings=forbidden_aspect,
            )
        )
    return records


# Run-identity helpers moved to agent.evals.spine (QNT-293); aliased here under
# their historical private names so the suites and tests that import
# ``_git_sha`` / ``_prompt_version`` from golden_set keep working unchanged.
_git_sha = git_sha
_prompt_version = prompt_version


def run_record(record: GoldenRecord, *, llm_for_judge: Any | None = None) -> EvalOutcome:
    """Run a single record through the agent and score it.

    Errors during graph invocation produce a failing outcome rather than
    propagating — one broken ticker shouldn't stop a 16-record sweep.
    """
    wrapped, recorder = wrap_with_recorder(default_report_tools())
    # QNT-220 (#8): mirror production -- thesis/comparison consume the compact
    # company report. Wrap it under the same "company" recorder key so
    # tool_call_ok still logs "company" while the eval exercises the real
    # compact payload the SSE path ships.
    compact_wrapped, _ = wrap_with_recorder(
        {"company": get_company_report_compact}, recorder=recorder
    )

    started = time.perf_counter()
    try:
        graph = build_graph(wrapped, compact_company_tool=compact_wrapped["company"])
        state = graph.invoke({"ticker": record.ticker, "question": record.question})
    except Exception as exc:  # noqa: BLE001 — surface as failed row, keep loop alive
        logger.exception("eval %s: build_graph or graph.invoke raised", record.id)
        # QNT-234: distinguish a provider-capacity blow-up (Groq quota / timeout /
        # 5xx) from a genuine app/routing/code regression. The former is flagged
        # but must not gate the suite or pollute history.csv (see is_failing,
        # run_all); the latter keeps the old "graph error: <Type>" reason.
        provider = is_provider_pressure_error(exc)
        reason = provider_error_label(exc) if provider else f"graph error: {type(exc).__name__}"
        return EvalOutcome(
            record=record,
            thesis="",
            actual_tools=tuple(recorder),
            hallucination_ok=False,
            hallucination_reason=reason,
            tool_call_ok=False,
            tool_call_reason=reason,
            judge_score=None,
            cosine=0.0,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            provider_error=provider,
        )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    # QNT-133/149/156: state can carry one of four structured payloads
    # (``thesis``, ``quick_fact``, ``comparison``, ``conversational``). The
    # eval scorers (hallucination / judge / cosine) all want a flat
    # string, so render through ``to_markdown`` here rather than push the
    # per-shape contract into each scorer. Comparison runs check
    # hallucination against the union of all per-ticker reports;
    # conversational runs treat ANY digit as a hallucination per the
    # QNT-156 guardrail.
    thesis_obj = state.get("thesis")
    quick_fact_obj = state.get("quick_fact")
    comparison_obj = state.get("comparison")
    conversational_obj = state.get("conversational")
    focused_obj = state.get("focused")
    exploration_obj = state.get("exploration")
    if isinstance(comparison_obj, ComparisonAnswer):
        thesis = comparison_obj.to_markdown()
    elif isinstance(thesis_obj, Thesis):
        thesis = thesis_obj.to_markdown()
    elif isinstance(quick_fact_obj, QuickFactAnswer):
        thesis = quick_fact_obj.to_markdown()
    elif isinstance(focused_obj, FocusedAnalysis):
        thesis = focused_obj.to_markdown()
    elif isinstance(exploration_obj, ExplorationAnswer):
        thesis = exploration_obj.to_markdown()
    elif isinstance(conversational_obj, ConversationalAnswer):
        thesis = conversational_obj.to_markdown()
    else:
        thesis = ""
    reports = dict(state.get("reports") or {})

    # Comparison runs gather reports per ticker — flatten to a corpus the
    # hallucination scorer can scan against.
    reports_by_ticker = state.get("reports_by_ticker") or {}
    if reports_by_ticker:
        flat_reports: list[str] = []
        for ticker_reports in reports_by_ticker.values():
            flat_reports.extend(ticker_reports.values())
    else:
        flat_reports = list(reports.values())

    # Conversational answers must contain NO digits (QNT-156 guardrail);
    # the hallucination scorer's "every number must appear in a report"
    # rule already enforces this when reports are empty (any digit ⇒ flag).
    hresult = check_hallucination(thesis, flat_reports)
    tresult = check_tool_calls(record.expected_tools, recorder)
    judge_score = judge_score_fn(
        record.question, thesis, record.reference_thesis, llm=llm_for_judge
    )
    cosine_score = cosine(thesis, record.reference_thesis)

    # QNT-184: forbidden_substrings — policy-violation check folded into
    # hallucination_ok so it gates the same hard exit-code path without
    # adding a new CSV column or EvalOutcome field.
    thesis_lower = thesis.lower()
    violated = [s for s in record.forbidden_substrings if s.lower() in thesis_lower]
    hallucination_ok = hresult.ok
    hallucination_reason = hresult.reason()
    if violated:
        hallucination_ok = False
        hallucination_reason = f"forbidden: {', '.join(repr(s) for s in violated)}"
    elif record.forbidden_aspect_support_substrings and isinstance(thesis_obj, Thesis):
        # QNT-208: forbidden_aspect_support_substrings — per-aspect contract.
        # Scoped to each aspect's ``supports`` list so challenges occurrences
        # of the same term don't trigger a false positive (e.g. "overbought"
        # is correct in technical.challenges).
        aspect_map = {
            "company": thesis_obj.company,
            "fundamental": thesis_obj.fundamental,
            "technical": thesis_obj.technical,
            "news": thesis_obj.news,
        }
        aspect_violations: list[str] = []
        for aspect_name, subs in record.forbidden_aspect_support_substrings.items():
            aspect = aspect_map.get(aspect_name)
            if aspect is None:
                continue
            supports_text = " ".join(aspect.supports).lower()
            for sub in subs:
                if sub.lower() in supports_text:
                    aspect_violations.append(f"{aspect_name}.supports: {sub!r}")
        if aspect_violations:
            hallucination_ok = False
            hallucination_reason = f"forbidden in supports: {', '.join(aspect_violations)}"

    # QNT-208: verdict_consistency — the v2 verdict_rationale must mention
    # at least one aspect label verbatim (Premium, Inline, Discounted,
    # Uptrend, Sideways, Downtrend). Folded into hallucination_ok so any
    # rationale that drifts off-vocabulary gates the same exit code.
    if hallucination_ok and isinstance(thesis_obj, Thesis):
        rationale_lower = thesis_obj.verdict_rationale.lower()
        aspect_labels = ("premium", "inline", "discounted", "uptrend", "sideways", "downtrend")
        if not any(label in rationale_lower for label in aspect_labels):
            hallucination_ok = False
            hallucination_reason = (
                "verdict_consistency: verdict_rationale must mention at least "
                "one aspect label verbatim (Premium/Inline/Discounted/"
                "Uptrend/Sideways/Downtrend)"
            )

    # QNT-302: advisory verdict-vs-labels tripwire. Recorded per structured
    # thesis so the golden run surfaces the observed mismatch rate; never gates
    # the exit code (the Thesis model_validator already logs each mismatch).
    verdict_label_consistent = (
        thesis_obj.verdict_matches_labels() if isinstance(thesis_obj, Thesis) else None
    )

    return EvalOutcome(
        record=record,
        thesis=thesis,
        actual_tools=tuple(recorder),
        hallucination_ok=hallucination_ok,
        hallucination_reason=hallucination_reason,
        verdict_label_consistent=verdict_label_consistent,
        tool_call_ok=tresult.ok,
        tool_call_reason=tresult.reason(),
        judge_score=judge_score,
        cosine=cosine_score,
        elapsed_ms=elapsed_ms,
        reports=tuple(flat_reports),
    )


def append_history(
    outcomes: list[EvalOutcome],
    *,
    run_id: str | None = None,
    history_path: Path = GOLDEN_HISTORY_PATH,
) -> str:
    """Append one row per outcome to ``golden_history.csv``. Returns the run_id.

    QNT-293 follow-up: golden rows now go to the golden suite's own file (columns
    = :data:`GOLDEN_FIELDS`) via :func:`spine.append_suite_history`, which stamps
    the shared envelope. Creates the file with a header if absent so the first run
    also produces a self-describing artefact. ``history_path`` overrides the
    default file (CI / experimentation / tests).
    """
    rid = run_id or uuid.uuid4().hex[:8]

    def _rows() -> Iterator[dict[str, Any]]:
        for outcome in outcomes:
            js = outcome.judge_score
            yield {
                "eval_type": "structured",
                "ticker": outcome.record.ticker,
                "question_id": outcome.record.id,
                "question": outcome.record.question,
                "faithfulness": "" if js is None else js.faithfulness,
                "structure": "" if js is None else js.structure,
                "correctness": "" if js is None else js.correctness,
                "analyst_logic": "" if js is None else js.analyst_logic,
                "composite": "" if js is None else js.composite,
                "cosine": outcome.cosine,
                "tool_call_ok": "1" if outcome.tool_call_ok else "0",
                "hallucination_ok": "1" if outcome.hallucination_ok else "0",
                "verdict_label_consistent": (
                    ""
                    if outcome.verdict_label_consistent is None
                    else ("1" if outcome.verdict_label_consistent else "0")
                ),
                "elapsed_ms": outcome.elapsed_ms,
            }

    return append_suite_history("golden", GOLDEN_FIELDS, _rows(), run_id=rid, path=history_path)


def run_all(
    *,
    history_path: Path = GOLDEN_HISTORY_PATH,
    only: str | None = None,
    llm_for_judge: Any | None = None,
    run_id_suffix: str | None = None,
) -> tuple[str, list[EvalOutcome]]:
    """Run every record in the golden set and return ``(run_id, outcomes)``.

    ``only`` filters to a single ticker (case-insensitive) — useful for
    iterating on one report's prompt without paying for the full sweep.

    ``run_id_suffix`` appends a tag to the generated run_id (QNT-129 bench
    harness uses the model alias suffix so per-model aggregates over
    history.csv can ``startswith`` / ``endswith`` filter without a schema
    column).
    """
    records = load_goldens()
    if only is not None:
        wanted = only.upper()
        records = [r for r in records if r.ticker == wanted]
        if not records:
            raise ValueError(f"no golden records for ticker {wanted!r}")

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rid = f"{timestamp}-{uuid.uuid4().hex[:6]}"
    if run_id_suffix:
        rid = f"{rid}-{run_id_suffix}"
    outcomes = [run_record(rec, llm_for_judge=llm_for_judge) for rec in records]
    # QNT-234: provider-pressure failures (Groq quota / timeout) are
    # infrastructure, not code-quality measurements -- appending their 0/0 rows
    # would corrupt the committed history.csv trend (git log -p evals/history.csv)
    # by reading as regressions below this commit. Keep them out of history; they
    # still surface in the stdout summary (provider_pressure_warning) and in the
    # returned outcomes the caller inspects.
    measured = [o for o in outcomes if not o.provider_error]
    append_history(measured, run_id=rid, history_path=history_path)
    return rid, outcomes


def summarise(outcomes: list[EvalOutcome]) -> str:
    """One-line aggregate + per-record breakdown for stdout."""
    total = len(outcomes)
    if total == 0:
        return "no records evaluated"
    halluc_ok = sum(1 for o in outcomes if o.hallucination_ok)
    tools_ok = sum(1 for o in outcomes if o.tool_call_ok)
    provider_failures = sum(1 for o in outcomes if o.provider_error)
    judged = [o.judge_score for o in outcomes if o.judge_score is not None]
    avg_cosine = round(sum(o.cosine for o in outcomes) / total, 3)

    # QNT-302: advisory verdict-vs-labels tripwire aggregate over structured
    # theses only (None on other shapes). Reported, never gated.
    thesis_outcomes = [o for o in outcomes if o.verdict_label_consistent is not None]
    if thesis_outcomes:
        verdict_ok = sum(1 for o in thesis_outcomes if o.verdict_label_consistent)
        verdict_summary = f"verdict_label_consistent: {verdict_ok}/{len(thesis_outcomes)}"
    else:
        verdict_summary = "verdict_label_consistent: n/a"

    if judged:
        avg_composite = round(sum(js.composite for js in judged) / len(judged), 2)
        avg_faithfulness = round(sum(js.faithfulness for js in judged) / len(judged), 2)
        avg_structure = round(sum(js.structure for js in judged) / len(judged), 2)
        avg_correctness = round(sum(js.correctness for js in judged) / len(judged), 2)
        avg_analyst_logic = round(sum(js.analyst_logic for js in judged) / len(judged), 2)
        judge_summary = (
            f"composite={avg_composite} "
            f"(F={avg_faithfulness} S={avg_structure} "
            f"C={avg_correctness} A={avg_analyst_logic})"
        )
    else:
        judge_summary = "n/a"

    lines: list[str] = []
    # QNT-234: lead with the contamination banner so a provider-throttled run is
    # never read top-down as a code regression.
    warning = provider_pressure_warning(outcomes)
    if warning is not None:
        lines += [warning, ""]
    lines += [
        f"records: {total}  hallucination_ok: {halluc_ok}/{total}  "
        f"tool_call_ok: {tools_ok}/{total}  "
        f"provider_failures: {provider_failures}/{total}  "
        f"avg_judge: {judge_summary}  "
        f"avg_cosine: {avg_cosine}  "
        f"{verdict_summary}",
        "",
        "per-record:",
    ]
    for o in outcomes:
        js = o.judge_score
        if js is not None:
            judge_tag = (
                f" judge={js.composite}"
                f"(F={js.faithfulness} S={js.structure}"
                f" C={js.correctness} A={js.analyst_logic})"
            )
        else:
            judge_tag = " judge=n/a"
        # QNT-234: a provider failure is not a contract result -- mark it [P] so
        # it reads distinctly from a hallucination ([h]) or tool-call ([t]) miss.
        if o.provider_error:
            marks = "P" + judge_tag
        else:
            marks = (
                ("H" if o.hallucination_ok else "h") + ("T" if o.tool_call_ok else "t") + judge_tag
            )
        lines.append(
            f"  [{marks}] {o.record.id:24s} {o.record.ticker:5s} "
            f"cos={o.cosine:.2f} elapsed={o.elapsed_ms}ms — "
            f"{o.hallucination_reason} | {o.tool_call_reason}"
        )
    return "\n".join(lines)


def provider_pressure_warning(outcomes: list[EvalOutcome]) -> str | None:
    """Flag a run contaminated by Groq quota / throttle / timeout pressure (QNT-234).

    Returns a warning string when the run's failures or latency point at provider
    capacity rather than the agent code, else None. Two signals:

    * ``provider_error`` rows -- ``graph.invoke`` raised a rate-limit / timeout /
      5xx the classifier recognised. Excluded from history.csv and the exit gate.
    * latency-contaminated rows -- a record whose wall time cleared one full
      :data:`CONTAMINATION_LATENCY_MS` (an LLM call ran to its ceiling). Synthesis
      catches its own timeout and degrades to a redirect, so the contract may
      still pass while the judge score is throttle-depressed; flag it so a
      contaminated aggregate is not mistaken for a quality regression.

    Mirrors :func:`agent.evals.dialogue_eval.contamination_warning`: when this
    fires, re-run on a clean rate-limit window before trusting the numbers
    (QNT-218 rationale).
    """
    if not outcomes:
        return None
    provider_errors = [o for o in outcomes if o.provider_error]
    slow = [
        o for o in outcomes if not o.provider_error and o.elapsed_ms >= CONTAMINATION_LATENCY_MS
    ]
    if not provider_errors and not slow:
        return None
    parts = [
        "PROVIDER-PRESSURE CONTAMINATION -- do not read these failures as code "
        f"regressions. {len(provider_errors)} provider error(s) "
        "(quota/timeout/5xx, excluded from history.csv and the exit gate); "
        f"{len(slow)} record(s) over the {CONTAMINATION_LATENCY_MS}ms "
        "timeout-ceiling floor (likely throttle-degraded scores)."
    ]
    if provider_errors:
        parts.append(
            "  provider errors: "
            + ", ".join(f"{o.record.id}({o.hallucination_reason})" for o in provider_errors)
        )
    if slow:
        parts.append(
            "  slow records: " + ", ".join(f"{o.record.id}={o.elapsed_ms}ms" for o in slow)
        )
    parts.append("Re-run on a clean rate-limit window before trusting the aggregate.")
    return "\n".join(parts)


def is_failing(outcomes: list[EvalOutcome]) -> bool:
    """Return True if any outcome is a hallucination or tool-call failure,
    OR the outcomes list is empty.

    The judge score is treated as soft signal: an LLM judge can disagree
    without there being a contract violation. Hallucination + tool-call are
    hard contracts — those gate exit codes (``__main__`` reads this).

    Empty input fails the gate: a malformed YAML stub or an upstream filter
    that strips every record would otherwise let ``any([])`` quietly pass
    the suite. Surface "evaluated zero records" as a failure so a broken
    golden file can't masquerade as a clean run.

    QNT-234: provider-pressure failures (Groq quota / timeout / 5xx) are NOT
    code regressions and must not gate the exit code, or a QNT-233-style routing
    fix gets blocked by free-tier capacity instead of by its own correctness --
    EXCEPT when every record is a provider failure (a full outage), which leaves
    zero usable measurements and so gates like the empty-outcomes case. They
    surface via :func:`provider_pressure_warning` regardless.
    """
    if not outcomes:
        return True
    measured = [o for o in outcomes if not o.provider_error]
    if not measured:
        # QNT-234: every record died on provider pressure -- we measured zero
        # usable rows, so this is "evaluated nothing" (a full Groq outage), not a
        # clean pass. Gate it like the empty-outcomes case rather than exit 0;
        # provider_pressure_warning explains why in the summary. This does NOT
        # conflict with the AC3 intent: that protects a MIXED run where real
        # records still passed -- there, ``measured`` is non-empty and provider
        # rows are simply excluded below.
        return True
    return any(not o.hallucination_ok or not o.tool_call_ok for o in measured)


def fail_threshold_from_env() -> float | None:
    """Optional minimum average judge score, configured via ``EVAL_MIN_JUDGE``.

    Off by default so the gate stays on hard contracts. Set ``EVAL_MIN_JUDGE=7``
    in CI once the harness has produced enough history to trust a threshold.
    Thin golden-set binding over the shared :func:`spine.threshold_from_env`
    (QNT-293) so every suite parses env thresholds the same way.
    """
    return threshold_from_env("EVAL_MIN_JUDGE")


__all__ = [
    "CONTAMINATION_LATENCY_MS",
    "EvalOutcome",
    "GoldenRecord",
    "GOLDENS_PATH",
    "GOLDEN_FIELDS",
    "GOLDEN_HISTORY_PATH",
    "HISTORY_FIELDS",
    "HISTORY_PATH",
    "append_history",
    "fail_threshold_from_env",
    "is_failing",
    "load_goldens",
    "provider_pressure_warning",
    "run_all",
    "run_record",
    "summarise",
]
