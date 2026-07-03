"""Dialogue-quality eval harness for multi-turn agent runs (QNT-214)."""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean, median, stdev
from typing import Any, cast

import httpx
import yaml
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from shared.config import settings
from shared.tickers import TICKERS

from agent.analyst_voice import find_filler
from agent.evals.dialogue_judge import (
    AGENT_UNDER_TEST_RESOLVED_MODEL,
    JUDGE_MODEL_ALIAS,
    JUDGE_RESOLVED_MODEL,
    DialogueJudgeScore,
)
from agent.evals.dialogue_judge import score as judge_score_fn
from agent.evals.golden_set import HISTORY_FIELDS, HISTORY_PATH, _git_sha, _prompt_version
from agent.evals.hallucination import HallucinationResult
from agent.evals.hallucination import check as check_hallucination
from agent.graph import build_graph
from agent.llm import current_model_info, set_temperature_override
from agent.tools import default_report_tools, get_company_report_compact
from agent.tracing import flush as flush_langfuse
from agent.tracing import langfuse, make_callback_handler, propagate_attributes

logger = logging.getLogger(__name__)

DIALOGUE_GOLDENS_PATH = Path(__file__).parent / "goldens" / "dialogue.yaml"
DIALOGUE_AXES = (
    "analyst_likeness",
    "helpfulness",
    "non_hallucination",
    "exploration_quality",
    "voice_match",
)

# QNT-218: the agent-under-test runs at temperature 0 during the eval so its
# sampling variance stops polluting the measurement. Judge is already temp 0.
EVAL_AGENT_TEMPERATURE = 0.0

# QNT-218: a clean dialogue sweep runs each fixture in ~25-30s. Groq throttling
# pushes that to 45-62s with scattered judge failures. Flag a run whose median
# fixture latency clears this so a contaminated aggregate is never trusted.
CONTAMINATION_LATENCY_MS = 40_000

# QNT-218: the restated QNT-215 merge gate. QNT-215's spec is "analyst_likeness
# +0.10, exploration_quality +0.15, no regression elsewhere". So the LIFT axes
# must show a per-fixture mean delta that clears GATE_K * SE_delta (significant
# lift), and every OTHER axis is a must-not-regress guardrail (mean delta no worse
# than -GATE_K * SE_delta) -- including the deterministic non_hallucination the
# tracer already pins, plus helpfulness and voice_match ("elsewhere"). Keep the
# two tuples a partition of DIALOGUE_AXES so no axis goes silently unchecked.
GATE_LIFT_AXES = ("analyst_likeness", "exploration_quality")
GATE_GUARDRAIL_AXES = ("non_hallucination", "helpfulness", "voice_match")
GATE_K = 2.0


@dataclass(frozen=True)
class DialogueFixture:
    id: str
    ticker: str
    turns: tuple[str, ...]
    expected_signals: tuple[str, ...]
    anchors: dict[str, str]


@dataclass(frozen=True)
class DialogueOutcome:
    fixture: DialogueFixture
    transcript: str
    narrative: str
    structured_payload: str
    numeric_support: HallucinationResult
    judge_score: DialogueJudgeScore | None
    trace_id: str | None
    elapsed_ms: int


@dataclass(frozen=True)
class DialogueAggregate:
    """Per-run descriptive band over the scored fixtures (QNT-218).

    ``axis_se`` is the standard error of one run's axis mean across the
    fixtures -- it describes how scattered this single sweep is, NOT the
    run-to-run noise and NOT a lift test. A QNT-215 lift is a two-run paired
    quantity (see :func:`paired_delta_gate`); one run can only report its own
    scatter.
    """

    n: int
    axis_mean: dict[str, float]
    axis_se: dict[str, float]
    composite_mean: float
    composite_se: float


def _std_error(values: list[float]) -> float:
    """Standard error of the mean; 0.0 when fewer than two samples."""
    if len(values) < 2:
        return 0.0
    return stdev(values) / math.sqrt(len(values))


def aggregate(outcomes: list[DialogueOutcome]) -> DialogueAggregate | None:
    """Per-axis mean + standard error across the judged fixtures of one run."""
    judged = [o.judge_score for o in outcomes if o.judge_score is not None]
    if not judged:
        return None
    axis_mean: dict[str, float] = {}
    axis_se: dict[str, float] = {}
    for axis in DIALOGUE_AXES:
        values = [float(getattr(js, axis).score) for js in judged]
        axis_mean[axis] = fmean(values)
        axis_se[axis] = _std_error(values)
    composites = [float(js.composite) for js in judged]
    return DialogueAggregate(
        n=len(judged),
        axis_mean=axis_mean,
        axis_se=axis_se,
        composite_mean=fmean(composites),
        composite_se=_std_error(composites),
    )


