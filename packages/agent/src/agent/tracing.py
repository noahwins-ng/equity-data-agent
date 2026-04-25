"""Langfuse tracing for the agent.

Any code path that calls the LLM in the agent package MUST go through
``langfuse.traced_invoke`` (or be wrapped by an ``@observe``-decorated caller).
That contract is checked by ``tests/test_tracing.py::test_no_raw_llm_invoke``.

Pattern:
    from agent.tracing import langfuse, observe
    from agent.llm import get_llm

    @observe()
    def synthesize(state):
        response = langfuse.traced_invoke(get_llm(), prompt, name="synthesize")
        return response.content

When ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` are unset (e.g. unit tests)
tracing silently no-ops so callers don't need to branch on environment.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage
from langfuse import Langfuse, observe
from shared.config import settings

logger = logging.getLogger(__name__)


def _usage_from_response(response: BaseMessage) -> dict[str, int] | None:
    """langchain-openai surfaces token counts on ``AIMessage.usage_metadata``
    for OpenAI-compatible providers — LiteLLM passes them through from
    Groq / Gemini. Returns None if unavailable so Langfuse falls back to its
    own tokenizer."""
    if not isinstance(response, AIMessage):
        return None
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return None
    return {
        "input": int(usage.get("input_tokens", 0)),
        "output": int(usage.get("output_tokens", 0)),
        "total": int(usage.get("total_tokens", 0)),
    }


def _model_from_response(response: BaseMessage) -> str | None:
    if not isinstance(response, AIMessage):
        return None
    meta = getattr(response, "response_metadata", None) or {}
    return meta.get("model_name") or meta.get("model")


class LangfuseResource:
    """Configured Langfuse client + helpers for tracing agent runs.

    Initialised from ``shared.settings``. When keys are missing tracing is
    disabled at the SDK level and ``traced_invoke`` degrades to a plain
    ``llm.invoke`` — matches the "no agent code path emits an LLM call without
    a Langfuse trace" AC: with keys set every call is traced; without keys
    the call still works but no trace is emitted (and none could be).
    """

    def __init__(self) -> None:
        self.enabled = bool(settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY)
        # Skip client construction entirely when disabled — the Langfuse SDK
        # emits a loud "Authentication error" at init with empty keys even when
        # tracing_enabled=False, which would scare users running tests offline.
        self._client: Langfuse | None = (
            Langfuse(
                public_key=settings.LANGFUSE_PUBLIC_KEY,
                secret_key=settings.LANGFUSE_SECRET_KEY,
                base_url=settings.LANGFUSE_BASE_URL,
            )
            if self.enabled
            else None
        )
        if not self.enabled:
            logger.info("Langfuse keys not configured; agent tracing disabled.")

    @property
    def client(self) -> Langfuse | None:
        return self._client

    def traced_invoke(
        self,
        llm: Any,
        prompt: str | list[BaseMessage],
        *,
        name: str = "llm-call",
    ) -> BaseMessage:
        """Invoke ``llm`` inside a Langfuse generation span.

        Captures prompt, output, model name, and token usage. Latency is
        captured automatically by the span's start/end times. If tracing is
        disabled, invokes the LLM unchanged.

        ``prompt`` accepts either a plain string (used by the plan node, which
        emits a one-shot user instruction) or a list of ``BaseMessage`` (used
        by the synthesize node since QNT-58, which delivers ``SYSTEM_PROMPT``
        in the system turn rather than flattened into the user message).

        QNT-60 follow-up: add an async ``traced_ainvoke`` wrapping
        ``llm.ainvoke`` for SSE streaming — the AST contract test already
        covers ``ainvoke`` so adding the wrapper first keeps CI green.
        """
        if not self.enabled or self._client is None:
            return llm.invoke(prompt)

        # Langfuse's `input` field expects something serialisable to JSON; a
        # ``list[BaseMessage]`` round-trips fine because BaseMessage subclasses
        # are pydantic models. We pass it through unchanged so the dashboard
        # shows the system + user turns separately.
        with self._client.start_as_current_observation(
            as_type="generation",
            name=name,
            input=prompt,
        ) as gen:
            try:
                response = llm.invoke(prompt)
            except Exception as exc:
                # Tag the span so failed runs show up in the dashboard instead of
                # hanging as empty generations, then let the caller see the error.
                gen.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
                raise
            output = response.content if hasattr(response, "content") else str(response)
            gen.update(
                output=output,
                model=_model_from_response(response),
                usage_details=_usage_from_response(response),
            )
            return response

    def flush(self) -> None:
        """Block until queued events reach Langfuse. Call at process exit —
        CLI runs are short-lived and would otherwise drop the trailing spans."""
        if self._client is not None:
            self._client.flush()


langfuse = LangfuseResource()


__all__ = ["LangfuseResource", "langfuse", "observe"]
