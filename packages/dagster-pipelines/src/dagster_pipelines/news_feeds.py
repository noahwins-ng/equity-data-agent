"""Finnhub /company-news client for the news_raw asset (QNT-141, per ADR-015).

Replaces the prior Yahoo Finance RSS surface. Finnhub gives per-publisher
attribution + article images + a 1y historical window — none of which RSS
returned. ADR-015 documents the source pick + alternatives considered.

Free tier (verified premium:null in the docs JSON, 2026-04-27): 60 req/min,
1y historical backfill via the from/to query params. Empty FINNHUB_API_KEY
makes ``fetch_company_news`` raise — the asset surfaces this rather than
silently degrading, since topology (a) (ADR-015 §Decision) needs real rows
to drive the downstream classifier.

Also exposes ``resolve_publisher_host`` (QNT-148): Finnhub serves
"Yahoo"/"Benzinga"/"CNBC"-tagged articles via ``finnhub.io/api/news?id=...``
redirects, so the URL host is opaque to us. Resolving the redirect at
ingest time lets the news pill render the actual outlet (cnbc.com,
seekingalpha.com, …) instead of the redirect host. See ADR-016.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import date
from typing import Any
from urllib.parse import urlparse

import httpx
from shared.config import settings

from dagster_pipelines.retry_helpers import retry_after_seconds_from_exception

logger = logging.getLogger(__name__)

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
COMPANY_NEWS_PATH = "/company-news"

# Per-call timeout. Finnhub typically responds in <1s; 30s is generous so a
# transient slowdown doesn't false-fail the asset. Retries are owned by the
# Dagster RetryPolicy on news_raw, not the HTTP client.
_REQUEST_TIMEOUT_SECONDS = 30.0

# Intra-attempt retry budget for /company-news (QNT-63). A single transient
# 5xx (or a 429 with a Retry-After) should not burn an entire asset retry
# slot — that policy waits 30s+ and re-launches the whole op. Two extra
# in-process tries (3 total) absorbs typical Finnhub blips while still
# bubbling persistent 5xx so the asset-level RetryPolicy can engage.
_INTRA_ATTEMPT_MAX_RETRIES = 2
# Conservative base for exponential in-attempt backoff. Finnhub's free tier
# is 60 RPM (~1 RPS); 1s → 2s pauses keep us inside the bucket while still
# absorbing the transient blip. Skipped when Retry-After is provided.
_INTRA_ATTEMPT_BASE_DELAY_SECONDS = 1.0

# Publisher-resolution timeout. Tighter than the API call — we'd rather give
# up and store '' than have one slow outlet stall the per-partition run.
# 5s with up to 5 hops covers the observed Finnhub → outlet redirect chain
# (typically 1-2 hops, occasionally a CDN bounce makes it 3).
_RESOLVE_TIMEOUT_SECONDS = 5.0
_RESOLVE_MAX_REDIRECTS = 5

# Finnhub redirect host — articles sourced from Yahoo/Benzinga/CNBC/etc.
# come through this URL and need resolution; everything else is already a
# direct outlet URL and ``urlparse`` is the answer.
_FINNHUB_REDIRECT_HOST = "finnhub.io"

# Per-process rate limit on requests to ``finnhub.io``. ADR-015 documents
# 60 RPM on the authenticated ``/company-news`` endpoint; the unauthenticated
# redirect server at ``/api/news?id=...`` empirically appears to share or
# closely shadow that bucket. The QNT-148 prod smoke (NVDA partition, 250
# articles) ran at ~4 RPS uncapped and got 43% resolution success; the failed
# URLs all returned ``Location: /`` consistent with throttle activation. With
# Dagster's max_concurrent_runs=3 the aggregate ceiling is 3 × 1/_INTER_FINNHUB
# = 2 RPS, well under the 1 RPS-per-bucket free-tier limit with a 2x cushion.
# Jitter avoids a thundering herd if multiple partitions launch in lockstep.
_INTER_FINNHUB_REQUEST_SECONDS = 1.5
_FINNHUB_JITTER_SECONDS = 0.2

# Process-local "when did we last hit finnhub.io" tracker. Subprocess workers
# launched by DockerRunLauncher each own a fresh copy, so this is a per-
# partition limiter — see the comment block above for the aggregate budget.
_last_finnhub_request_at: float = 0.0


def _sleep_for_finnhub_rate_limit() -> None:
    """Block until the next Finnhub request is allowed by the rate budget.

    No-op if enough wall-clock time has already passed since the last call;
    otherwise sleeps the remainder plus 0-200ms jitter. Updates the tracker
    after the (potential) sleep so back-to-back calls space correctly even
    under burst.
    """
    global _last_finnhub_request_at
    now = time.monotonic()
    deadline = _last_finnhub_request_at + _INTER_FINNHUB_REQUEST_SECONDS
    if now < deadline:
        time.sleep(deadline - now + random.uniform(0, _FINNHUB_JITTER_SECONDS))
    _last_finnhub_request_at = time.monotonic()


class FinnhubAPIKeyMissing(RuntimeError):
    """Raised when FINNHUB_API_KEY is empty. Surfaced to the asset so the
    failure mode is "loud and obvious" rather than "silent empty insert"."""


def _is_retriable_status_error(exc: httpx.HTTPStatusError) -> bool:
    """5xx and 429 are transient; 4xx auth / bad-symbol errors are not.

    Splitting this out makes the retry loop's intent obvious and gives the
    test a single seam — flip a 503 to a 401 and the retry budget should
    not engage.
    """
    code = exc.response.status_code
    return code == 429 or 500 <= code < 600


def _fetch_with_intra_attempt_retry(
    http: httpx.Client,
    url: str,
    params: dict[str, Any],
    ticker: str,
) -> Any:
    """GET ``url`` with a small intra-attempt retry budget on 5xx / 429.

    Honors the response's ``Retry-After`` header when present (delta-seconds
    or HTTP-date, parsed by ``retry_helpers``); otherwise applies a short
    exponential backoff (1s, 2s, …) bounded by ``_INTRA_ATTEMPT_MAX_RETRIES``.

    Persistent 4xx (auth / bad symbol) and persistent 5xx still raise
    ``httpx.HTTPStatusError`` so the asset-level Dagster RetryPolicy can
    re-launch the run. Non-list payloads are surfaced to the caller via
    the existing ``payload`` check there.
    """
    last_exc: httpx.HTTPStatusError | None = None
    for attempt in range(_INTRA_ATTEMPT_MAX_RETRIES + 1):
        try:
            response = http.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if not _is_retriable_status_error(exc):
                raise
            last_exc = exc
            if attempt == _INTRA_ATTEMPT_MAX_RETRIES:
                # Budget exhausted — bubble so the asset RetryPolicy gets
                # a chance to re-launch the whole op with jittered backoff.
                raise
            wait = retry_after_seconds_from_exception(exc)
            if wait is None or wait <= 0:
                wait = _INTRA_ATTEMPT_BASE_DELAY_SECONDS * (2**attempt)
            logger.warning(
                "Finnhub /company-news %s for %s — retry %d/%d in %.1fs",
                exc.response.status_code,
                ticker,
                attempt + 1,
                _INTRA_ATTEMPT_MAX_RETRIES,
                wait,
            )
            time.sleep(wait)
    # Unreachable: the loop either returns or raises. Guard for type-checker.
    assert last_exc is not None
    raise last_exc


def fetch_company_news(
    ticker: str,
    *,
    from_date: date,
    to_date: date,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Fetch Finnhub /company-news for ``ticker`` between ``from_date`` and ``to_date``.

    Returns the raw JSON list (one dict per article). Field shape per Finnhub
    docs: ``{category, datetime, headline, id, image, related, source, summary, url}``.

    Args:
        ticker: Symbol passed verbatim to Finnhub's ``symbol`` parameter.
        from_date: Inclusive lower bound (YYYY-MM-DD).
        to_date: Inclusive upper bound (YYYY-MM-DD). Same-day values are valid.
        client: Optional pre-configured httpx Client (used in tests for stubbing
            transports). When None, a fresh Client is created per call.

    Raises:
        FinnhubAPIKeyMissing: If ``settings.FINNHUB_API_KEY`` is empty.
        httpx.HTTPStatusError: For persistent non-2xx (4xx auth, 4xx rate
            limit, or 5xx that survives the intra-attempt retry budget).
    """
    api_key = settings.FINNHUB_API_KEY
    if not api_key:
        raise FinnhubAPIKeyMissing(
            "FINNHUB_API_KEY is empty. Set it in .env (dev) or SOPS prod secrets "
            "(prod). Register at https://finnhub.io/register."
        )

    params = {
        "symbol": ticker,
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "token": api_key,
    }

    owns_client = client is None
    http = client or httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS)
    try:
        payload = _fetch_with_intra_attempt_retry(
            http,
            f"{FINNHUB_BASE_URL}{COMPANY_NEWS_PATH}",
            params,
            ticker,
        )
    finally:
        if owns_client:
            http.close()

    if not isinstance(payload, list):
        # Finnhub error payloads are objects (`{"error": "..."}`), not lists.
        # Treat anything non-list as a non-fatal "no rows this tick" — empty
        # list ([]) is the legitimate "no news" signal and stays valid. The
        # error text propagates to the caller's log so the operator can tell
        # a quiet ticker apart from a bad-symbol / bad-key configuration error
        # without digging into raw HTTP traces.
        error_text = payload.get("error") if isinstance(payload, dict) else None
        logger.warning(
            "Finnhub /company-news for %s returned non-list payload (error=%r, raw=%r)",
            ticker,
            error_text,
            payload,
        )
        return []

    return payload


