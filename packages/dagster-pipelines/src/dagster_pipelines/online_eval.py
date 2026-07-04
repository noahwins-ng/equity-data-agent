"""Weekly online eval loop — sample prod traces and push judge scores (QNT-192).

Runs every Sunday at 04:00 ET. Pulls the previous 7 days of Langfuse traces
(name="agent-chat"), samples ONLINE_EVAL_SAMPLE_RATE of them (default 5%),
and pushes 4 per-axis judge scores (faithfulness, structure, correctness,
analyst_logic) back via langfuse.create_score().

Why no reference thesis?
    Prod traces have no golden reference stored alongside them. The
    ``correctness`` axis therefore scores internal consistency of the
    generated text, not fidelity to a vetted reference — treat it as a soft
    signal. ``faithfulness``, ``structure``, and ``analyst_logic`` remain
    fully meaningful and are the primary trend signals.

Online vs offline comparability:
    Both loops call the same ``agent.evals.judge.score()`` at temperature=0
    and push the same four axis names to Langfuse. Offline golden-set results
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
        traces: list[Any] = []
        page = 1
        while True:
            resp = client.api.trace.list(
                name="agent-chat",
                from_timestamp=from_ts,
                to_timestamp=now,
                limit=500,
                page=page,
            )
            traces.extend(resp.data)
            if page >= resp.meta.total_pages:
                break
            page += 1
    except Exception:
        context.log.exception("Failed to fetch traces from Langfuse")
        return

    sampled = [t for t in traces if random.random() < sample_rate]
    context.log.info("Total traces: %d  Sampled: %d", len(traces), len(sampled))

    if len(traces) < 20:
        context.log.warning(
            "Only %d traces in the last 7 days. "
            "Set ONLINE_EVAL_SAMPLE_RATE=1.0 to score every trace.",
            len(traces),
        )
    elif len(sampled) < 20:
        needed = min(1.0, math.ceil(20 / len(traces) * 100) / 100)
        context.log.warning(
            "Only %d traces sampled this week (< 20, from %d total). "
            "Set ONLINE_EVAL_SAMPLE_RATE=%.2f to produce >=20 samples.",
            len(sampled),
            len(traces),
            needed,
        )

    scored = 0
    skipped = 0
    for trace in sampled:
        question = _extract_question(trace.input)
        generated = _extract_generated(trace.output)
        if not generated:
            context.log.warning("Skipping trace %s: no generated text extracted", trace.id)
            skipped += 1
            continue

        js = judge_score(question=question, generated=generated, reference="")
        if js is None:
            context.log.warning("Judge returned None for trace %s", trace.id)
            skipped += 1
            continue

        try:
            for axis, value in [
                ("faithfulness", js.faithfulness),
                ("structure", js.structure),
                ("correctness", js.correctness),
                ("analyst_logic", js.analyst_logic),
            ]:
                client.create_score(
                    trace_id=trace.id,
                    name=axis,
                    value=float(value),
                    data_type="NUMERIC",
                )
            scored += 1
        except Exception:
            context.log.exception("Score push failed for trace %s", trace.id)
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
