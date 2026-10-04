"""QNT-493 (AC3): the hourly LLM-route canary fails on any fallback or slow shape."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from dagster import Failure, build_op_context
from dagster_pipelines import llm_canary
from dagster_pipelines.llm_canary import LATENCY_BOUND_S, check_llm_route

PRIMARY_ID = "primary-deployment"
FALLBACK_ID = "fallback-deployment"
PAYLOADS: dict[str, dict[str, Any]] = {
    "structured": {"model": "equity-agent/default", "response_format": {"type": "json_object"}},
    "stream": {"model": "equity-agent/default", "stream": True},
}


def _raw(headers: dict[str, str], parsed: Any) -> SimpleNamespace:
    return SimpleNamespace(headers=headers, parse=lambda: parsed)


def _completion(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _chunks(text: str) -> Iterator[SimpleNamespace]:
    for piece in (text[:2], text[2:]):
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=piece))])
    yield SimpleNamespace(choices=[])  # trailing usage-only chunk


def _fake_create(
    *,
    structured_fallbacks: str | None = "0",
    structured_id: str = PRIMARY_ID,
    stream_id: str = PRIMARY_ID,
    structured_content: str = '{"reply": "pong"}',
    stream_text: str = "pong",
) -> Any:
    def create(**payload: Any) -> SimpleNamespace:
        if payload.get("stream"):
            return _raw({"x-litellm-model-id": stream_id}, _chunks(stream_text))
        headers = {"x-litellm-model-id": structured_id}
        if structured_fallbacks is not None:
            headers["x-litellm-attempted-fallbacks"] = structured_fallbacks
        return _raw(headers, _completion(structured_content))

    return create


def _clock(step: float) -> Any:
    ticks = iter(range(100))
    return lambda: next(ticks) * step


def test_healthy_route_passes() -> None:
    results, failures = check_llm_route(_fake_create(), PAYLOADS, clock=_clock(0.5))
    assert failures == []
    assert [r.shape for r in results] == ["structured", "stream"]
    assert results[1].content == "pong"


def test_structured_fallback_fails() -> None:
    _, failures = check_llm_route(
        _fake_create(structured_fallbacks="1", structured_id=FALLBACK_ID, stream_id=FALLBACK_ID),
        PAYLOADS,
        clock=_clock(0.5),
    )
    assert any("attempted-fallbacks=1" in f for f in failures)


def test_missing_fallback_header_fails() -> None:
    """No header = no evidence the primary served it -- never a silent pass."""
    _, failures = check_llm_route(
        _fake_create(structured_fallbacks=None), PAYLOADS, clock=_clock(0.5)
    )
    assert any("attempted-fallbacks=None" in f for f in failures)


def test_stream_served_by_other_deployment_fails() -> None:
    _, failures = check_llm_route(_fake_create(stream_id=FALLBACK_ID), PAYLOADS, clock=_clock(0.5))
    assert failures == [
        f"stream: served by deployment {FALLBACK_ID}, not the primary {PRIMARY_ID}"
        " -- a fallback fired"
    ]


def test_missing_model_id_fails_instead_of_passing_vacuously() -> None:
    """No deployment id on either probe would make the stream comparison
    None == None -- a stream fallback must not pass silently."""

    def create(**payload: Any) -> SimpleNamespace:
        if payload.get("stream"):
            return _raw({}, _chunks("pong"))
        return _raw({"x-litellm-attempted-fallbacks": "0"}, _completion('{"reply": "pong"}'))

    _, failures = check_llm_route(create, PAYLOADS, clock=_clock(0.5))
    assert any("x-litellm-model-id" in f for f in failures)


def test_failed_structured_probe_does_not_misreport_stream_fallback() -> None:
    request = httpx.Request("POST", "http://litellm:4000/chat/completions")

    def create(**payload: Any) -> Any:
        if payload.get("stream"):
            return _raw({"x-litellm-model-id": PRIMARY_ID}, _chunks("pong"))
        raise openai.NotFoundError(
            "No endpoints found", response=httpx.Response(404, request=request), body=None
        )

    _, failures = check_llm_route(create, PAYLOADS, clock=_clock(0.5))
    assert failures == ["structured: request failed (404): NotFoundError: No endpoints found"]


def test_slow_shape_fails() -> None:
    _, failures = check_llm_route(_fake_create(), PAYLOADS, clock=_clock(LATENCY_BOUND_S + 1))
    assert len([f for f in failures if "bound" in f]) == 2


def test_non_json_structured_reply_fails() -> None:
    _, failures = check_llm_route(
        _fake_create(structured_content="pong"), PAYLOADS, clock=_clock(0.5)
    )
    assert any("not JSON" in f for f in failures)


def test_empty_stream_fails() -> None:
    _, failures = check_llm_route(_fake_create(stream_text=""), PAYLOADS, clock=_clock(0.5))
    assert "stream: empty completion" in failures


def test_http_error_fails() -> None:
    request = httpx.Request("POST", "http://litellm:4000/chat/completions")

    def create(**_payload: Any) -> Any:
        raise openai.NotFoundError(
            "No endpoints found", response=httpx.Response(404, request=request), body=None
        )

    _, failures = check_llm_route(create, PAYLOADS, clock=_clock(0.5))
    assert len(failures) == 2
    assert all("request failed (404)" in f for f in failures)


def test_op_raises_failure_so_the_discord_sensor_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forced-failure case: a fallback makes the op raise, failing the run -- which
    is what dagster_run_failure_alert_sensor posts to Discord."""
    monkeypatch.setattr(
        llm_canary,
        "_build_requests",
        lambda: (_fake_create(structured_fallbacks="1"), PAYLOADS),
    )
    with pytest.raises(Failure, match="attempted-fallbacks=1"):
        llm_canary.run_llm_canary(build_op_context())