def _strip_www(host: str) -> str:
    """Drop the leading ``www.`` so the news pill renders ``fool.com`` not
    ``www.fool.com``. Frontend used to do this client-side; per QNT-148 the
    canonical publisher comes from the API, so the normalisation moves here."""
    return host.removeprefix("www.")


def make_resolver_client() -> httpx.Client:
    """Construct an ``httpx.Client`` configured for redirect resolution.

    Centralises the timeout / redirect-following config so callers (the
    ``news_raw`` asset) and the resolver itself share one source of truth
    for the redirect budget. The asset uses one pooled client per
    partition; without this factory it would have to import the underlying
    constants and risk drift.
    """
    return httpx.Client(
        timeout=_RESOLVE_TIMEOUT_SECONDS,
        follow_redirects=True,
        max_redirects=_RESOLVE_MAX_REDIRECTS,
    )


def resolve_publisher_host(
    url: str,
    *,
    client: httpx.Client | None = None,
) -> str:
    """Return the apparent outlet host for a news article URL.

    For ``finnhub.io/api/news?id=...`` redirects, follows up to
    ``_RESOLVE_MAX_REDIRECTS`` hops with a ``_RESOLVE_TIMEOUT_SECONDS``
    deadline and returns the *final* URL's host. For URLs already on a
    direct outlet (anything other than ``finnhub.io``), short-circuits with
    no network call — the URL host is the answer.

    Soft-fails to ``""`` on any error (timeout, non-2xx, malformed URL,
    redirect loop). Per AC #2 / AC #6 in QNT-148: the asset must not crash
    when outlet servers are flaky, and the frontend pill must keep
    rendering by falling back to ``publisher_name``.

    Note that ``HEAD`` is preferred over ``GET`` so we don't transfer the
    article body. Some outlets (notably a few cloudflare-fronted ones)
    answer 403/405 to ``HEAD`` but accept ``GET``; we retry with a streamed
    ``GET`` (no body read) in that case.

    Args:
        url: Article URL from ``equity_raw.news_raw.url``.
        client: Optional pre-configured ``httpx.Client`` (used in tests for
            stubbing transports). When None, a fresh Client is created and
            closed per call.

    Returns:
        Lowercased host without ``www.`` prefix on success, ``""`` on any
        failure or unrecognised URL shape.
    """
    if not url:
        return ""

    parsed_input = urlparse(url)
    host = (parsed_input.hostname or "").lower()
    if not host:
        return ""
    if host != _FINNHUB_REDIRECT_HOST:
        # Already a direct outlet URL — short-circuit, no network call.
        return _strip_www(host)

    owns_client = client is None
    http = client or make_resolver_client()
    try:
        # Headers + final URL after redirects are all we need; the body is
        # never relevant. ``head`` is the cheapest path; ~10% of outlets
        # answer 403/405 to HEAD (cloudflare-fronted, mostly) and need a
        # GET retry. We extract status + final URL inside the helper so
        # both branches return the same shape regardless of method used.
        status, final_url_str = _fetch_head_or_get(http, url)
        if status is None or status >= 400:
            return ""

        final_host = (urlparse(final_url_str).hostname or "").lower()
        if not final_host or final_host == _FINNHUB_REDIRECT_HOST:
            # Resolution didn't move us off the redirect host (e.g. a 200
            # response that didn't redirect, or a malformed response). Treat
            # as unresolved so the API/frontend fallback chain takes over.
            return ""
        return _strip_www(final_host)
    finally:
        if owns_client:
            http.close()


