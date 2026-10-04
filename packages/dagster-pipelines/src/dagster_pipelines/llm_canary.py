"""Hourly synthetic LLM-route canary (QNT-493).

QNT-492 served every conversational turn from the slow free fallback for 7 weeks
because one request param got the primary filtered out. Prod traffic is about
one turn a day, so the per-turn fallback tripwire only fired after a real user
had already paid the slow path. This job sends one tiny request per production
call shape through the LiteLLM proxy every hour and fails the run (-> the
existing ``dagster_run_failure_alert_sensor`` -> Discord) when the primary did
not serve it within the latency bound.

Shapes probed, each built with the agent's own ``get_llm()`` request payload so
any param the real call carries (the QNT-492 failure class) is exercised too:

* structured -- ``json_mode`` (``response_format: json_object``), the shape every
  synthesize / conversational / clarify call uses (ADR-029). Asserts
  ``x-litellm-attempted-fallbacks == 0``; a missing header is a failure, not a pass.
* stream -- the free-text streamed narrate shape. LiteLLM omits
  ``x-litellm-attempted-fallbacks`` on streamed responses, so this asserts the
  stream was served by the same deployment (``x-litellm-model-id``) as the
  zero-fallback structured probe.
* structured_stream -- ``json_mode`` streamed, the shape the synthesize card
  calls use since QNT-494 (partial cards over SSE). Checked like ``stream``
  (same deployment as the structured probe) plus a JSON-parse check. The
  per-turn QNT-492 tripwire cannot see a fallback on this shape (no
  attempted-fallbacks header on streams), so this probe is its route coverage.

Debugging a red run: docs/guides/ops-runbook.md ("LLM canary failed").
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import openai
from dagster import (
    DefaultScheduleStatus,
    Failure,
    RunRequest,
    ScheduleEvaluationContext,
    job,
    op,
    schedule,
)

# A tiny probe on the primary answers in ~1-2s (QNT-493 receipts); a real
# synthesize call is ~5-20s. 15s is well clear of a healthy probe and still far
# below the 45s per-hop timeout, so it flags a degraded route, not jitter.
LATENCY_BOUND_S = 15.0
_MAX_TOKENS = 32

_STRUCTURED_MESSAGES = [
    ("system", 'Reply with a JSON object of the form {"reply": "<one word>"}.'),
    ("user", "Say pong."),
]
_STREAM_MESSAGES = [("user", "Say pong in one word.")]

# ``openai`` SDK ``chat.completions.with_raw_response.create`` -- returns a raw
# response exposing ``.headers`` and ``.parse()``.
CreateFn = Callable[..., Any]


@dataclass(frozen=True)
class ProbeResult:
    shape: str
    status_code: int
    latency_s: float
    attempted_fallbacks: str | None
    model_id: str | None
    content: str


def _build_requests() -> tuple[CreateFn, dict[str, dict[str, Any]]]:
    """Return the agent's own OpenAI client + the request bodies it sends per shape.

    Sending through ``llm.client`` (the SDK LangChain itself calls) rather than raw
    HTTP keeps the wire format identical -- e.g. the SDK flattens ``extra_body``
    into the top level; posting it literally makes the proxy replace the alias's
    own ``extra_body`` (reasoning-off + provider pin), which a raw-HTTP probe
    measured as reasoning silently turning back on.
    """
    from agent.llm import get_llm
    from langchain_core.messages import convert_to_messages

    llm = get_llm(max_tokens=_MAX_TOKENS)
    binding = llm.with_structured_output(None, method="json_mode").first  # pyright: ignore[reportAttributeAccessIssue]
    structured = llm._get_request_payload(  # noqa: SLF001
        convert_to_messages(_STRUCTURED_MESSAGES), **binding.kwargs
    )
    # LangChain strips this tracing-only kwarg before the request leaves.
    structured.pop("ls_structured_output_format", None)
    stream_kwargs: dict[str, Any] = {"stream": True, "stream_options": {"include_usage": True}}
    stream = llm._get_request_payload(  # noqa: SLF001
        convert_to_messages(_STREAM_MESSAGES), **stream_kwargs
    )
    return llm.client.with_raw_response.create, {
        "structured": structured,
        "stream": stream,
        "structured_stream": {**structured, **stream_kwargs},
    }


def _probe(
    create: CreateFn, shape: str, payload: dict[str, Any], clock: Callable[[], float]
) -> ProbeResult:
    start = clock()
    try:
        raw = create(**payload)
        parsed = raw.parse()
        if payload.get("stream"):
            content = "".join(c.choices[0].delta.content or "" for c in parsed if c.choices)
        else:
            content = parsed.choices[0].message.content or ""
    except openai.APIError as exc:
        return ProbeResult(
            shape=shape,
            status_code=getattr(exc, "status_code", 0),
            latency_s=clock() - start,
            attempted_fallbacks=None,
            model_id=None,
            content=f"{type(exc).__name__}: {exc}"[:200],
        )
    return ProbeResult(
        shape=shape,
        status_code=200,
        latency_s=clock() - start,
        attempted_fallbacks=raw.headers.get("x-litellm-attempted-fallbacks"),
        model_id=raw.headers.get("x-litellm-model-id"),
        content=content,
    )


def check_llm_route(
    create: CreateFn,
    payloads: dict[str, dict[str, Any]],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[ProbeResult], list[str]]:
    """Probe each shape; return the results and a list of human-readable failures."""
    results = [_probe(create, shape, payload, clock) for shape, payload in payloads.items()]
    by_shape = {r.shape: r for r in results}
    failures: list[str] = []

    for r in results:
        if r.status_code != 200:
            failures.append(f"{r.shape}: request failed ({r.status_code}): {r.content}")
            continue
        if r.latency_s > LATENCY_BOUND_S:
            failures.append(f"{r.shape}: {r.latency_s:.1f}s > {LATENCY_BOUND_S:.0f}s bound")
        if not r.content.strip():
            failures.append(f"{r.shape}: empty completion")

    structured = by_shape["structured"]
    if structured.status_code == 200 and structured.attempted_fallbacks != "0":
        failures.append(
            f"structured: x-litellm-attempted-fallbacks={structured.attempted_fallbacks}"
            " (expected 0) -- the primary did not serve it"
        )

    for shape, payload in payloads.items():
        r = by_shape[shape]
        if r.status_code != 200:
            continue
        if payload.get("response_format") == {"type": "json_object"}:
            try:
                json.loads(r.content)
            except ValueError:
                failures.append(f"{shape}: json_mode reply is not JSON: {r.content!r}")
        # Streams carry no attempted-fallbacks header; a fallback shows as a
        # different deployment id than the primary that served the zero-fallback
        # structured probe. Only comparable when the structured probe itself
        # succeeded, and a missing id must fail -- otherwise None == None would
        # pass a stream fallback silently.
        if payload.get("stream") and structured.status_code == 200:
            if not structured.model_id or not r.model_id:
                failures.append(
                    f"x-litellm-model-id missing -- cannot verify {shape} used the primary"
                )
            elif r.model_id != structured.model_id:
                failures.append(
                    f"{shape}: served by deployment {r.model_id}, not the primary"
                    f" {structured.model_id} -- a fallback fired"
                )
    return results, failures


@op
def run_llm_canary(context) -> None:
    create, payloads = _build_requests()
    results, failures = check_llm_route(create, payloads)
    for r in results:
        context.log.info(
            "llm canary %s: status=%s latency=%.2fs fallbacks=%s model_id=%s",
            r.shape,
            r.status_code,
            r.latency_s,
            r.attempted_fallbacks,
            r.model_id,
        )
    if failures:
        raise Failure(description="LLM canary: " + "; ".join(failures))


@job
def llm_canary_job():
    run_llm_canary()


@schedule(
    job=llm_canary_job,
    cron_schedule="17 * * * *",  # hourly, off the top of the hour
    execution_timezone="America/New_York",
    default_status=DefaultScheduleStatus.RUNNING,
)
def llm_canary_hourly_schedule(context: ScheduleEvaluationContext):
    """Hourly LLM-route canary; run key dedups re-evaluations of the same tick."""
    ts = context.scheduled_execution_time.isoformat() if context.scheduled_execution_time else ""
    yield RunRequest(run_key=f"llm_canary_{ts}")
