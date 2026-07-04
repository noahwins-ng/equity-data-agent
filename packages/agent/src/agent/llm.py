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
from dataclasses import dataclass
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

# QNT-220 (#7) per-node model tiering. ``classify`` / ``plan`` / the
# exploration decision are simple structured calls (in -> short structured
# out) that the 70b model is wildly oversized for (the 14-day baseline showed
# classify spending 1,251 input tokens to produce an ~11-token decision). Those
# nodes call ``get_llm(model_alias=SMALL_NODE_ALIAS)``; ``synthesize`` /
# ``narrate`` keep the default 70b by calling ``get_llm()`` with no alias. One
# constant per tier so AC3's "revert any node where the small model regresses"
# is a one-line change. The alias must exist in ``litellm_config.yaml``.
#
# QNT-220: gpt-oss-20b on Groq (free-tier TPD), NOT gemini-2.5-flash -- the
# gemini free tier caps at 20 requests/DAY which is non-viable for a node that
# runs on 100% of turns (see reference_gemini_free_tier_rpd).
SMALL_NODE_ALIAS = "equity-agent/small"

# QNT-182: Static map from LiteLLM alias to the upstream provider/model the
# alias resolves to, kept in sync with ``litellm_config.yaml``. Used to stamp
# Langfuse traces with the real model name -- LangChain only sees the alias
# (we pass ``model=alias`` to ChatOpenAI), and LiteLLM echoes the alias back
# in the response, so without this map "which model served this trace" is
# unanswerable from observability. Does NOT capture fallback fires (when
# ``equity-agent/default`` falls back to ``equity-agent/fallback-llama4scout``
# on Groq TPD exhaustion); detecting that needs LiteLLM response inspection
# and is a follow-up.
_RESOLVED_MODEL_BY_ALIAS: dict[str, str] = {
    # QNT-258 / ADR-025: paid launch primary (was groq/llama-3.3-70b-versatile).
    "equity-agent/default": "openrouter/deepseek/deepseek-v4-flash",
    "equity-agent/fallback-nemotron-ultra": "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
    "equity-agent/fallback-llama4scout": "groq/meta-llama/llama-4-scout-17b-16e-instruct",
    "equity-agent/fallback-groq-gptoss120b": "groq/openai/gpt-oss-120b",
    "equity-agent/gemini": "gemini/gemini-2.5-flash",
    "equity-agent/small": "groq/openai/gpt-oss-20b",
    "equity-agent/bench-gptoss120b": "groq/openai/gpt-oss-120b",
    "equity-agent/bench-cerebras-gptoss120b": "cerebras/gpt-oss-120b",
    "equity-agent/bench-deepseek-v4-flash": "openrouter/deepseek/deepseek-v4-flash",
    "equity-agent/bench-gptoss20b": "groq/openai/gpt-oss-20b",
    "equity-agent/bench-llama4scout": "groq/meta-llama/llama-4-scout-17b-16e-instruct",
    "equity-agent/bench-qwen3-32b": "groq/qwen/qwen3-32b",
    "equity-agent/bench-gemma4-31b": "gemini/gemma-4-31b-it",
    "equity-agent/bench-gemma3-27b": "gemini/gemma-3-27b-it",
    "equity-agent/bench-gemini31flashlite": "gemini/gemini-3.1-flash-lite-preview",
    "equity-agent/bench-llama3-70b": "groq/llama-3.3-70b-versatile",
}

# QNT-230 (#10): the LLM-as-judge must stay on a FIXED model while the
# agent-under-test swaps. ``set_model_override`` (the QNT-129 bench sweep)
# re-routes plan / synthesize so a candidate model is benchmarked end to end --
# but if it ALSO re-routed the judge, each candidate would score its own output
# (Qwen judging Qwen, llama judging llama), and self-preference bias is well
# documented in LLM-as-judge setups. :func:`get_judge_llm` resolves this alias
# directly, independent of the override, so the judge is constant across a
# sweep. Same model the dialogue judge already pins
# (``dialogue_judge.JUDGE_MODEL_ALIAS``).
JUDGE_ALIAS = "equity-agent/bench-cerebras-gptoss120b"

# QNT-275 / ADR-023: the DeepEval RAGAS suite's judge. A judged record fires ~12
# judge calls, so a free-tier judge's daily token ceiling caps a run at ~20-35
# records. This paid OpenRouter alias (DeepSeek V4 Flash) has no such ceiling --
# a >=50-record baseline runs in one window for ~$0.18 -- so the DeepEval suite
# pins THIS judge while the dialogue / golden evals stay on the free
# ``JUDGE_ALIAS`` above. Reach it via ``get_judge_llm(model_alias=...)``.
DEEPEVAL_JUDGE_ALIAS = "equity-agent/bench-deepseek-v4-flash"

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


