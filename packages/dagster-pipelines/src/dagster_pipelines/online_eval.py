"""Weekly online eval loop — sample prod traces and push judge scores (QNT-192).

Runs every Sunday at 04:00 ET. Pulls the previous 7 days of Langfuse traces
(name="agent-chat", read via the v2 observations API -- QNT-360), samples
ONLINE_EVAL_SAMPLE_RATE of the thesis-intent ones (default 100%), and pushes 2
per-axis judge scores (structure, analyst_logic) back via
langfuse.create_score().

Why only thesis traces, and only 2 axes?
    The judge rubric scores ``faithfulness`` and ``correctness`` against a
    REFERENCE thesis (QNT-230 #9), and prod traces have no golden reference
    stored alongside them -- with an empty reference those two axes are
    arbitrary (the first prod run scored a well-grounded fundamental answer
    faithfulness 0), so they are not pushed. ``structure`` scores the four
    thesis aspect blocks, so it only means something on thesis answers.
    ``structure`` and ``analyst_logic`` on thesis traces hold without a
    reference (QNT-360 follow-up).

Online vs offline comparability:
    Both loops call the same ``agent.evals.judge.score()`` at temperature=0
    and use the same axis names in Langfuse. Offline golden-set results
    live in ``history.csv``; online results live as Langfuse scores so
    dashboard trend lines require no CSV exports.

Keys:
    The schedule uses ONLINE_EVAL_LANGFUSE_PUBLIC_KEY / SECRET_KEY (not the
    agent's LANGFUSE_PUBLIC_KEY / SECRET_KEY). This keeps them isolated from
    ``evals/__main__.py``'s key-stripping pattern, which would otherwise
    silently disable the online client if the two code paths ever ran in the
    same process.

See docs/guides/ops-runbook.md for how to interpret a score drop.
"""

from __future__ import annotations

import json
import logging
import math
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from dagster import DefaultScheduleStatus, RunRequest, ScheduleEvaluationContext, job, op, schedule
from shared.config import settings

logger = logging.getLogger(__name__)


def _build_langfuse_client():
    """Build a Langfuse client from ONLINE_EVAL_* keys, or None if unconfigured."""
    from langfuse import Langfuse

    pk = settings.ONLINE_EVAL_LANGFUSE_PUBLIC_KEY
    sk = settings.ONLINE_EVAL_LANGFUSE_SECRET_KEY
    if not (pk and sk):
        logger.info("ONLINE_EVAL_LANGFUSE keys not set; online eval disabled.")
        return None
    return Langfuse(
        public_key=pk,
        secret_key=sk,
        base_url=settings.LANGFUSE_BASE_URL,
    )


def _fetch_agent_chat_runs(client: Any, from_ts: datetime, to_ts: datetime) -> list[Any]:
    """Fetch the ``langgraph-run`` observation of every ``agent-chat`` trace in the window.

    QNT-360: the v1 ``trace.list`` endpoint is removed after 2026-11-16, so read the
    v2 observations API instead. Filter on the ``langgraph-run`` span rather than the
    root: the root span is named ``agent-chat`` on the API path but not on every code
    path, while the LangGraph CallbackHandler's ``langgraph-run`` span carries the
    graph's input/output state on every trace -- the same payload v1 exposed as trace
    input/output.
    """
    flt = json.dumps(
        [
            {"type": "string", "column": "traceName", "operator": "=", "value": "agent-chat"},
            {"type": "string", "column": "name", "operator": "=", "value": "langgraph-run"},
            {
                "type": "datetime",
                "column": "startTime",
                "operator": ">=",
                "value": from_ts.isoformat(),
            },
            {
                "type": "datetime",
                "column": "startTime",
                "operator": "<",
                "value": to_ts.isoformat(),
            },
        ]
    )
    runs: list[Any] = []
    cursor: str | None = None
    while True:
        resp = client.api.observations.get_many(
            fields="core,basic,io", filter=flt, limit=1000, cursor=cursor
        )
        runs.extend(resp.data)
        cursor = resp.meta.cursor
        if not cursor:
            return runs


