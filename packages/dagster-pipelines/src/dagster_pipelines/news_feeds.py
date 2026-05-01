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
from datetime import date
from typing import Any
from urllib.parse import urlparse

import httpx
from shared.config import settings

logger = logging.getLogger(__name__)

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
COMPANY_NEWS_PATH = "/company-news"

# Per-call timeout. Finnhub typically responds in <1s; 30s is generous so a
# transient slowdown doesn't false-fail the asset. Retries are owned by the
# Dagster RetryPolicy on news_raw, not the HTTP client.
_REQUEST_TIMEOUT_SECONDS = 30.0

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


class FinnhubAPIKeyMissing(RuntimeError):
    """Raised when FINNHUB_API_KEY is empty. Surfaced to the asset so the
    failure mode is "loud and obvious" rather than "silent empty insert"."""


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
        httpx.HTTPStatusError: For non-2xx responses (4xx auth, 429 rate limit, 5xx).
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
        response = http.get(f"{FINNHUB_BASE_URL}{COMPANY_NEWS_PATH}", params=params)
        response.raise_for_status()
        payload = response.json()
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
    """
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

    try:
        # Stream context exits without consuming the body — `response.url`
        # is populated from headers, which is all the resolver needs.
        with http.stream("GET", url) as response_stream:
            return response_stream.status_code, str(response_stream.url)
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("GET fallback failed for %s: %s", url, exc)
        return None, ""
