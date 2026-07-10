from uuid import uuid4

import pytest
from agent.llm import (
    _ALIAS_BY_PROVIDER,
    JUDGE_ALIAS,
    SMALL_NODE_ALIAS,
    ServedModelInfo,
    ServedModelTracker,
    _fallback_info_from_response,
    _ServedModelCallback,
    get_judge_llm,
    get_llm,
    resolve_trace_model_tag,
    set_model_override,
    set_temperature_override,
)


@pytest.fixture(autouse=True)
def _reset_override():
    """Ensure no test leaks the QNT-129 model override into the next test.

    Resets both before AND after each test so the first test in this module
    is also protected if a future session-scoped fixture sets the override
    at module-import time.
    """
    set_model_override(None)
    set_temperature_override(None)
    yield
    set_model_override(None)
    set_temperature_override(None)


def test_alias_map_covers_both_providers():
    assert _ALIAS_BY_PROVIDER == {
        "groq": "equity-agent/default",
        "gemini": "equity-agent/gemini",
    }


def test_default_provider_routes_to_groq_alias(monkeypatch):
    monkeypatch.delenv("EQUITY_AGENT_PROVIDER", raising=False)
    from shared import config as cfg

    monkeypatch.setattr(cfg.settings, "EQUITY_AGENT_PROVIDER", "groq")
    llm = get_llm()
    assert llm.model_name == "equity-agent/default"


def test_gemini_override_routes_to_gemini_alias(monkeypatch):
    from shared import config as cfg

    monkeypatch.setattr(cfg.settings, "EQUITY_AGENT_PROVIDER", "gemini")
    llm = get_llm()
    assert llm.model_name == "equity-agent/gemini"


def test_unknown_provider_raises(monkeypatch):
    from shared import config as cfg

    monkeypatch.setattr(cfg.settings, "EQUITY_AGENT_PROVIDER", "nope")
    with pytest.raises(ValueError, match="Unknown EQUITY_AGENT_PROVIDER"):
        get_llm()


def test_provider_case_insensitive(monkeypatch):
    from shared import config as cfg

    monkeypatch.setattr(cfg.settings, "EQUITY_AGENT_PROVIDER", "GEMINI")
    llm = get_llm()
    assert llm.model_name == "equity-agent/gemini"


def test_model_override_bypasses_provider_lookup(monkeypatch):
    """QNT-129: --model flag short-circuits the provider env var entirely."""
    from shared import config as cfg

    monkeypatch.setattr(cfg.settings, "EQUITY_AGENT_PROVIDER", "groq")
    set_model_override("equity-agent/bench-gptoss120b")
    llm = get_llm()
    assert llm.model_name == "equity-agent/bench-gptoss120b"


def test_model_override_unset_falls_back_to_provider(monkeypatch):
    from shared import config as cfg

    monkeypatch.setattr(cfg.settings, "EQUITY_AGENT_PROVIDER", "gemini")
    set_model_override("equity-agent/bench-anything")
    set_model_override(None)
    llm = get_llm()
    assert llm.model_name == "equity-agent/gemini"


def test_temperature_override_wins_over_explicit_arg(monkeypatch):
    """QNT-218: the eval determinism override beats even an explicit temperature."""
    from shared import config as cfg

    monkeypatch.setattr(cfg.settings, "EQUITY_AGENT_PROVIDER", "groq")
    set_temperature_override(0.0)
    # narrate streams at 0.3; the override must still pin it to 0.0.
    assert get_llm(temperature=0.3).temperature == 0.0


def test_temperature_override_unset_uses_call_arg(monkeypatch):
    from shared import config as cfg

    monkeypatch.setattr(cfg.settings, "EQUITY_AGENT_PROVIDER", "groq")
    set_temperature_override(None)
    assert get_llm(temperature=0.3).temperature == 0.3
    assert get_llm().temperature == 0.2


def test_max_tokens_override_routes_through_extra_body(monkeypatch):
    """QNT-358: the per-call output-budget override MUST travel as the literal
    ``max_tokens`` key in ``extra_body`` -- the ChatOpenAI ``max_tokens=`` field
    serialises as ``max_completion_tokens``, a different key from the config's
    ``max_tokens: 1500``, so it would NOT override the cap and the comparison
    payload would silently truncate (the QNT-351 fail-close this ticket fixes).
    This is the code-level tripwire for that regression."""
    from shared import config as cfg

    monkeypatch.setattr(cfg.settings, "EQUITY_AGENT_PROVIDER", "groq")
    # With an override: the literal max_tokens key lands in extra_body, and the
    # field-level max_tokens (-> max_completion_tokens) stays unset.
    llm = get_llm(max_tokens=3000)
    assert llm.extra_body == {"max_tokens": 3000}
    assert llm.max_tokens is None
    # Without an override every other caller is untouched (no extra_body).
    assert get_llm().extra_body is None