# QNT-218 dialogue-eval determinism override. When set, every ``get_llm()`` call
# ignores its ``temperature`` argument and uses this value instead. The dialogue
# eval pins it to 0.0 around a sweep so the agent-under-test stops contributing
# sampling variance (narrate streams at 0.3, plan/synthesize default to 0.2).
# This removes *sampling* variance only -- Groq's MoE serving is still
# non-deterministic, which is why the eval also reports per-axis error bars.
_TEMPERATURE_OVERRIDE: float | None = None


def set_temperature_override(temperature: float | None) -> None:
    """Force every subsequent ``get_llm()`` to use ``temperature``, or clear with None."""
    global _TEMPERATURE_OVERRIDE
    _TEMPERATURE_OVERRIDE = temperature


def _current_alias() -> str:
    """Return the alias ``get_llm()`` would currently use.

    Mirrors the alias-resolution branch in :func:`get_llm` so callers (e.g.
    the SSE handler tagging Langfuse traces) can stamp the right name without
    constructing an LLM instance. Falls back to the groq default on misconfig
    rather than raising, because telemetry-stamping must never break a request.
    """
    if _MODEL_OVERRIDE is not None:
        return _MODEL_OVERRIDE
    provider = settings.EQUITY_AGENT_PROVIDER.lower()
    return _ALIAS_BY_PROVIDER.get(provider, _ALIAS_BY_PROVIDER["groq"])


def current_model_info() -> dict[str, str]:
    """Return ``{"alias": ..., "resolved_model": ...}`` for trace tagging.

    Reads the active alias the same way ``get_llm()`` does and resolves it
    against the static :data:`_RESOLVED_MODEL_BY_ALIAS` map. Unknown aliases
    (e.g. someone added a new bench entry to ``litellm_config.yaml`` but
    forgot to update the map) resolve to ``"unknown"`` rather than raising
    -- the trace still carries the alias, the resolved-model field just
    flags the gap so a Langfuse filter on ``resolved_model = unknown``
    surfaces drift.
    """
    alias = _current_alias()
    return {
        "alias": alias,
        "resolved_model": _RESOLVED_MODEL_BY_ALIAS.get(alias, "unknown"),
    }


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


# ─── Served-model tracking (QNT-230 #14) ────────────────────────────────────


@dataclass(frozen=True)
class ServedModelInfo:
    """What a LiteLLM response told us about how a call was actually served.

    ``fallback_fired`` comes from the ``x-litellm-attempted-fallbacks`` header
    (the authoritative signal). ``served_model`` is ``response.model``: LiteLLM
    echoes the requested ALIAS there on a clean call (useless), but on a
    fallback it echoes the REAL served model (e.g.
    ``meta-llama/llama-4-scout-17b-16e-instruct``) -- so it's only trusted when
    ``fallback_fired`` is True.
    """

    fallback_fired: bool
    served_model: str = ""


class ServedModelTracker:
    """Records, per requested alias, whether LiteLLM served via a fallback.

    The static :data:`_RESOLVED_MODEL_BY_ALIAS` map answers "what does this
    alias point at" but NOT "did this call fall back". QNT-182 established that
    LiteLLM echoes the requested ALIAS in ``response.model`` (so the response
    *body* cannot reveal a fallback), but the response *headers* carry the
    authoritative ``x-litellm-attempted-fallbacks`` counter. When
    ``equity-agent/default`` falls over on Groq TPD exhaustion (QNT-227:
    fallbacks are transitive), this tracker captures that per-alias so the SSE
    handler can mark the trace instead of attributing the run to the primary
    model that never served it.
    """

    __slots__ = ("_lock", "_info")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._info: dict[str, ServedModelInfo] = {}

    def record(self, requested_alias: str, *, fallback_fired: bool, served_model: str) -> None:
        if not requested_alias:
            return
        with self._lock:
            prev = self._info.get(requested_alias)
            # Sticky: once any call on this alias fell back, the run did. Keep
            # the served model from whichever call actually fell back.
            fired = fallback_fired or (prev.fallback_fired if prev else False)
            if fallback_fired and served_model:
                model = served_model
            else:
                model = prev.served_model if prev else ""
            self._info[requested_alias] = ServedModelInfo(fired, model)

    def info(self) -> dict[str, ServedModelInfo]:
        with self._lock:
            return dict(self._info)


_SERVED_MODEL_TRACKER: contextvars.ContextVar[ServedModelTracker | None] = contextvars.ContextVar(
    "agent_served_model_tracker", default=None
)


