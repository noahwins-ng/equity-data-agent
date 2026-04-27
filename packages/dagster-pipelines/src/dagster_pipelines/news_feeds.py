"""Finnhub /company-news client for the news_raw asset (QNT-141, per ADR-015).

Replaces the prior Yahoo Finance RSS surface. Finnhub gives per-publisher
attribution + article images + a 1y historical window — none of which RSS
returned. ADR-015 documents the source pick + alternatives considered.

Free tier (verified premium:null in the docs JSON, 2026-04-27): 60 req/min,
1y historical backfill via the from/to query params. Empty FINNHUB_API_KEY
makes ``fetch_company_news`` raise — the asset surfaces this rather than
silently degrading, since topology (a) (ADR-015 §Decision) needs real rows
to drive the downstream classifier.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx
from shared.config import settings

logger = logging.getLogger(__name__)

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
COMPANY_NEWS_PATH = "/company-news"

# Per-call timeout. Finnhub typically responds in <1s; 30s is generous so a
# transient slowdown doesn't false-fail the asset. Retries are owned by the
# Dagster RetryPolicy on news_raw, not the HTTP client.
_REQUEST_TIMEOUT_SECONDS = 30.0


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