# ─── QNT-220 (#7) per-node model tiering ────────────────────────────────────


def test_small_node_alias_is_a_known_litellm_alias():
    """The tiering alias must be resolvable by litellm_config.yaml. QNT-220 uses
    a Groq small model (gpt-oss-20b) -- gemini-2.5-flash free tier caps at 20
    requests/DAY, non-viable for a node that runs on 100% of turns."""
    assert SMALL_NODE_ALIAS == "equity-agent/small"


def test_model_alias_routes_to_small_alias(monkeypatch):
    """QNT-220: a per-node ``model_alias`` overrides the provider default so
    classify/plan/exploration can run on the small model while the provider
    env still points the rest of the graph at the 70b default."""
    from shared import config as cfg

    monkeypatch.setattr(cfg.settings, "EQUITY_AGENT_PROVIDER", "groq")
    assert get_llm(model_alias=SMALL_NODE_ALIAS).model_name == "equity-agent/small"
    # No alias -> provider default (synthesize/narrate path).
    assert get_llm().model_name == "equity-agent/default"


def test_eval_model_override_wins_over_per_node_alias(monkeypatch):
    """Precedence: the eval ``--model`` sweep must beat per-node tiering so one
    flag still benchmarks every node on the same model (QNT-129 contract)."""
    from shared import config as cfg

    monkeypatch.setattr(cfg.settings, "EQUITY_AGENT_PROVIDER", "groq")
    set_model_override("equity-agent/bench-gptoss120b")
    assert get_llm(model_alias=SMALL_NODE_ALIAS).model_name == "equity-agent/bench-gptoss120b"


def test_stream_usage_enabled(monkeypatch):
    """QNT-219: streamed runs (narrate) must request token usage in the final
    chunk, else Langfuse records 0 tokens for every streamed generation."""
    from shared import config as cfg

    monkeypatch.setattr(cfg.settings, "EQUITY_AGENT_PROVIDER", "groq")
    assert get_llm().stream_usage is True


# ─── QNT-230 #10: pinned judge alias ────────────────────────────────────────


def test_judge_alias_is_cerebras_gptoss120b():
    """The structured judge pins the same model the dialogue judge already uses."""
    assert JUDGE_ALIAS == "equity-agent/bench-cerebras-gptoss120b"


def test_get_judge_llm_resolves_pinned_alias():
    llm = get_judge_llm()
    assert llm.model_name == JUDGE_ALIAS
    assert llm.temperature == 0.0


def test_judge_llm_unaffected_by_model_override(monkeypatch):
    """The whole point of #10: a bench sweep re-routes the agent-under-test but
    NOT the judge, so a candidate model never scores its own output."""
    from shared import config as cfg

    monkeypatch.setattr(cfg.settings, "EQUITY_AGENT_PROVIDER", "groq")
    set_model_override("equity-agent/bench-gptoss20b")
    # The agent-under-test moves to the override...
    assert get_llm().model_name == "equity-agent/bench-gptoss20b"
    # ...but the judge stays pinned.
    assert get_judge_llm().model_name == JUDGE_ALIAS


def test_judge_llm_unaffected_by_temperature_override(monkeypatch):
    """The dialogue-eval determinism override must not perturb the judge either."""
    from shared import config as cfg

    monkeypatch.setattr(cfg.settings, "EQUITY_AGENT_PROVIDER", "groq")
    set_temperature_override(0.7)
    assert get_judge_llm().temperature == 0.0


# ─── QNT-230 #14: fallback-fire visibility via x-litellm headers ─────────────
#
# QNT-182 finding: LiteLLM echoes the requested ALIAS in response.model, so the
# body can't reveal a fallback. The authoritative signal is the response header
# x-litellm-attempted-fallbacks; x-litellm-model-api-base shows where it landed.


def test_resolve_trace_model_tag_no_fallback_keeps_static():
    """The common case: attempted-fallbacks==0, so keep the static resolution and
    existing Langfuse model: filters keep matching."""
    value, fired = resolve_trace_model_tag(
        alias="equity-agent/default",
        static_resolved="groq/llama-3.3-70b-versatile",
        served_info={"equity-agent/default": ServedModelInfo(fallback_fired=False)},
    )
    assert value == "groq/llama-3.3-70b-versatile"
    assert fired is False


