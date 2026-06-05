import pytest
from agent.llm import (
    _ALIAS_BY_PROVIDER,
    get_llm,
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


def test_stream_usage_enabled(monkeypatch):
    """QNT-219: streamed runs (narrate) must request token usage in the final
    chunk, else Langfuse records 0 tokens for every streamed generation."""
    from shared import config as cfg

    monkeypatch.setattr(cfg.settings, "EQUITY_AGENT_PROVIDER", "groq")
    assert get_llm().stream_usage is True