def _fetch_head_or_get(http: httpx.Client, url: str) -> tuple[int | None, str]:
    """Try HEAD first; fall back to a streamed GET if HEAD is rejected.

    Returns ``(status_code, final_url)`` on success or ``(None, "")`` on any
    transport-level failure. Uses ``http.stream("GET", ...)`` for the
    fallback — the context manager exits *without* reading the body, so we
    pay only the headers + redirect-chain cost, not the per-article HTML
    payload.

    Both HEAD and the GET fallback go through ``_sleep_for_finnhub_rate_limit``
    so the process stays under Finnhub's 60 RPM bucket (per QNT-148 follow-
    up: an uncapped 4 RPS burst dropped resolution success to 43% with the
    failed URLs returning ``Location: /``, the throttle signal).
    """
    _sleep_for_finnhub_rate_limit()
    try:
        response = http.head(url)
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("HEAD failed for %s: %s — retrying with GET", url, exc)
        response = None

    # 403/405/501 from HEAD → outlet rejects the method; retry with GET.
    # All other status codes (including 4xx/5xx from a real outlet) flow
    # through with whatever HEAD returned so the caller's `>= 400` guard
    # short-circuits without a second round-trip.
    if response is not None and response.status_code not in {403, 405, 501}:
        return response.status_code, str(response.url)

    _sleep_for_finnhub_rate_limit()
    try:
        # Stream context exits without consuming the body — `response.url`
        # is populated from headers, which is all the resolver needs.
        with http.stream("GET", url) as response_stream:
            return response_stream.status_code, str(response_stream.url)
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("GET fallback failed for %s: %s", url, exc)
        return None, ""
