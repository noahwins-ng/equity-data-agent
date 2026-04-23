import pytest
from agent.llm import _ALIAS_BY_PROVIDER, get_llm


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