@dataclass(frozen=True)
class AxisGateResult:
    """One axis verdict from the paired QNT-215 gate (QNT-218)."""

    axis: str
    kind: str  # "lift" | "guardrail"
    n: int
    mean_delta: float
    se_delta: float
    threshold: float  # GATE_K * se_delta
    passed: bool


def load_dialogue_scores(
    run_id: str, *, history_path: Path = HISTORY_PATH
) -> dict[str, dict[str, float]]:
    """Read one run's per-fixture axis scores: ``fixture_id -> {axis: score}``.

    Skips the ``eval_type="dialogue_summary"`` aggregate row so the gate pairs
    raw per-fixture draws, not the run-level mean.
    """
    scores: dict[str, dict[str, float]] = {}
    if not history_path.exists():
        return scores
    with history_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("run_id") != run_id or row.get("eval_type") != "dialogue":
                continue
            fid = row.get("dialogue_fixture_id") or row.get("question_id") or ""
            axis_scores: dict[str, float] = {}
            for axis in DIALOGUE_AXES:
                raw = row.get(axis)
                if raw not in (None, ""):
                    axis_scores[axis] = float(raw)
            if fid and axis_scores:
                scores[fid] = axis_scores
    return scores


def paired_delta_gate(
    baseline: dict[str, dict[str, float]],
    candidate: dict[str, dict[str, float]],
    *,
    lift_axes: tuple[str, ...] = GATE_LIFT_AXES,
    guardrail_axes: tuple[str, ...] = GATE_GUARDRAIL_AXES,
    k: float = GATE_K,
) -> list[AxisGateResult]:
    """Restated QNT-215 gate: a paired per-fixture lift test (QNT-218).

    The fixtures are shared between the baseline and candidate runs, so the
    correct test pairs them: ``delta_i = candidate_i - baseline_i`` on each
    shared fixture, then ``SE_delta = sd(delta_i) / sqrt(n)``. Pairing cancels
    the shared fixture-difficulty component that an independent two-sample SE
    would double-count, so it is both tighter and conceptually right -- a lift
    is inherently a two-run quantity, which is why this consumes per-fixture
    rows rather than the single-run dispersion band from :func:`aggregate`.

    A lift axis passes when ``mean_delta > k * SE_delta`` (significant gain) and
    at least two fixtures were paired. Every other axis is a guardrail -- the
    deterministic ``non_hallucination`` plus ``helpfulness`` and ``voice_match``
    (QNT-215's "no regression elsewhere") -- and passes when it does not
    significantly regress: ``mean_delta >= -k * SE_delta``. Intended for the full
    12-fixture set; with ``n < 2`` a lift cannot be significant and fails.
    """
    shared = sorted(set(baseline) & set(candidate))
    results: list[AxisGateResult] = []
    for axis in (*lift_axes, *guardrail_axes):
        deltas = [
            candidate[f][axis] - baseline[f][axis]
            for f in shared
            if axis in baseline[f] and axis in candidate[f]
        ]
        n = len(deltas)
        mean_delta = fmean(deltas) if deltas else 0.0
        se_delta = _std_error(deltas)
        threshold = k * se_delta
        kind = "guardrail" if axis in guardrail_axes else "lift"
        if kind == "guardrail":
            passed = mean_delta >= -threshold
        else:
            # A lift needs at least two paired fixtures to establish significance;
            # n<2 leaves se_delta=0, which would otherwise pass on any tiny delta.
            passed = n >= 2 and mean_delta > threshold
        results.append(AxisGateResult(axis, kind, n, mean_delta, se_delta, threshold, passed))
    return results


def gate_passed(results: list[AxisGateResult]) -> bool:
    """Overall QNT-215 verdict: every gated axis must pass."""
    return bool(results) and all(r.passed for r in results)