def _parse_io(raw: Any) -> Any:
    """Decode a v2 observation input/output, which the API returns as a raw JSON string."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _intent(run: Any) -> str | None:
    """Return the classified intent recorded in a ``langgraph-run`` output state."""
    output = _parse_io(run.output)
    return output.get("intent") if isinstance(output, dict) else None


def _extract_question(trace_input: Any) -> str:
    """Extract the user question from a Langfuse trace input.

    The LangGraph CallbackHandler records the graph's initial state dict as
    the trace input: ``{"ticker": "NVDA", "question": "..."}``.
    """
    if isinstance(trace_input, dict):
        q = trace_input.get("question") or trace_input.get("input", "")
        return str(q).strip()
    if isinstance(trace_input, str):
        return trace_input.strip()
    return ""


def _extract_generated(trace_output: Any) -> str:
    """Render the generated answer from a Langfuse trace output to markdown.

    The LangGraph CallbackHandler records the final graph state dict as the
    trace output. QNT-307: the answer lives in the single ``answer`` key (a
    serialized payload dict with no discriminator field), so we render the answer
    class whose EXACT field set matches the dict. The shapes overlap loosely -- a
    QuickFactAnswer dict ``{answer, cited_value, source}`` validates as
    ConversationalAnswer, which only requires ``answer`` and ignores extras -- so a
    plain try-each-class-in-order would mis-render. Every answer shape has a
    distinct field set, so ``keys == model_fields`` disambiguates. Falls back to a
    plain string otherwise.

    Trace back-compat: a PRE-QNT-307 trace carries the legacy per-shape slot keys
    (``thesis`` / ``quick_fact`` / ...) and no ``answer`` key, so it hits the
    string fallback. Online eval samples only the last 7 days, so old-shape traces
    age out of the window within a week of deploy -- the degradation is transient.
    """
    if not trace_output:
        return ""
    answer = trace_output.get("answer") if isinstance(trace_output, dict) else None
    if answer is not None:
        try:
            from agent.comparison import ComparisonAnswer
            from agent.conversational import ConversationalAnswer
            from agent.focused import FocusedAnalysis
            from agent.quick_fact import QuickFactAnswer
            from agent.thesis import Thesis

            def _try_render(cls: type) -> str | None:
                if isinstance(answer, cls):
                    return answer.to_markdown()
                # Exact field-set match: render as ``cls`` only when the dict's keys
                # are exactly ``cls``'s fields -- the shapes' loose overlap (extras
                # ignored on validate) would otherwise cross-match a wrong class.
                fields = getattr(cls, "model_fields", None)
                if isinstance(answer, dict) and fields is not None and set(answer) == set(fields):
                    try:
                        return cls(**answer).to_markdown()
                    except Exception:
                        return None
                return None

            for cls in (
                ComparisonAnswer,
                ConversationalAnswer,
                QuickFactAnswer,
                FocusedAnalysis,
                Thesis,
            ):
                rendered = _try_render(cls)
                if rendered:
                    return rendered
        except ImportError:
            pass

    if isinstance(trace_output, str):
        return trace_output.strip()
    return str(trace_output).strip()


@op
def run_online_eval(context) -> None:
    """Sample recent prod traces and push per-axis judge scores to Langfuse."""
    from agent.evals.judge import score as judge_score

    client = _build_langfuse_client()
    if client is None:
        context.log.info("Online eval skipped: ONLINE_EVAL_LANGFUSE keys not configured.")
        return

    sample_rate = settings.ONLINE_EVAL_SAMPLE_RATE
    now = datetime.now(UTC)
    from_ts = now - timedelta(days=7)

    context.log.info(
        "Fetching traces %s → %s (sample_rate=%.0f%%)",
        from_ts.date().isoformat(),
        now.date().isoformat(),
        sample_rate * 100,
    )

    try:
        runs = _fetch_agent_chat_runs(client, from_ts, now)
    except Exception:
        # QNT-360: re-raise so the run goes red -- returning here made every weekly
        # run report SUCCESS while scoring nothing (v1 rejected limit=500 for months).
        context.log.exception("Failed to fetch traces from Langfuse")
        raise

    # QNT-360 follow-up: the judge rubric is thesis-shaped (structure = the four
    # aspect blocks), so only thesis-intent traces are scored.
    traces = [r for r in runs if _intent(r) == "thesis"]
    sampled = [t for t in traces if random.random() < sample_rate]
    context.log.info(
        "Total traces: %d  Thesis: %d  Sampled: %d", len(runs), len(traces), len(sampled)
    )

    if len(traces) < 20:
        context.log.warning(
            "Only %d thesis traces in the last 7 days -- the trend signal is thin.",
            len(traces),
        )
    elif len(sampled) < 20:
        needed = min(1.0, math.ceil(20 / len(traces) * 100) / 100)
        context.log.warning(
            "Only %d traces sampled this week (< 20, from %d thesis traces). "
            "Set ONLINE_EVAL_SAMPLE_RATE=%.2f to produce >=20 samples.",
            len(sampled),
            len(traces),
            needed,
        )

    scored = 0
    skipped = 0
    for run in sampled:
        trace_id = run.trace_id
        question = _extract_question(_parse_io(run.input))
        generated = _extract_generated(_parse_io(run.output))
        if not generated:
            context.log.warning("Skipping trace %s: no generated text extracted", trace_id)
            skipped += 1
            continue

        js = judge_score(question=question, generated=generated, reference="")
        if js is None:
            context.log.warning("Judge returned None for trace %s", trace_id)
            skipped += 1
            continue

        try:
            # faithfulness / correctness are scored against a REFERENCE thesis
            # (QNT-230 #9) and prod traces carry none, so they are not pushed.
            for axis, value in [
                ("structure", js.structure),
                ("analyst_logic", js.analyst_logic),
            ]:
                client.create_score(
                    trace_id=trace_id,
                    name=axis,
                    value=float(value),
                    data_type="NUMERIC",
                )
            scored += 1
        except Exception:
            context.log.exception("Score push failed for trace %s", trace_id)
            skipped += 1

    context.log.info("Online eval complete: scored=%d skipped=%d", scored, skipped)
    client.flush()


@job
def online_eval_job():
    run_online_eval()


@schedule(
    job=online_eval_job,
    cron_schedule="0 4 * * 0",  # 04:00 ET, Sunday
    execution_timezone="America/New_York",
    default_status=DefaultScheduleStatus.RUNNING,
)
def online_eval_weekly_schedule(context: ScheduleEvaluationContext):
    """Weekly online eval sweep — sample prod traces, push judge scores.

    Single global run per week (no partition key). Run key is the ISO
    scheduled timestamp so Dagster deduplicates re-evaluations of the same
    tick.
    """
    ts = context.scheduled_execution_time.isoformat() if context.scheduled_execution_time else ""
    yield RunRequest(run_key=f"online_eval_{ts}")
