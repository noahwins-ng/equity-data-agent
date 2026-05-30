"""Dialogue-quality eval harness for multi-turn agent runs (QNT-214)."""

from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from shared.tickers import TICKERS

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
from agent.llm import current_model_info
from agent.tools import default_report_tools
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
    for key in ("comparison", "conversational", "quick_fact", "focused", "thesis"):
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


def run_fixture(
    fixture: DialogueFixture,
    *,
    llm_for_judge: Any | None = None,
    emit_langfuse_scores: bool = False,
) -> DialogueOutcome:
    """Replay one multi-turn fixture through the agent and score the final turn."""
    started = time.perf_counter()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    trace_id: str | None = None
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

        graph = build_graph(default_report_tools(), checkpointer=SqliteSaver(conn))
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
        if emit_langfuse_scores:
            push_langfuse_scores(judge_score, trace_id)
    finally:
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
    return rid


def run_all(
    *,
    history_path: Path = HISTORY_PATH,
    only: str | None = None,
    llm_for_judge: Any | None = None,
    emit_langfuse_scores: bool = False,
) -> tuple[str, list[DialogueOutcome]]:
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
    judged = [o.judge_score for o in outcomes if o.judge_score is not None]
    if not total:
        return "no dialogue fixtures evaluated"
    clean = sum(1 for o in outcomes if o.numeric_support.ok)
    avg = round(sum(j.composite for j in judged) / len(judged), 3) if judged else None
    lines = [
        f"dialogues: {total}  numeric_support_ok: {clean}/{total}  avg_dialogue: {avg}",
        "",
        "per-fixture:",
    ]
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
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        run_id, outcomes = run_all(
            history_path=args.history_path,
            only=args.only,
            emit_langfuse_scores=args.emit_langfuse_scores,
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
    "DialogueFixture",
    "DialogueOutcome",
    "append_dialogue_history",
    "load_dialogues",
    "push_langfuse_scores",
    "run_all",
    "run_fixture",
    "summarise",
]


if __name__ == "__main__":
    sys.exit(main())
