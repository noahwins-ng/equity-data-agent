"""Retry-After header parsing for external HTTP APIs (QNT-63).

External fetches in this codebase have two retry layers:

1. **Asset-level Dagster RetryPolicy** — owns retries that re-execute
   the whole op. Configured on `ohlcv_raw`, `fundamentals`, `news_raw`
   with `max_retries=3, delay=30, Backoff.EXPONENTIAL, Jitter.PLUS_MINUS`.

2. **In-attempt retries inside fetchers** — own transient blips that
   should not burn an asset retry slot (e.g. a single Finnhub 5xx, or
   a 429 with a server-suggested Retry-After). Lives in this module.

Per RFC 9110 §10.2.3 the Retry-After header is either delta-seconds
or an HTTP-date; this module accepts both and clamps the result to a
sensible ceiling so a hostile / mis-formatted header can't sleep the
asset for hours.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

logger = logging.getLogger(__name__)

# Upper bound on a single in-process sleep, regardless of what the server
# advertises. yfinance and Finnhub free tiers reset within the minute, so
# anything beyond ~5 min is a signal that the run should fail and let the
# asset-level RetryPolicy re-launch — which buys jittered backoff and
# fresh process state, both useful for true outages.
MAX_RETRY_AFTER_SECONDS = 300.0


def parse_retry_after(value: str | bytes | None) -> float | None:
    """Parse an RFC 9110 Retry-After value into seconds.

    Accepts delta-seconds (``"60"``) or HTTP-date
    (``"Wed, 21 Oct 2015 07:28:00 GMT"``). Returns None for missing /
    malformed inputs so the caller can fall back to a default delay.
    Result is clamped to ``[0, MAX_RETRY_AFTER_SECONDS]``.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    raw = str(value).strip()
    if not raw:
        return None

    try:
        seconds: float | None = float(raw)
    except ValueError:
        seconds = None

    if seconds is None:
        # HTTP-date branch. parsedate_to_datetime returns None for bad
        # inputs on older stdlib, raises on newer; handle both.
        try:
            target = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if target is None:
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        seconds = (target - datetime.now(UTC)).total_seconds()

    if seconds < 0:
        seconds = 0.0
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def retry_after_seconds_from_exception(exc: BaseException) -> float | None:
    """Extract Retry-After (seconds) from an exception's attached response.

    Walks ``exc.response.headers['Retry-After']`` defensively. Any library
    that attaches the original response to its exception (``httpx``,
    ``requests``) flows through this path. yfinance's ``YFRateLimitError``
    discards the response and returns None here — the caller's default
    delay still applies in that case.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers: Any = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        # httpx.Headers / requests.structures.CaseInsensitiveDict are case-
        # insensitive, but a plain dict is not — try both spellings.
        raw = headers.get("Retry-After")
        if raw is None:
            raw = headers.get("retry-after")
    except (AttributeError, TypeError):
        return None
    return parse_retry_after(raw)
