"""Company-logo endpoint — Finnhub /stock/profile2 logos, inlined as data URLs.

Sourced from the same vendor as ``equity_raw.news_raw`` (Finnhub free tier;
ADR-015) so no new vendor dependency. The frontend watchlist + quote header
render these alongside ticker symbols as a recognition aid; the canonical
universe is still ``shared.tickers.TICKERS``.

Why ``data:`` URLs instead of the Finnhub CDN URL:
    The browser would otherwise round-trip to ``static.finnhub.io`` per
    ``<img src>`` on first paint — for the watchlist that's 10 cross-origin
    fetches, and the quote-header logo flashes blank on each ticker
    navigation until its fetch lands. Inlining the bytes as base64 ``data:``
    URLs eliminates the browser hop entirely; the logos arrive with the
    JSON in a single response and render synchronously. The ~70-130 KB
    response (gzipped: ~50 KB) is amortised by the frontend's 24h Next
    Data Cache TTL — Finnhub sees roughly one fetch per ticker per pod.

Caching strategy:
    Logos basically never change for an established public company, so the
    in-process cache is keyed only on ticker — no TTL. We pre-warm the
    cache during ``app.lifespan`` startup (in a background thread so the
    API can serve other endpoints while logos load). A soft failure (no
    key, HTTP error, missing logo field, oversized image) caches ``None``
    so a bad ticker isn't retried per request; a pod restart is the
    explicit re-fetch trigger.

Concurrency:
    Reads of the cache are lockless — Python dict reads are GIL-safe and
    we never expose partial entries (a ticker is absent until its full
    data URL is computed, then assigned atomically). Populate operations
    serialize on ``_populate_lock`` so two threads that both see "missing"
    don't issue duplicate Finnhub calls. Crucially, the request route
    does NOT call populate inline — it waits on a ``threading.Event``
    set by the prewarm thread. That bound is what keeps an SSE/long-poll
    worker from being held hostage by a slow Finnhub during cold start.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter
from shared.config import settings
from shared.tickers import TICKERS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["logos"])

_FINNHUB_PROFILE2_URL = "https://finnhub.io/api/v1/stock/profile2"
# Logo bytes come from a Finnhub CDN host. Validate before we GET the URL
# returned by /stock/profile2 — the JSON value is technically attacker-
# controlled (a compromised or spoofed Finnhub response could redirect us
# to an internal endpoint), and the cost of a strict allowlist is one
# regex match per ticker per pod lifetime.
#
# QNT-163: Finnhub sharded the CDN; URLs now arrive at static2.finnhub.io
# (and presumably will at static3, static4, ... as load grows). The pattern
# accepts ``static.finnhub.io`` plus any ``staticN.finnhub.io`` for N >= 0,
# and rejects everything else (subdomain spoofing like
# ``static.finnhub.io.evil.com``, suffixes like ``static.finnhub.io2``,
# unrelated hosts). ``fullmatch`` anchors implicitly so a partial match
# can't slip through.
_FINNHUB_CDN_HOST_PATTERN = re.compile(r"^static\d*\.finnhub\.io$")
_REQUEST_TIMEOUT_SECONDS = 5.0
# Free-tier Finnhub allows 60 RPM on /stock/profile2; 1s spacing keeps us
# well below the limit even on cold start. The CDN host (static.finnhub.io)
# is unmetered so we don't space those out.
_INTER_PROFILE_REQUEST_SECONDS = 1.0
# Hard cap on a single decoded logo. QNT-163 raised this from 64 KB to
# 128 KB after observing JPM's Finnhub PNG at 83 KB — the original cap
# was set on a "Real Finnhub PNGs come in around 5-15 KB" assumption that
# turned out wrong for the larger-cap-icon brands (banks, healthcare).
# 128 KB still cleanly rejects misrouted assets (an HTML error page, a
# multi-MB stock photo) without trimming legitimate logos. Worst-case the
# 10-ticker JSON response grows to ~1.3 MB pre-gzip (~400 KB gzipped),
# amortised by the frontend's 24h Next Data Cache TTL.
_MAX_LOGO_BYTES = 128 * 1024
# Bounded wait the request handler is willing to spend on a still-warming
# cache. Pre-warm typically completes in <15s for 10 tickers; 30s is the
# soft ceiling that prevents a stuck Finnhub from indefinitely tying up a
# uvicorn worker. On timeout we return whatever's already cached and let
# the frontend render initials for the gaps.
_PREWARM_WAIT_TIMEOUT_SECONDS = 30.0

_logo_cache: dict[str, str | None] = {}
# Serializes populate operations only — readers do not acquire it.
_populate_lock = threading.Lock()
# Set by the prewarm thread when it finishes (success OR failure). The
# request handler waits on this so the first user request after a cold
# start sees a populated cache rather than partial Nones being baked into
# the frontend's 24h Data Cache.
_prewarm_done = threading.Event()


def _fetch_logo_data_url(ticker: str, client: httpx.Client) -> str | None:
    """Fetch the Finnhub logo for ``ticker`` and return a base64 data URL.

    Two-stage: first hits the rate-limited /stock/profile2 endpoint to
    discover the CDN URL, then GETs the bytes from static.finnhub.io.
    Soft-fails (returns None) on every error path: empty key, non-2xx,
    JSON decode failure, missing/empty ``logo`` field, host mismatch,
    oversized payload. The frontend renders initials when None — there
    is no caller that depends on knowing why.
    """
    try:
        profile = client.get(
            _FINNHUB_PROFILE2_URL,
            params={"symbol": ticker, "token": settings.FINNHUB_API_KEY},
        )
        profile.raise_for_status()
        payload = profile.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("finnhub profile2 fetch failed for %s: %s", ticker, exc)
        return None
    if not isinstance(payload, dict):
        return None
    url = payload.get("logo")
    if not isinstance(url, str) or not url:
        return None

    # Pin the host before issuing the second GET — Finnhub's JSON is the
    # only thing telling us where to fetch the bytes, and a bad value
    # would otherwise let us reach arbitrary network destinations.
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if parsed.scheme != "https" or not _FINNHUB_CDN_HOST_PATTERN.fullmatch(hostname):
        logger.warning("finnhub logo URL host mismatch for %s: %r", ticker, parsed.hostname)
        return None

    try:
        image = client.get(url)
        image.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("finnhub logo bytes fetch failed for %s: %s", ticker, exc)
        return None
    body = image.content
    if not body or len(body) > _MAX_LOGO_BYTES:
        return None
    content_type = image.headers.get("content-type", "image/png").split(";")[0].strip()
    if not content_type.startswith("image/"):
        content_type = "image/png"
    encoded = base64.b64encode(body).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def _populate_missing(missing: list[str]) -> None:
    """Fetch ``missing`` tickers and store their results.

    Serializes on ``_populate_lock`` so two threads that both see "missing"
    don't issue duplicate Finnhub calls. Crucially, the global cache lock
    is NOT held during HTTP I/O — readers can hit ``_logo_cache`` without
    blocking on us, and dict writes are GIL-safe. Re-checks under the
    lock so a thread that waited for the lock skips tickers another
    thread already populated.
    """
    if not missing:
        return
    with _populate_lock:
        still_missing = [t for t in missing if t not in _logo_cache]
        if not still_missing:
            return
        if not settings.FINNHUB_API_KEY:
            # Cache None for all so we don't retry per request when the key
            # is absent (dev without secrets, prod misconfiguration).
            for ticker in still_missing:
                _logo_cache[ticker] = None
            return
        with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            for idx, ticker in enumerate(still_missing):
                if idx > 0:
                    time.sleep(_INTER_PROFILE_REQUEST_SECONDS)
                _logo_cache[ticker] = _fetch_logo_data_url(ticker, client)


def prewarm_logo_cache() -> None:
    """Populate the cache for the configured ticker universe.

    Intended to run in a background thread from FastAPI's ``lifespan`` so
    boot doesn't block on ~10s of Finnhub round-trips. Sets
    ``_prewarm_done`` when finished (success OR failure) so the request
    handler stops waiting. Idempotent — already-cached tickers are
    skipped under the populate lock.
    """
    try:
        missing = [t for t in TICKERS if t not in _logo_cache]
        _populate_missing(missing)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("logo cache prewarm failed: %s", exc)
    finally:
        _prewarm_done.set()


@router.get("/logos")
def get_logos() -> dict[str, str | None]:
    """Return ``{ticker: data_url | null}`` for the configured ticker universe.

    Waits up to ``_PREWARM_WAIT_TIMEOUT_SECONDS`` for the startup pre-warm
    thread to populate the cache, then reads it lockless. Multiple
    concurrent requests share the same Event without serializing — the
    handler does NOT issue Finnhub calls inline, so a slow vendor never
    holds a uvicorn worker beyond the bounded wait.

    Tickers absent from the cache after the wait return ``None``; the
    frontend renders initials for those rows. The frontend caches the
    response for 24h via Next.js Data Cache, so Finnhub sees ~one fetch
    per ticker per pod lifetime.
    """
    _prewarm_done.wait(timeout=_PREWARM_WAIT_TIMEOUT_SECONDS)
    return {t: _logo_cache.get(t) for t in TICKERS}
