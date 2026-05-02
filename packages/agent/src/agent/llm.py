"""LLM factory + per-request token tracker (QNT-129, QNT-161).

Every agent LLM call goes through ``get_llm()``. This module owns two
runtime concerns the rest of the agent doesn't have to think about:

1. **Provider routing** — ``EQUITY_AGENT_PROVIDER`` (or the QNT-129 bench
   override) picks the LiteLLM model alias. Rest of the agent uses
   ``get_llm()`` and the routing decision is invisible.
2. **Per-request token accounting** (QNT-161) — the SSE chat endpoint
   needs to know how many Groq tokens a single chat run burned so it can
   debit the per-IP + global daily budgets. We thread that via a
   ``contextvars.ContextVar`` set by the SSE handler before each request:
   ``get_llm()`` reads it and attaches a LangChain callback that sums
   ``response.usage.total_tokens`` across every LLM call. The variable
   propagates through ``asyncio.to_thread`` automatically (Python's
   contextvars copy semantics), so the worker thread that runs the graph
   sees the same tracker the SSE coroutine installed.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from typing import TYPE_CHECKING, Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI
from shared.config import settings

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)

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


# ─── Per-request token tracking (QNT-161) ───────────────────────────────────


class TokenUsageTracker:
    """Thread-safe accumulator for tokens used during one chat run.

    Multiple LLM calls (classify, plan, synthesize) all charge into the same
    tracker. The SSE handler reads ``total`` after the graph completes and
    debits the per-IP + global budgets in one shot. We sum total_tokens
    (prompt + completion) because that's what Groq counts against TPD.
    """

    __slots__ = ("_lock", "_total")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = 0

    def add(self, tokens: int) -> None:
        if tokens <= 0:
            return
        with self._lock:
            self._total += tokens

    @property
    def total(self) -> int:
        with self._lock:
            return self._total


# Per-request context-local. The SSE handler does
# ``set_token_tracker(tracker)`` before invoking the graph; ``get_llm()``
# inside the graph reads it and attaches the callback. None = no tracking
# (CLI runs, eval harness, unit tests that don't care about budgets).
_TOKEN_TRACKER: contextvars.ContextVar[TokenUsageTracker | None] = contextvars.ContextVar(
    "agent_token_tracker", default=None
)


def set_token_tracker(
    tracker: TokenUsageTracker | None,
) -> contextvars.Token[TokenUsageTracker | None]:
    """Install ``tracker`` for the current async context. Returns the reset
    token so the caller can ``_TOKEN_TRACKER.reset(token)`` on teardown
    (the SSE handler does this in its finally clause)."""
    return _TOKEN_TRACKER.set(tracker)


def reset_token_tracker(token: contextvars.Token[TokenUsageTracker | None]) -> None:
    """Restore the previous context-local value. Pair with ``set_token_tracker``."""
    _TOKEN_TRACKER.reset(token)


class _UsageCallback(BaseCallbackHandler):
    """LangChain callback that pulls ``token_usage`` out of each LLM response
    and adds it to the bound tracker.

    LiteLLM forwards the OpenAI ``usage`` block straight through, so the path
    is: provider → LiteLLM proxy → langchain_openai → ``response.llm_output``
    on the ``on_llm_end`` hook. ``total_tokens`` is what counts against
    Groq's TPD ceiling; we sum that. When the proxy strips the usage block
    (rare, but observed on some structured-output paths) we silently record 0
    — the budget isn't perfectly tight but it never under-bills.
    """

    def __init__(self, tracker: TokenUsageTracker) -> None:
        super().__init__()
        self._tracker = tracker

    def on_llm_end(  # type: ignore[override]
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        del run_id, parent_run_id, kwargs
        try:
            llm_output = getattr(response, "llm_output", None) or {}
            usage = llm_output.get("token_usage") or {}
            total = int(usage.get("total_tokens") or 0)
        except Exception as exc:  # noqa: BLE001 — never let telemetry crash the request
            logger.warning("token-usage callback failed: %s", exc)
            total = 0
        self._tracker.add(total)


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

    callbacks: list[BaseCallbackHandler] = []
    tracker = _TOKEN_TRACKER.get()
    if tracker is not None:
        callbacks.append(_UsageCallback(tracker))

    return ChatOpenAI(
        model=alias,
        base_url=settings.LITELLM_BASE_URL,
        api_key="litellm-proxy",  # pyright: ignore[reportArgumentType]  # proxy ignores; real keys server-side
        temperature=temperature,
        # QNT-150: bound every LLM call so a hung LiteLLM proxy / stalled
        # provider can't keep an SSE chat connection open forever.
        timeout=settings.LLM_REQUEST_TIMEOUT,
        callbacks=callbacks or None,
    )


__all__ = [
    "TokenUsageTracker",
    "get_llm",
    "reset_token_tracker",
    "set_model_override",
    "set_token_tracker",
]
