from langchain_openai import ChatOpenAI
from shared.config import settings

_ALIAS_BY_PROVIDER = {
    "groq": "equity-agent/default",
    "gemini": "equity-agent/gemini",
}


def get_llm(temperature: float = 0.2) -> ChatOpenAI:
    provider = settings.EQUITY_AGENT_PROVIDER.lower()
    if provider not in _ALIAS_BY_PROVIDER:
        raise ValueError(
            f"Unknown EQUITY_AGENT_PROVIDER={provider!r}; "
            f"expected one of {sorted(_ALIAS_BY_PROVIDER)}"
        )
    return ChatOpenAI(
        model=_ALIAS_BY_PROVIDER[provider],
        base_url=settings.LITELLM_BASE_URL,
        api_key="litellm-proxy",  # pyright: ignore[reportArgumentType]  # proxy ignores; real keys server-side
        temperature=temperature,
    )
