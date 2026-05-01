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

import csv
import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from shared.tickers import TICKERS

from agent.comparison import ComparisonAnswer
from agent.conversational import ConversationalAnswer
from agent.evals.hallucination import check as check_hallucination
from agent.evals.judge import score as judge_score_fn
from agent.evals.similarity import cosine
from agent.evals.tool_calls import check as check_tool_calls
from agent.evals.tool_calls import wrap_with_recorder
from agent.graph import build_graph
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from agent.tools import default_report_tools

logger = logging.getLogger(__name__)

GOLDENS_PATH = Path(__file__).parent / "goldens" / "questions.yaml"
HISTORY_PATH = Path(__file__).parent / "history.csv"

HISTORY_FIELDS = (
    "run_id",
    "git_sha",
    "prompt_version",
    "ticker",
    "question_id",
    "question",
    "judge_score",
    "cosine",
    "tool_call_ok",
    "hallucination_ok",
    "elapsed_ms",
)


@dataclass(frozen=True)
class GoldenRecord:
    """One row from goldens/questions.yaml.

    ``expected_intent`` defaults to "auto" — the harness lets the
    classifier decide and just scores whatever shape comes out. Setting it
    explicitly is informational; the conversational shape additionally
    permits an empty ``expected_tools`` list (the path skips gather).
    """

    id: str
    ticker: str
    question: str
    expected_tools: tuple[str, ...]
    reference_thesis: str
    expected_intent: str = "auto"


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
    judge_score: int | None
    cosine: float
    elapsed_ms: int


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
            )
        )
    return records