def test_op_passes_on_healthy_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_canary, "_build_requests", lambda: (_fake_create(), PAYLOADS))
    llm_canary.run_llm_canary(build_op_context())


def test_payloads_are_the_agents_json_mode_and_stream_shapes() -> None:
    """The canary must send the agent's real request shapes, not a hand-written
    approximation -- a param the agent adds (the QNT-492 class) must be probed."""
    _, payloads = llm_canary._build_requests()
    structured, stream = payloads["structured"], payloads["stream"]
    assert structured["model"] == stream["model"] == "equity-agent/default"
    assert structured["response_format"] == {"type": "json_object"}
    assert "ls_structured_output_format" not in structured
    for key in ("tools", "tool_choice", "parallel_tool_calls"):
        assert key not in structured
    assert stream["stream"] is True
    assert "response_format" not in stream
    # QNT-494: synthesize streams its json_mode call, so that shape is probed too.
    structured_stream = payloads["structured_stream"]
    assert structured_stream["stream"] is True
    assert structured_stream["response_format"] == {"type": "json_object"}
    stream_keys = ("stream", "stream_options")
    assert {k: v for k, v in structured_stream.items() if k not in stream_keys} == {
        k: v for k, v in structured.items() if k not in stream_keys
    }


# ─── QNT-494: streamed json_mode shape (synthesize) ─────────────────────────

STREAM_PAYLOADS: dict[str, dict[str, Any]] = {
    **PAYLOADS,
    "structured_stream": {
        "model": "equity-agent/default",
        "response_format": {"type": "json_object"},
        "stream": True,
    },
}


def _fake_create_streamed_json(stream_id: str = PRIMARY_ID, json_text: str = '{"r": 1}') -> Any:
    base = _fake_create()

    def create(**payload: Any) -> SimpleNamespace:
        if payload.get("stream") and payload.get("response_format"):
            return _raw({"x-litellm-model-id": stream_id}, _chunks(json_text))
        return base(**payload)

    return create


def test_structured_stream_healthy_passes() -> None:
    _, failures = check_llm_route(_fake_create_streamed_json(), STREAM_PAYLOADS, clock=_clock(0.5))
    assert failures == []


def test_structured_stream_served_by_other_deployment_fails() -> None:
    _, failures = check_llm_route(
        _fake_create_streamed_json(stream_id=FALLBACK_ID), STREAM_PAYLOADS, clock=_clock(0.5)
    )
    assert failures == [
        f"structured_stream: served by deployment {FALLBACK_ID}, not the primary {PRIMARY_ID}"
        " -- a fallback fired"
    ]


def test_structured_stream_non_json_fails() -> None:
    _, failures = check_llm_route(
        _fake_create_streamed_json(json_text="pong"), STREAM_PAYLOADS, clock=_clock(0.5)
    )
    assert any(f.startswith("structured_stream:") and "not JSON" in f for f in failures)