def format_gate(results: list[AxisGateResult]) -> str:
    """Human-readable paired-gate report for stdout."""
    if not results:
        return "gate: no shared fixtures to compare"
    lines = [f"QNT-215 gate (paired, k={GATE_K}): {'PASS' if gate_passed(results) else 'FAIL'}"]
    for r in results:
        mark = "ok" if r.passed else "XX"
        lines.append(
            f"  [{mark}] {r.axis:20s} {r.kind:9s} "
            f"mean_delta={r.mean_delta:+.3f} se={r.se_delta:.3f} "
            f"threshold={r.threshold:+.3f} (n={r.n})"
        )
    return "\n".join(lines)


def load_dialogues(path: Path = DIALOGUE_GOLDENS_PATH) -> list[DialogueFixture]:
    """Parse dialogue fixtures from YAML and validate the dialogue axes."""
    raw = yaml.safe_load(path.read_text())
    entries = raw.get("dialogues") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{path}: missing top-level `dialogues` list")

    records: list[DialogueFixture] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each dialogue must be a mapping, got {type(entry)}")
        try:
            fixture_id = str(entry["id"])
            ticker = str(entry["ticker"]).upper()
            raw_turns = entry["turns"]
            expected = tuple(str(axis) for axis in entry["expected_signals"])
        except KeyError as exc:
            raise ValueError(f"{path}: dialogue missing field {exc}") from exc
        if fixture_id in seen:
            raise ValueError(f"{path}: duplicate dialogue id {fixture_id!r}")
        if ticker not in TICKERS:
            raise ValueError(
                f"{path}: dialogue {fixture_id!r} references unknown ticker {ticker!r}"
            )
        if not isinstance(raw_turns, list) or not raw_turns:
            raise ValueError(f"{path}: dialogue {fixture_id!r} must have at least one turn")
        turns = tuple(_parse_user_turn(path, fixture_id, raw_turn) for raw_turn in raw_turns)
        unknown_axes = set(expected) - set(DIALOGUE_AXES)
        if unknown_axes:
            raise ValueError(
                f"{path}: dialogue {fixture_id!r} has unknown expected_signals "
                f"{sorted(unknown_axes)}"
            )
        anchors_raw = entry.get("anchors") or {}
        if not isinstance(anchors_raw, dict):
            raise ValueError(f"{path}: dialogue {fixture_id!r} anchors must be a mapping")
        records.append(
            DialogueFixture(
                id=fixture_id,
                ticker=ticker,
                turns=turns,
                expected_signals=expected,
                anchors={str(k): str(v) for k, v in anchors_raw.items()},
            )
        )
        seen.add(fixture_id)
    return records


def _parse_user_turn(path: Path, fixture_id: str, raw_turn: object) -> str:
    if isinstance(raw_turn, str):
        content = raw_turn
    elif isinstance(raw_turn, dict):
        content = str(raw_turn.get("user", ""))
    else:
        raise ValueError(f"{path}: dialogue {fixture_id!r} turn must be a string or mapping")
    content = content.strip()
    if not content:
        raise ValueError(f"{path}: dialogue {fixture_id!r} contains an empty user turn")
    return content


def _render_payload(state: dict[str, Any]) -> str:
    """Render the user-visible structured payload, if any, to markdown."""
    for key in ("comparison", "conversational", "quick_fact", "focused", "exploration", "thesis"):
        raw = state.get(key)
        to_markdown = getattr(raw, "to_markdown", None)
        if callable(to_markdown):
            return str(to_markdown())
    return ""


def _flatten_reports(state: dict[str, Any]) -> list[str]:
    reports_by_ticker = state.get("reports_by_ticker") or {}
    if reports_by_ticker:
        flat: list[str] = []
        for ticker_reports in reports_by_ticker.values():
            if isinstance(ticker_reports, dict):
                flat.extend(str(v) for v in ticker_reports.values())
        return flat
    reports = state.get("reports") or {}
    return [str(v) for v in reports.values()] if isinstance(reports, dict) else []


def _transcript_from_state(state: dict[str, Any], fallback_turns: tuple[str, ...]) -> str:
    messages = state.get("messages") or []
    if isinstance(messages, list) and messages:
        lines: list[str] = []
        for message in messages:
            if isinstance(message, dict):
                role = str(message.get("role", "unknown"))
                content = str(message.get("content", ""))
                lines.append(f"{role}: {content}")
        if lines:
            return "\n".join(lines)
    return "\n".join(f"user: {turn}" for turn in fallback_turns)


def _numeric_support_text(result: HallucinationResult) -> str:
    if result.ok:
        return "clean"
    return result.reason()