def _git_sha() -> str:
    """Short SHA of HEAD, or ``unknown`` if git isn't reachable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def _prompt_version() -> str:
    """Stable hash of the system prompts + report-tool registry.

    Hashing all five (thesis + quick-fact + comparison + conversational +
    tools) keeps a tool-name rename or a prompt edit on any path visible in
    history.csv as a different ``prompt_version`` — so a regression
    showing "judge 8 → 5 the day prompt_version changed" reads obviously
    in the diff.
    """
    from hashlib import sha256

    from agent.prompts import (
        COMPARISON_SYSTEM_PROMPT,
        CONVERSATIONAL_SYSTEM_PROMPT,
        QUICK_FACT_SYSTEM_PROMPT,
        REPORT_TOOLS,
        SYSTEM_PROMPT,
    )

    payload = (
        SYSTEM_PROMPT
        + "\n"
        + QUICK_FACT_SYSTEM_PROMPT
        + "\n"
        + COMPARISON_SYSTEM_PROMPT
        + "\n"
        + CONVERSATIONAL_SYSTEM_PROMPT
        + "\n"
        + ",".join(sorted(REPORT_TOOLS))
    )
    return sha256(payload.encode("utf-8")).hexdigest()[:10]


def run_record(record: GoldenRecord, *, llm_for_judge: Any | None = None) -> EvalOutcome:
    """Run a single record through the agent and score it.

    Errors during graph invocation produce a failing outcome rather than
    propagating — one broken ticker shouldn't stop a 16-record sweep.
    """
    wrapped, recorder = wrap_with_recorder(default_report_tools())

    started = time.perf_counter()
    try:
        graph = build_graph(wrapped)
        state = graph.invoke({"ticker": record.ticker, "question": record.question})
    except Exception as exc:  # noqa: BLE001 — surface as failed row, keep loop alive
        logger.exception("eval %s: build_graph or graph.invoke raised", record.id)
        return EvalOutcome(
            record=record,
            thesis="",
            actual_tools=tuple(recorder),
            hallucination_ok=False,
            hallucination_reason=f"graph error: {type(exc).__name__}",
            tool_call_ok=False,
            tool_call_reason=f"graph error: {type(exc).__name__}",
            judge_score=None,
            cosine=0.0,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
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
    if isinstance(comparison_obj, ComparisonAnswer):
        thesis = comparison_obj.to_markdown()
    elif isinstance(thesis_obj, Thesis):
        thesis = thesis_obj.to_markdown()
    elif isinstance(quick_fact_obj, QuickFactAnswer):
        thesis = quick_fact_obj.to_markdown()
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

    return EvalOutcome(
        record=record,
        thesis=thesis,
        actual_tools=tuple(recorder),
        hallucination_ok=hresult.ok,
        hallucination_reason=hresult.reason(),
        tool_call_ok=tresult.ok,
        tool_call_reason=tresult.reason(),
        judge_score=judge_score,
        cosine=cosine_score,
        elapsed_ms=elapsed_ms,
    )


def append_history(
    outcomes: list[EvalOutcome],
    *,
    run_id: str | None = None,
    history_path: Path = HISTORY_PATH,
) -> str:
    """Append one row per outcome to ``history.csv``. Returns the run_id used.

    Creates the file with a header if absent so the first run also produces
    a self-describing artefact (one of the AC items: "history.csv committed
    with at least one row after first run").

    Concurrency: the existence-check + write is not atomic. Two parallel
    eval processes racing on a fresh file could both write the header. Not
    a problem for the single-developer dev tool this is today; if extracted
    as a standalone CI service, wrap with a file lock or pre-create the
    header out-of-band.
    """
    rid = run_id or uuid.uuid4().hex[:8]
    sha = _git_sha()
    pv = _prompt_version()
    new_file = not history_path.exists()
    history_path.parent.mkdir(parents=True, exist_ok=True)

    with history_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
        if new_file:
            writer.writeheader()
        for outcome in outcomes:
            writer.writerow(
                {
                    "run_id": rid,
                    "git_sha": sha,
                    "prompt_version": pv,
                    "ticker": outcome.record.ticker,
                    "question_id": outcome.record.id,
                    "question": outcome.record.question,
                    "judge_score": "" if outcome.judge_score is None else outcome.judge_score,
                    "cosine": outcome.cosine,
                    "tool_call_ok": "1" if outcome.tool_call_ok else "0",
                    "hallucination_ok": "1" if outcome.hallucination_ok else "0",
                    "elapsed_ms": outcome.elapsed_ms,
                }
            )
    return rid


def run_all(
    *,
    history_path: Path = HISTORY_PATH,
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
    append_history(outcomes, run_id=rid, history_path=history_path)
    return rid, outcomes


def summarise(outcomes: list[EvalOutcome]) -> str:
    """One-line aggregate + per-record breakdown for stdout."""
    total = len(outcomes)
    if total == 0:
        return "no records evaluated"
    halluc_ok = sum(1 for o in outcomes if o.hallucination_ok)
    tools_ok = sum(1 for o in outcomes if o.tool_call_ok)
    judged = [o.judge_score for o in outcomes if o.judge_score is not None]
    avg_judge = round(sum(judged) / len(judged), 2) if judged else None
    avg_cosine = round(sum(o.cosine for o in outcomes) / total, 3)

    lines = [
        f"records: {total}  hallucination_ok: {halluc_ok}/{total}  "
        f"tool_call_ok: {tools_ok}/{total}  "
        f"avg_judge: {avg_judge if avg_judge is not None else 'n/a'}  "
        f"avg_cosine: {avg_cosine}",
        "",
        "per-record:",
    ]
    for o in outcomes:
        marks = (
            ("H" if o.hallucination_ok else "h")
            + ("T" if o.tool_call_ok else "t")
            + (f" judge={o.judge_score}" if o.judge_score is not None else " judge=n/a")
        )
        lines.append(
            f"  [{marks}] {o.record.id:24s} {o.record.ticker:5s} "
            f"cos={o.cosine:.2f} elapsed={o.elapsed_ms}ms — "
            f"{o.hallucination_reason} | {o.tool_call_reason}"
        )
    return "\n".join(lines)


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
    """
    if not outcomes:
        return True
    return any(not o.hallucination_ok or not o.tool_call_ok for o in outcomes)


def fail_threshold_from_env() -> float | None:
    """Optional minimum average judge score, configured via ``EVAL_MIN_JUDGE``.

    Off by default so the gate stays on hard contracts. Set ``EVAL_MIN_JUDGE=7``
    in CI once the harness has produced enough history to trust a threshold.

    CLAUDE.md says config goes through ``shared.Settings``; this is a
    deliberate exception. The setting is a developer-time eval gate, off by
    default and probably never used in prod. Adding a field to the global
    Settings object so a never-used eval threshold can be configured the
    same way as ``CLICKHOUSE_HOST`` would pollute the runtime contract.
    Promote to ``Settings`` if a second eval-only knob ever shows up.
    """
    raw = os.environ.get("EVAL_MIN_JUDGE")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("EVAL_MIN_JUDGE=%r not a number; ignoring", raw)
        return None


__all__ = [
    "EvalOutcome",
    "GoldenRecord",
    "GOLDENS_PATH",
    "HISTORY_FIELDS",
    "HISTORY_PATH",
    "append_history",
    "fail_threshold_from_env",
    "is_failing",
    "load_goldens",
    "run_all",
    "run_record",
    "summarise",
]
