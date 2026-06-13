"""Classify an eval failure as provider pressure vs an app regression (QNT-234).

QNT-233's routing fix was verified during a bad Groq window: comparison
synthesis hit ``APITimeoutError`` and several records ran ~186s (3x the 60s
per-call ``LLM_REQUEST_TIMEOUT`` -- plan, synthesis, and narrate each ran to
the ceiling). The structured golden-set harness flattened those into the same
``hallucination_ok=False`` / exit-1 path as a real hallucination or tool-call
regression, so a free-tier capacity blip looked identical to a code regression
and blocked the fix.

This module answers one question for the eval harness: when ``graph.invoke``
raises, is the cause the provider running out of capacity (rate limit, TPM/TPD
quota, request timeout, upstream 5xx) or our code being wrong? A
provider-pressure failure is infrastructure -- it must surface loudly but must
NOT gate the contract exit code (``golden_set.is_failing``) or pollute the
committed quality trend (``history.csv``).

Detection walks the exception chain (``__cause__`` / ``__context__``) because
LiteLLM / langchain_openai re-wrap the upstream openai/httpx error, and matches
on both the class name and the message text so a re-wrapped error whose type was
flattened to a bare ``Exception`` still trips on its message.
"""

from __future__ import annotations

# Exception class names raised by the openai SDK / httpx / LiteLLM on
# provider-capacity or transport-timeout conditions. Matched by name (not
# import) so a LiteLLM re-wrap that preserves the class name still trips, and so
# this module stays import-light. ``ContextWindowExceededError`` is deliberately
# ABSENT -- an over-long prompt is an app / token-budget bug (AC5 territory),
# not provider pressure, and must stay a real failure.
_PROVIDER_ERROR_TYPE_NAMES = frozenset(
    {
        "APITimeoutError",
        "APIConnectionError",
        "APIConnectionTimeoutError",
        "Timeout",
        "RateLimitError",
        "InternalServerError",
        "ServiceUnavailableError",
    }
)

# Message substrings (lowercased) that mark provider pressure even when a
# wrapper flattened the class name to a bare ``Exception`` / ``ValueError``.
# Deliberately HIGH-PRECISION: only signatures unambiguous for LLM provider
# capacity. Transport timeouts and bare 5xx codes are intentionally ABSENT --
# the type-name set already catches the openai SDK's ``APITimeoutError`` /
# ``APIConnectionError`` / ``InternalServerError`` (preserved through the
# LiteLLM proxy), and a bare "timeout" / "502" substring would also match a
# timeout against OUR report API being down, which is a different infra failure
# that must fail the suite loudly, not be silently excluded as provider pressure.
_PROVIDER_ERROR_SUBSTRINGS = (
    "rate limit",
    "ratelimit",
    "rate_limit",
    "quota",
    "insufficient_quota",
    "tokens per day",
    "tokens per minute",
    "too many requests",
    "error code: 429",
    "overloaded",
)


def _chain(exc: BaseException) -> list[BaseException]:
    """The exception plus its ``__cause__`` / ``__context__`` ancestry, de-cycled."""
    out: list[BaseException] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def is_provider_pressure_error(exc: BaseException) -> bool:
    """True when ``exc`` (or any cause in its chain) is a provider-capacity failure."""
    for err in _chain(exc):
        if type(err).__name__ in _PROVIDER_ERROR_TYPE_NAMES:
            return True
        if any(sub in str(err).lower() for sub in _PROVIDER_ERROR_SUBSTRINGS):
            return True
    return False


def provider_error_label(exc: BaseException) -> str:
    """Short ``provider: <Type>`` reason for the eval row.

    Names the recognised provider error type from the chain when there is one
    (e.g. ``provider: RateLimitError``); falls back to the outermost type when
    only a message substring matched. Empty string when ``exc`` is not provider
    pressure, so callers can branch on truthiness.
    """
    if not is_provider_pressure_error(exc):
        return ""
    for err in _chain(exc):
        if type(err).__name__ in _PROVIDER_ERROR_TYPE_NAMES:
            return f"provider: {type(err).__name__}"
    return f"provider: {type(exc).__name__}"


__all__ = [
    "is_provider_pressure_error",
    "provider_error_label",
]