def _apply_deterministic_numeric_gate(
    score: DialogueJudgeScore | None,
    numeric_support: HallucinationResult,
) -> DialogueJudgeScore | None:
    if score is None or numeric_support.ok:
        return score
    score.non_hallucination.score = 0.0
    score.non_hallucination.rationale = (
        f"Deterministic numeric checker failed: {numeric_support.reason()}."
    )
    return score


def _apply_deterministic_filler_gate(
    score: DialogueJudgeScore | None,
    narrative: str,
) -> DialogueJudgeScore | None:
    """QNT-303 (D-6): cap ``voice_match`` at 0 when banned filler is present.

    The no-regret twin of the numeric gate above: soft padding a senior desk
    never writes ("it's important to note", a leading "Overall,") is a voice
    failure by definition, so it overrides whatever the LLM judge scored on
    ``voice_match``. Enforced on every fixture regardless of the judge outcome.
    """
    if score is None:
        return score
    filler = find_filler(narrative)
    if not filler:
        return score
    score.voice_match.score = 0.0
    score.voice_match.rationale = (
        f"Deterministic filler check failed: banned analyst-voice filler: {', '.join(filler)}."
    )
    return score


def run_fixture(
    fixture: DialogueFixture,
    *,
    llm_for_judge: Any | None = None,
    emit_langfuse_scores: bool = False,
) -> DialogueOutcome:
    """Replay one multi-turn fixture through the agent and score the final turn.

    The agent-under-test is pinned to temperature 0 for the duration so a
    rerun of the same fixture stops drifting on sampling noise (QNT-218).
    """
    started = time.perf_counter()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    trace_id: str | None = None
    set_temperature_override(EVAL_AGENT_TEMPERATURE)
    try:
        handler = make_callback_handler() if emit_langfuse_scores else None
        graph_config: RunnableConfig = {
            "configurable": {"thread_id": f"dialogue-eval:{fixture.id}:{uuid.uuid4().hex[:8]}"},
        }
        if handler is not None:
            graph_config.update(
                {
                    "callbacks": [handler],
                    "run_name": "dialogue-eval",
                    "metadata": {
                        "langfuse_session_id": f"dialogue-eval:{fixture.id}",
                        "eval_type": "dialogue",
                        **current_model_info(),
                    },
                }
            )

        # QNT-220 (#8): mirror production -- thesis/comparison use compact company.
        graph = build_graph(
            default_report_tools(),
            checkpointer=SqliteSaver(conn),
            compact_company_tool=get_company_report_compact,
        )
        state: dict[str, Any] = {}
        for turn in fixture.turns:
            if handler is not None:
                with propagate_attributes(trace_name="dialogue-eval"):
                    state = graph.invoke(
                        {"ticker": fixture.ticker, "question": turn},
                        config=graph_config,
                    )
            else:
                state = graph.invoke(
                    {"ticker": fixture.ticker, "question": turn}, config=graph_config
                )

        trace_id = getattr(handler, "last_trace_id", None) if handler is not None else None
        narrative = str(state.get("narrative") or "")
        structured_payload = _render_payload(state)
        numeric_support = check_hallucination(narrative, _flatten_reports(state))
        judge_score = judge_score_fn(
            fixture_id=fixture.id,
            transcript=_transcript_from_state(state, fixture.turns),
            narrative=narrative,
            structured_payload=structured_payload,
            expected_signals=fixture.expected_signals,
            numeric_support=_numeric_support_text(numeric_support),
            llm=llm_for_judge,
            config=graph_config if handler is not None else None,
        )
        judge_score = _apply_deterministic_numeric_gate(judge_score, numeric_support)
        judge_score = _apply_deterministic_filler_gate(judge_score, narrative)
        if emit_langfuse_scores:
            push_langfuse_scores(judge_score, trace_id)
    finally:
        set_temperature_override(None)
        conn.close()

    return DialogueOutcome(
        fixture=fixture,
        transcript=_transcript_from_state(state, fixture.turns),
        narrative=narrative,
        structured_payload=structured_payload,
        numeric_support=numeric_support,
        judge_score=judge_score,
        trace_id=trace_id,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


def push_langfuse_scores(score: DialogueJudgeScore | None, trace_id: str | None) -> None:
    """Attach dialogue scores to a Langfuse trace when tracing is enabled."""
    if langfuse is None or trace_id is None or score is None:
        return
    try:
        for axis in DIALOGUE_AXES:
            axis_score = getattr(score, axis)
            langfuse.create_score(
                trace_id=trace_id,
                name=f"dialogue_{axis}",
                value=float(axis_score.score),
                data_type="NUMERIC",
                comment=axis_score.rationale,
            )
    except Exception as exc:  # noqa: BLE001 -- eval observability must not crash the harness
        logger.warning("dialogue score push failed: %s", exc)


def append_dialogue_history(
    outcomes: list[DialogueOutcome],
    *,
    run_id: str | None = None,
    history_path: Path = HISTORY_PATH,
) -> str:
    """Append dialogue eval rows to the shared history.csv schema."""
    rid = (
        run_id
        or f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:6]}-dialogue"
    )
    sha = _git_sha()
    pv = _prompt_version()
    new_file = not history_path.exists()
    history_path.parent.mkdir(parents=True, exist_ok=True)

    with history_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
        if new_file:
            writer.writeheader()
        for outcome in outcomes:
            js = outcome.judge_score
            row: dict[str, Any] = {field: "" for field in HISTORY_FIELDS}
            row.update(
                {
                    "run_id": rid,
                    "git_sha": sha,
                    "prompt_version": pv,
                    "ticker": outcome.fixture.ticker,
                    "question_id": outcome.fixture.id,
                    "question": " | ".join(outcome.fixture.turns),
                    "hallucination_ok": "1" if outcome.numeric_support.ok else "0",
                    "elapsed_ms": outcome.elapsed_ms,
                    "eval_type": "dialogue",
                    "dialogue_fixture_id": outcome.fixture.id,
                    "dialogue_turns": len(outcome.fixture.turns),
                    "dialogue_composite": "" if js is None else js.composite,
                    "judge_model": f"{JUDGE_MODEL_ALIAS} ({JUDGE_RESOLVED_MODEL})",
                    "agent_model": AGENT_UNDER_TEST_RESOLVED_MODEL,
                }
            )
            if js is not None:
                for axis in DIALOGUE_AXES:
                    axis_score = getattr(js, axis)
                    row[axis] = axis_score.score
                    row[f"{axis}_rationale"] = axis_score.rationale
            writer.writerow(cast(Any, row))

        # QNT-218: one aggregate row per run carrying the per-axis mean (axis
        # columns) + standard error (`*_se` columns) so a single sweep ships
        # with its own dispersion band.
        agg = aggregate(outcomes)
        if agg is not None:
            summary: dict[str, Any] = {field: "" for field in HISTORY_FIELDS}
            summary.update(
                {
                    "run_id": rid,
                    "git_sha": sha,
                    "prompt_version": pv,
                    "question_id": "ALL",
                    "eval_type": "dialogue_summary",
                    "dialogue_composite": round(agg.composite_mean, 4),
                    "dialogue_composite_se": round(agg.composite_se, 4),
                    "dialogue_n": agg.n,
                    "judge_model": f"{JUDGE_MODEL_ALIAS} ({JUDGE_RESOLVED_MODEL})",
                    "agent_model": AGENT_UNDER_TEST_RESOLVED_MODEL,
                }
            )
            for axis in DIALOGUE_AXES:
                summary[axis] = round(agg.axis_mean[axis], 4)
                summary[f"{axis}_se"] = round(agg.axis_se[axis], 4)
            writer.writerow(cast(Any, summary))
    return rid