def test_resolve_trace_model_tag_no_header_keeps_static():
    """No header captured (e.g. only the streamed narrate call ran) -> assume no
    fallback and keep the static resolution."""
    value, fired = resolve_trace_model_tag(
        alias="equity-agent/default",
        static_resolved="groq/llama-3.3-70b-versatile",
        served_info={},
    )
    assert value == "groq/llama-3.3-70b-versatile"
    assert fired is False


def test_resolve_trace_model_tag_fallback_surfaces_served_model():
    """A genuine fallback surfaces the model LiteLLM actually served (response.model
    carries the real name on a fallback) plus the fired marker."""
    value, fired = resolve_trace_model_tag(
        alias="equity-agent/default",
        static_resolved="groq/llama-3.3-70b-versatile",
        served_info={
            "equity-agent/default": ServedModelInfo(
                fallback_fired=True, served_model="meta-llama/llama-4-scout-17b-16e-instruct"
            )
        },
    )
    assert value == "meta-llama/llama-4-scout-17b-16e-instruct"
    assert fired is True


def test_resolve_trace_model_tag_fallback_without_name_marks_unverified():
    value, fired = resolve_trace_model_tag(
        alias="equity-agent/default",
        static_resolved="groq/llama-3.3-70b-versatile",
        served_info={"equity-agent/default": ServedModelInfo(fallback_fired=True)},
    )
    assert value == "unverified-fallback"
    assert fired is True


def test_resolve_trace_model_tag_unknown_static_marks_unverified():
    value, fired = resolve_trace_model_tag(
        alias="equity-agent/new-bench",
        static_resolved="unknown",
        served_info={},
    )
    assert value == "unverified-alias"
    assert fired is False


def test_fallback_info_from_response_no_fallback():
    class _Resp:
        llm_output = {
            "headers": {"x-litellm-attempted-fallbacks": "0"},
            "model_name": "equity-agent/default",
        }

    info = _fallback_info_from_response(_Resp())
    assert info is not None
    assert info.fallback_fired is False


def test_fallback_info_from_response_fallback_fired_captures_served_model():
    class _Resp:
        # On a fallback, response.model echoes the REAL served model, not the alias.
        llm_output = {
            "headers": {"x-litellm-attempted-fallbacks": "1"},
            "model_name": "meta-llama/llama-4-scout-17b-16e-instruct",
        }

    info = _fallback_info_from_response(_Resp())
    assert info is not None
    assert info.fallback_fired is True
    assert info.served_model == "meta-llama/llama-4-scout-17b-16e-instruct"


def test_fallback_info_from_response_absent_header_returns_none():
    class _Resp:
        llm_output = {"model_name": "equity-agent/default"}
        generations = []

    assert _fallback_info_from_response(_Resp()) is None


def test_served_model_callback_records_fallback_from_response():
    """A mocked LiteLLM response whose headers report a fallback lands in the tracker
    with the real served model."""

    class _Resp:
        llm_output = {
            "headers": {"x-litellm-attempted-fallbacks": "1"},
            "model_name": "meta-llama/llama-4-scout-17b-16e-instruct",
        }

    tracker = ServedModelTracker()
    cb = _ServedModelCallback(tracker, "equity-agent/default")
    cb.on_llm_end(_Resp(), run_id=uuid4())
    info = tracker.info()["equity-agent/default"]
    assert info.fallback_fired is True
    assert info.served_model == "meta-llama/llama-4-scout-17b-16e-instruct"


def test_served_model_callback_reads_streamed_generation_headers():
    """Headers can also arrive on a generation message's response_metadata."""

    class _Msg:
        response_metadata = {
            "headers": {"x-litellm-attempted-fallbacks": "0"},
            "model_name": "equity-agent/default",
        }

    class _Gen:
        message = _Msg()

    class _Resp:
        llm_output = None
        generations = [[_Gen()]]

    tracker = ServedModelTracker()
    cb = _ServedModelCallback(tracker, "equity-agent/default")
    cb.on_llm_end(_Resp(), run_id=uuid4())
    assert tracker.info()["equity-agent/default"].fallback_fired is False


def test_served_model_tracker_fallback_is_sticky():
    """Once any call on an alias falls back, the run is marked fallback regardless
    of a later clean call on the same alias, and keeps the served model."""
    tracker = ServedModelTracker()
    tracker.record("equity-agent/default", fallback_fired=True, served_model="scout")
    tracker.record("equity-agent/default", fallback_fired=False, served_model="")
    info = tracker.info()["equity-agent/default"]
    assert info.fallback_fired is True
    assert info.served_model == "scout"