def set_served_model_tracker(
    tracker: ServedModelTracker | None,
) -> contextvars.Token[ServedModelTracker | None]:
    """Install ``tracker`` for the current async context. Returns the reset token."""
    return _SERVED_MODEL_TRACKER.set(tracker)


def reset_served_model_tracker(token: contextvars.Token[ServedModelTracker | None]) -> None:
    """Restore the previous context-local value. Pair with ``set_served_model_tracker``."""
    _SERVED_MODEL_TRACKER.reset(token)


def _response_headers(response: Any) -> dict[str, Any]:
    """Pull the LiteLLM response headers out of a LangChain LLM response.

    With ``include_response_headers=True`` the headers land in
    ``llm_output["headers"]`` (``.invoke()`` paths) or in a generation
    message's ``response_metadata["headers"]``. Streamed chunks don't carry
    them -- the structured ``synthesize`` call (the one that falls back on TPD)
    does, which is what matters. Returns ``{}`` when absent.
    """
    out = getattr(response, "llm_output", None) or {}
    headers = out.get("headers")
    if isinstance(headers, dict):
        return headers
    for gen_list in getattr(response, "generations", None) or []:
        for gen in gen_list:
            message = getattr(gen, "message", None)
            meta = getattr(message, "response_metadata", None) or {}
            headers = meta.get("headers")
            if isinstance(headers, dict):
                return headers
    return {}


def _model_name_from_response(response: Any) -> str:
    """``response.model`` as LangChain surfaces it (``model_name``).

    The alias on a clean call, the real served model on a fallback. Empty when
    absent (e.g. a streamed chunk).
    """
    out = getattr(response, "llm_output", None) or {}
    model = out.get("model_name") or out.get("model")
    if model:
        return str(model)
    for gen_list in getattr(response, "generations", None) or []:
        for gen in gen_list:
            message = getattr(gen, "message", None)
            meta = getattr(message, "response_metadata", None) or {}
            model = meta.get("model_name") or meta.get("model")
            if model:
                return str(model)
    return ""


def _fallback_info_from_response(response: Any) -> ServedModelInfo | None:
    """Parse the x-litellm fallback signal + served model out of a response.

    Returns ``None`` when the headers don't carry the counter (so the caller
    records nothing and the run keeps the static resolution).
    """
    headers = _response_headers(response)
    raw = headers.get("x-litellm-attempted-fallbacks")
    if raw is None:
        return None
    try:
        fired = int(raw) > 0
    except (TypeError, ValueError):
        fired = False
    return ServedModelInfo(fallback_fired=fired, served_model=_model_name_from_response(response))


class _ServedModelCallback(BaseCallbackHandler):
    """LangChain callback that records the fallback signal for one alias's calls.

    One instance is bound per ``get_llm()`` call, so it knows the requested
    alias; ``on_llm_end`` reads the x-litellm-* headers LiteLLM returned and
    stores the result on the request-scoped :class:`ServedModelTracker`.
    """

    def __init__(self, tracker: ServedModelTracker, requested_alias: str) -> None:
        super().__init__()
        self._tracker = tracker
        self._alias = requested_alias

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
            info = _fallback_info_from_response(response)
        except Exception as exc:  # noqa: BLE001 — telemetry must never crash the request
            logger.warning("served-model extraction failed: %s", exc)
            return
        if info is not None:
            self._tracker.record(
                self._alias,
                fallback_fired=info.fallback_fired,
                served_model=info.served_model,
            )


def resolve_trace_model_tag(
    *,
    alias: str,
    static_resolved: str,
    served_info: dict[str, ServedModelInfo],
) -> tuple[str, bool]:
    """Return ``(model_tag_value, fallback_fired)`` for the trace ``model:`` tag.

    No fallback (the common case): the static alias resolution is correct, so
    existing Langfuse ``model:`` filters keep matching. On a genuine fallback
    (``x-litellm-attempted-fallbacks > 0``) the static map no longer describes
    the run, so we surface the model LiteLLM actually served (``response.model``
    carries the real model name on a fallback), or the explicit
    ``unverified-fallback`` marker if we couldn't read it -- either way the
    filter stops attributing the run to the primary model (QNT-230 #14). Falls
    back to ``unverified-alias`` when even the static map is blind.
    """
    info = served_info.get(alias)
    if info is not None and info.fallback_fired:
        return (info.served_model or "unverified-fallback"), True
    if static_resolved and static_resolved != "unknown":
        return static_resolved, False
    return "unverified-alias", False