def precheck_environment(*, timeout: float = 5.0) -> None:
    """Fail fast if the LiteLLM proxy or report API is unreachable (QNT-218).

    The QNT-214 follow-up was contaminated once by running with the FastAPI
    report server down: every tool returned a connection error and the agent
    answered on empty reports, which the judge happily scored. A reachable
    HTTP response (any status -- even 404 proves the server is up) clears the
    check; a connection error fails it before a single token is spent.
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
            "dialogue eval precheck failed -- start the dev stack first "
            "(make dev-litellm / make dev-api / make tunnel):\n  " + "\n  ".join(unreachable)
        )


def contamination_warning(outcomes: list[DialogueOutcome]) -> str | None:
    """Flag a run whose median latency or judge failures suggest throttling.

    Returns a warning string when the aggregate should not be trusted, else
    None. Groq rate-limiting roughly doubles per-fixture latency and scatters
    judge failures; either signal means rerun on a clean window rather than
    publish the numbers (QNT-218).
    """
    if not outcomes:
        return None
    med = median(o.elapsed_ms for o in outcomes)
    failures = sum(1 for o in outcomes if o.judge_score is None)
    if med <= CONTAMINATION_LATENCY_MS and failures == 0:
        return None
    return (
        f"CONTAMINATED RUN -- do not trust this aggregate. median latency "
        f"{int(med)}ms (clean ~25-30s, contamination threshold "
        f"{CONTAMINATION_LATENCY_MS}ms); {failures} judge failure(s). "
        "Likely Groq throttling; rerun on a clean rate-limit window."
    )


def run_all(
    *,
    history_path: Path = HISTORY_PATH,
    only: str | None = None,
    llm_for_judge: Any | None = None,
    emit_langfuse_scores: bool = False,
    skip_precheck: bool = False,
) -> tuple[str, list[DialogueOutcome]]:
    if not skip_precheck:
        precheck_environment()
    records = load_dialogues()
    if only is not None:
        wanted = only
        records = [r for r in records if r.id == wanted]
        if not records:
            raise ValueError(f"no dialogue fixture with id {wanted!r}")
    outcomes = [
        run_fixture(r, llm_for_judge=llm_for_judge, emit_langfuse_scores=emit_langfuse_scores)
        for r in records
    ]
    rid = append_dialogue_history(outcomes, history_path=history_path)
    if emit_langfuse_scores:
        flush_langfuse()
    return rid, outcomes


def summarise(outcomes: list[DialogueOutcome]) -> str:
    total = len(outcomes)
    if not total:
        return "no dialogue fixtures evaluated"
    clean = sum(1 for o in outcomes if o.numeric_support.ok)
    agg = aggregate(outcomes)
    avg = round(agg.composite_mean, 3) if agg is not None else None
    lines: list[str] = []
    warning = contamination_warning(outcomes)
    if warning is not None:
        lines += [warning, ""]
    lines.append(f"dialogues: {total}  numeric_support_ok: {clean}/{total}  avg_dialogue: {avg}")
    if agg is not None:
        lines += ["", "per-axis mean +/- SE (1 run; descriptive scatter, not a lift test):"]
        for axis in DIALOGUE_AXES:
            lines.append(f"  {axis:20s} {agg.axis_mean[axis]:.3f} +/- {agg.axis_se[axis]:.3f}")
    lines += ["", "per-fixture:"]
    for outcome in outcomes:
        score_tag = "n/a" if outcome.judge_score is None else f"{outcome.judge_score.composite:.3f}"
        lines.append(
            f"  [{score_tag}] {outcome.fixture.id:32s} "
            f"{_numeric_support_text(outcome.numeric_support)} elapsed={outcome.elapsed_ms}ms"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.evals.dialogue_eval")
    parser.add_argument("--only", help="Run only one dialogue fixture id")
    parser.add_argument(
        "--history-path",
        type=Path,
        default=HISTORY_PATH,
        help=f"Where to append history rows (default: {HISTORY_PATH})",
    )
    parser.add_argument(
        "--emit-langfuse-scores",
        action="store_true",
        help="Attach dialogue_* scores to Langfuse traces when keys are configured.",
    )
    parser.add_argument(
        "--skip-precheck",
        action="store_true",
        help="Skip the LiteLLM/report-API reachability precheck (offline/testing only).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        run_id, outcomes = run_all(
            history_path=args.history_path,
            only=args.only,
            emit_langfuse_scores=args.emit_langfuse_scores,
            skip_precheck=args.skip_precheck,
        )
    except Exception:
        logger.exception("dialogue eval run failed")
        return 1

    print(f"run_id: {run_id}")
    print(summarise(outcomes))
    if not outcomes or any(o.judge_score is None for o in outcomes):
        return 1
    return 0


__all__ = [
    "DIALOGUE_AXES",
    "DIALOGUE_GOLDENS_PATH",
    "AxisGateResult",
    "DialogueAggregate",
    "DialogueFixture",
    "DialogueOutcome",
    "aggregate",
    "append_dialogue_history",
    "contamination_warning",
    "format_gate",
    "gate_passed",
    "load_dialogue_scores",
    "load_dialogues",
    "paired_delta_gate",
    "precheck_environment",
    "push_langfuse_scores",
    "run_all",
    "run_fixture",
    "summarise",
]


if __name__ == "__main__":
    sys.exit(main())
