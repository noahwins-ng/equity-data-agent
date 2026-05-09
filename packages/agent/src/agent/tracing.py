"""Langfuse tracing — LangGraph CallbackHandler at graph entry (ADR-019).

Pattern at the request boundary (FastAPI ``agent_chat`` and CLI ``analyze``)::

    @observe(name="agent-chat")
    def run(...):
        with propagate_attributes(trace_name="agent-chat",
                                  session_id=..., user_id=...):
            handler = make_callback_handler()
            config = {"callbacks": [handler]} if handler else {}
            graph.invoke(state, config=config)

Graph nodes accept ``(state, config: RunnableConfig)`` and forward ``config``
to inner ``llm.invoke(prompt, config=config)`` so the handler reaches every
LLM call. When keys are unset (tests, eval bench runs) ``make_callback_handler``
returns ``None`` and callers branch on the empty-config form above.
"""

from __future__ import annotations

import logging

from langfuse import Langfuse, observe, propagate_attributes
from langfuse.langchain import CallbackHandler
from shared.config import settings

logger = logging.getLogger(__name__)


def _build_client() -> Langfuse | None:
    if not (settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY):
        logger.info("Langfuse keys not configured; agent tracing disabled.")
        return None
    return Langfuse(
        public_key=settings.LANGFUSE_PUBLIC_KEY,
        secret_key=settings.LANGFUSE_SECRET_KEY,
        base_url=settings.LANGFUSE_BASE_URL,
        sample_rate=settings.LANGFUSE_SAMPLE_RATE,
    )


langfuse: Langfuse | None = _build_client()


def make_callback_handler() -> CallbackHandler | None:
    """Per-request handler factory; ``None`` when tracing disabled."""
    return CallbackHandler() if langfuse is not None else None


def flush() -> None:
    """Block until queued events reach Langfuse; call at process exit."""
    if langfuse is not None:
        langfuse.flush()


__all__ = [
    "CallbackHandler",
    "flush",
    "langfuse",
    "make_callback_handler",
    "observe",
    "propagate_attributes",
]