def get_judge_llm(temperature: float = 0.0, model_alias: str | None = None) -> ChatOpenAI:
    """Return a ChatOpenAI pinned to a judge alias for LLM-as-judge scoring.

    Defaults to :data:`JUDGE_ALIAS` (the free bench-cerebras judge the dialogue /
    golden evals use). ``model_alias`` overrides it for a suite that needs a
    different judge -- the DeepEval RAGAS suite passes
    :data:`DEEPEVAL_JUDGE_ALIAS` (the paid OpenRouter DeepSeek judge, QNT-275) so
    its ~12-call/record budget isn't bound by the free-tier daily token ceiling,
    WITHOUT moving the dialogue/golden judge off the free model.

    Deliberately bypasses both ``_MODEL_OVERRIDE`` and ``_TEMPERATURE_OVERRIDE``:
    the judge must NOT move when a bench sweep re-routes the agent-under-test
    (QNT-230 #10), and it stays at temperature 0.0 for reproducible scores
    regardless of the dialogue-eval determinism override. No token tracker is
    attached -- judge calls run in the eval harness, outside the per-request
    budget context.
    """
    return ChatOpenAI(
        model=model_alias or JUDGE_ALIAS,
        base_url=settings.LITELLM_BASE_URL,
        api_key="litellm-proxy",  # pyright: ignore[reportArgumentType]  # proxy ignores; real keys server-side
        temperature=temperature,
        timeout=settings.LLM_REQUEST_TIMEOUT,
        # Mirror dialogue_judge.build_judge_llm: a transient judge-provider 429
        # must retry rather than drop the row to a (contaminating) None score in
        # history.csv (QNT-218 rationale, same pinned alias).
        max_retries=3,
    )


def get_llm(temperature: float = 0.2, model_alias: str | None = None) -> ChatOpenAI:
    """Return a ChatOpenAI bound to a LiteLLM alias.

    Alias precedence (highest first):

    1. ``_MODEL_OVERRIDE`` — the eval bench sweep (``--model``) re-routes every
       node to one model; it must still win over per-node tiering so a single
       ``--model bench-X`` flag benchmarks the whole graph (QNT-129).
    2. ``model_alias`` — QNT-220 (#7) per-node tiering: ``classify`` / ``plan``
       / exploration-decision pass :data:`SMALL_NODE_ALIAS` so simple structured
       calls run on a small/fast model. ``None`` (synthesize/narrate) falls
       through to the provider default.
    3. provider default — ``EQUITY_AGENT_PROVIDER`` lookup.
    """
    if _MODEL_OVERRIDE is not None:
        alias = _MODEL_OVERRIDE
    elif model_alias is not None:
        alias = model_alias
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
    served_tracker = _SERVED_MODEL_TRACKER.get()
    if served_tracker is not None:
        callbacks.append(_ServedModelCallback(served_tracker, alias))

    effective_temperature = (
        _TEMPERATURE_OVERRIDE if _TEMPERATURE_OVERRIDE is not None else temperature
    )

    return ChatOpenAI(
        model=alias,
        base_url=settings.LITELLM_BASE_URL,
        api_key="litellm-proxy",  # pyright: ignore[reportArgumentType]  # proxy ignores; real keys server-side
        temperature=effective_temperature,
        # QNT-150: bound every LLM call so a hung LiteLLM proxy / stalled
        # provider can't keep an SSE chat connection open forever.
        timeout=settings.LLM_REQUEST_TIMEOUT,
        # QNT-219: emit token usage on streamed runs (narrate streams via
        # .stream()). Without this, LangChain's ChatOpenAI omits the usage
        # block on streamed generations, so Langfuse recorded 0 prompt/
        # completion tokens for all 96 narrate calls in the 14-day baseline.
        # No-op for non-streamed .invoke() calls.
        stream_usage=True,
        # QNT-230 #14: surface LiteLLM's x-litellm-* headers in response_metadata
        # so _ServedModelCallback can read the authoritative
        # x-litellm-attempted-fallbacks counter. response.model only ever echoes
        # the requested alias (QNT-182), so the body can't reveal a fallback.
        include_response_headers=True,
        callbacks=callbacks or None,
    )


__all__ = [
    "JUDGE_ALIAS",
    "DEEPEVAL_JUDGE_ALIAS",
    "SMALL_NODE_ALIAS",
    "ServedModelInfo",
    "ServedModelTracker",
    "TokenUsageTracker",
    "current_model_info",
    "get_judge_llm",
    "get_llm",
    "reset_served_model_tracker",
    "reset_token_tracker",
    "resolve_trace_model_tag",
    "set_model_override",
    "set_served_model_tracker",
    "set_temperature_override",
    "set_token_tracker",
]
