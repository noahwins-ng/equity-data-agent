from langchain_openai import ChatOpenAI
from shared.config import settings

_ALIAS_BY_PROVIDER = {
    "groq": "equity-agent/default",
    "gemini": "equity-agent/gemini",
}

# QNT-129 bench harness override. When set, every ``get_llm()`` call returns a
# ChatOpenAI pointed at this alias instead of the provider lookup. Set via
# ``set_model_override(...)`` from ``agent.evals.__main__ --model`` so one
# flag re-routes plan / synthesize / judge in a single sweep without touching
# the production ``EQUITY_AGENT_PROVIDER`` env var.
_MODEL_OVERRIDE: str | None = None


def set_model_override(alias: str | None) -> None:
    """Force every subsequent ``get_llm()`` to return ``alias``, or clear with None."""
    global _MODEL_OVERRIDE
    _MODEL_OVERRIDE = alias


def get_llm(temperature: float = 0.2) -> ChatOpenAI:
    if _MODEL_OVERRIDE is not None:
        alias = _MODEL_OVERRIDE
    else:
        provider = settings.EQUITY_AGENT_PROVIDER.lower()
        if provider not in _ALIAS_BY_PROVIDER:
            raise ValueError(
                f"Unknown EQUITY_AGENT_PROVIDER={provider!r}; "
                f"expected one of {sorted(_ALIAS_BY_PROVIDER)}"
            )
        alias = _ALIAS_BY_PROVIDER[provider]
    return ChatOpenAI(
        model=alias,
        base_url=settings.LITELLM_BASE_URL,
        api_key="litellm-proxy",  # pyright: ignore[reportArgumentType]  # proxy ignores; real keys server-side
        temperature=temperature,
    )
