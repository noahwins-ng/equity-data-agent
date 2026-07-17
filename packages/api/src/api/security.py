"""Public-chat abuse prevention (QNT-161).

Three orthogonal controls layered on ``POST /api/v1/agent/chat``:

1. **Per-IP request rate limit** (``slowapi``) — protects against
   request-count flooding. Defaults: ``5/minute, 20/day``. Hitting
   any tier returns HTTP 429 with ``Retry-After``; the chat panel surfaces
   the friendly limit message.
2. **Per-IP daily token budget** — soft cap orthogonal to request
   count. A chatty user can stay under the request cap but still burn a large
   token spend by triggering many tool runs. Default ~280K tokens/IP/day —
   sized so the 20/day request cap binds first. Once exceeded,
   ``can_serve_request()`` returns ``False`` and the SSE handler
   short-circuits to a deterministic conversational redirect.
3. **Global daily token circuit breaker** — QNT-258 / ADR-025: with the
   paid launch primary (DeepSeek via OpenRouter) there is no free-tier TPD
   to proxy, so this is now a runaway-cost / abuse breaker sized in paid
   economics (see ``CHAT_TOKENS_GLOBAL_PER_DAY``). Defends against the
   rotating-IP / many-IPs-each-just-under long-tail. **FAILS CLOSED** —
   when the breaker trips, the agent never reaches the LLM, so a stuck
   loop or scraper cannot run cost past the daily ceiling.

Storage is in-memory (``threading.Lock``-guarded), keyed by IP, with a
UTC-midnight daily reset. Acceptable on the
single-host Hetzner deploy; a future multi-host topology would need to
swap the backend for Redis behind the same interface.

Token spend is recorded by a LangChain callback (``TokenUsageCallback``)
that the agent's ``get_llm()`` reads from a context-local — see
``agent.llm.set_token_callback`` for the threading model.

Sentry alerting (burst pattern + breaker trip) is plumbed via
``record_burst_alert`` / ``record_breaker_trip``; both fall back to a
``logger.warning`` line when ``SENTRY_DSN`` is unset (QNT-86 will complete
the Sentry wiring — this module exposes the hooks ahead of that).
"""

from __future__ import annotations

import logging
import re
import threading
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from shared.config import settings
from slowapi import Limiter

if TYPE_CHECKING:
    from collections.abc import Iterable

    from fastapi import Request

logger = logging.getLogger(__name__)

SentryLevel = Literal["fatal", "critical", "error", "warning", "info", "debug"]


# ─── SlowAPI rate limiter ───────────────────────────────────────────────────


def client_ip(request: Request) -> str:
    """Extract the client IP for rate-limiting and per-IP token budgeting.

    Ingress reality (ADR-018): the only public path to the API is the
    Cloudflare **named tunnel** — ``api.nusaverde.com`` → the Cloudflare edge
    → the ``cloudflared`` connector → ``api:8000`` over the Docker bridge.
    uvicorn binds to loopback; there is no public :8000 (and SSH on port 22 is
    the only other open port — never a request path here). Every real request
    therefore arrives having transited the Cloudflare edge.

    Header trust model:

    - ``CF-Connecting-IP`` is *set by the Cloudflare edge* to the real
      connecting IP and OVERWRITES any client-supplied value, so a caller
      cannot spoof it through the tunnel. This is the authoritative source and
      is preferred whenever present.
    - ``X-Forwarded-For`` is only partly trustworthy: a caller can prepend
      arbitrary entries, but the Cloudflare edge APPENDS the real connecting IP
      as the RIGHT-MOST entry. So we read the right-most entry, never the
      left-most (the left-most is attacker-controlled). Fallback for the
      unexpected case where ``CF-Connecting-IP`` is absent.
    - ``request.client.host`` is the direct peer. In dev there is no Cloudflare
      in front, so no CF headers are present and we fall through to this
      (``127.0.0.1`` / ``testclient``). In prod it is the ``cloudflared``
      container's Docker IP and is used only if both headers are missing.

    Why not the left-most XFF entry (the pre-QNT-380 behaviour): keying on the
    left-most entry let anyone send ``X-Forwarded-For: <random>`` through the
    tunnel and mint a fresh per-IP rate-limit / token-budget bucket on every
    request, collapsing per-IP fairness to the global breaker alone.
    """
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip and cf_ip.strip():
        return cf_ip.strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        entries = [entry.strip() for entry in fwd.split(",") if entry.strip()]
        if entries:
            return entries[-1]
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


# Module-level limiter instance shared by the FastAPI app + the chat router.
# Strategy "moving-window" approximates a true sliding-window counter and
# avoids the bucket-edge spikes a fixed-window strategy would allow at minute
# / hour boundaries.
limiter = Limiter(
    key_func=client_ip,
    default_limits=[],  # default_limits apply to ALL routes; opt-in per route
    strategy="moving-window",
)


# ─── In-memory token budgets ────────────────────────────────────────────────


def _today_utc() -> str:
    """ISO date for the current UTC day. Stable string is the dict key for
    the per-day reset — switching dates clears the per-IP + global counters
    in one atomic comparison."""
    return datetime.now(UTC).date().isoformat()


class TokenBudget:
    """Per-IP + global daily token spend tracker.

    Two counters share one daily reset boundary:

    - ``per_ip[ip]`` — tokens spent by ``ip`` today
    - ``global_total`` — tokens spent across all IPs today

    ``can_serve(ip)`` returns ``(allowed, reason)`` where ``reason`` is one of
    ``"per_ip"`` / ``"global"`` / ``None``. ``record(ip, tokens)`` adds spend
    after the agent run completes; we use a record-after-the-fact model so
    short requests don't get pre-debited optimistically.

    Thread-safe via a single coarse lock — the per-request work here is a
    handful of dict ops, contention is irrelevant at chat-panel volume.
    """

    def __init__(
        self,
        per_ip_daily: int,
        global_daily: int,
    ) -> None:
        self._per_ip_daily = per_ip_daily
        self._global_daily = global_daily
        self._lock = threading.Lock()
        self._date = _today_utc()
        self._per_ip: dict[str, int] = defaultdict(int)
        self._global = 0

    def _maybe_reset_locked(self) -> None:
        """Reset both counters when the UTC day rolls. MUST be called with the
        lock held — both readers and writers share this code path."""
        today = _today_utc()
        if today != self._date:
            self._date = today
            self._per_ip = defaultdict(int)
            self._global = 0

    def can_serve(self, ip: str) -> tuple[bool, str | None]:
        """Return ``(allowed, reason)``. ``reason`` is the short label used by
        the SSE handler to pick the right redirect copy."""
        with self._lock:
            self._maybe_reset_locked()
            if self._global >= self._global_daily:
                return False, "global"
            if self._per_ip[ip] >= self._per_ip_daily:
                return False, "per_ip"
            return True, None

    def record(self, ip: str, tokens: int) -> None:
        """Charge ``tokens`` to both the per-IP and global counters.

        Tokens may be 0 (the LangChain callback couldn't extract usage from
        the LiteLLM proxy response — known to happen when the proxy strips
        the ``usage`` block). We still call ``record`` so a future debug
        hook can see the request landed; the counters just don't advance.
        """
        if tokens <= 0:
            return
        with self._lock:
            self._maybe_reset_locked()
            self._per_ip[ip] += tokens
            self._global += tokens

    def snapshot(self) -> dict[str, int]:
        """Return a point-in-time view for tests and a future ops endpoint."""
        with self._lock:
            self._maybe_reset_locked()
            return {
                "global": self._global,
                "global_cap": self._global_daily,
                "per_ip_cap": self._per_ip_daily,
                "tracked_ips": len(self._per_ip),
            }

    def reset(self) -> None:
        """Test helper — clears all state without waiting for the UTC roll."""
        with self._lock:
            self._date = _today_utc()
            self._per_ip = defaultdict(int)
            self._global = 0


budget = TokenBudget(
    per_ip_daily=settings.CHAT_TOKENS_PER_IP_PER_DAY,
    global_daily=settings.CHAT_TOKENS_GLOBAL_PER_DAY,
)


# ─── Burst alerter (Sentry hook) ────────────────────────────────────────────


class BurstAlerter:
    """Track 429s per IP within a sliding window and fire a Sentry alert
    once the threshold is crossed.

    Windowed dedup: each IP gets one alert per window duration so a sustained
    burst doesn't produce a stream of duplicate Sentry events. The window
    size + threshold come from ``settings`` — defaults are tuned so a
    frustrated recruiter retrying twice doesn't trip but a scraper does.
    """

    def __init__(self, threshold: int, window_seconds: int) -> None:
        self._threshold = threshold
        self._window = window_seconds
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._last_alerted: dict[str, float] = {}

    def record_429(self, ip: str, now_monotonic: float) -> bool:
        """Return True if this 429 crossed the burst threshold (caller fires
        the Sentry message). False otherwise — including the dedup case where
        the same IP already alerted within this window."""
        cutoff = now_monotonic - self._window
        with self._lock:
            events = self._events[ip]
            while events and events[0] < cutoff:
                events.popleft()
            events.append(now_monotonic)
            if len(events) < self._threshold:
                return False
            last = self._last_alerted.get(ip)
            if last is not None and now_monotonic - last < self._window:
                return False
            self._last_alerted[ip] = now_monotonic
            return True

    def reset(self) -> None:
        """Test helper."""
        with self._lock:
            self._events.clear()
            self._last_alerted.clear()


burst_alerter = BurstAlerter(
    threshold=settings.CHAT_BURST_THRESHOLD,
    window_seconds=settings.CHAT_BURST_WINDOW_SECONDS,
)


def _sentry_capture(message: str, level: SentryLevel = "warning") -> None:
    """Best-effort Sentry alert. Falls back to ``logger.warning`` when the
    SDK is missing or ``SENTRY_DSN`` is unset — the hook surfaces alerts
    the moment Sentry init lands (QNT-86 completed the wiring; this hook
    is the surface the burst / breaker callsites already use).
    """
    if not settings.SENTRY_DSN:
        logger.warning("[burst-alert] %s", message)
        return
    try:
        import sentry_sdk

        sentry_sdk.capture_message(message, level=level)
    except Exception as exc:  # noqa: BLE001 — alerting must never crash the request
        logger.warning("[burst-alert] sentry capture failed (%s): %s", exc, message)


def sentry_capture_exception(exc: BaseException) -> None:
    """Best-effort Sentry exception capture (QNT-86).

    Used by the chat SSE error paths to surface graph crashes that happen
    in a worker thread (where Sentry's auto-capture middleware can't see
    them) and by the global exception handler as defensive insurance.

    No-op when ``SENTRY_DSN`` is unset — the caller already logs via
    ``logger.exception`` so dev runs aren't silent. Sentry deduplicates by
    stack-trace fingerprint, so a double-fire when both this hook and the
    FastAPI integration's auto-capture run is harmless.
    """
    if not settings.SENTRY_DSN:
        return
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception as inner:  # noqa: BLE001 — capture must never crash the request
        logger.warning("sentry exception capture failed (%s)", inner)


def record_burst_alert(ip: str, now_monotonic: float) -> None:
    """Public hook called from the SlowAPI 429 handler. Fires Sentry only
    when the burst threshold is crossed AND we haven't alerted this IP
    within the window."""
    if burst_alerter.record_429(ip, now_monotonic):
        _sentry_capture(
            f"chat-burst: ip={ip} >={settings.CHAT_BURST_THRESHOLD} 429s in "
            f"{settings.CHAT_BURST_WINDOW_SECONDS}s",
            level="warning",
        )


def record_breaker_trip(reason: str) -> None:
    """Public hook called when the global TPD breaker first trips this day.
    Always fires (no dedup); the SSE handler is expected to call this only
    once per breaker transition, not once per request after the trip."""
    _sentry_capture(
        f"chat-breaker-tripped: reason={reason} ; the daily Groq TPD ceiling "
        f"is exhausted, all chat requests now serve the demo-limit redirect",
        level="error",
    )


# ─── Prompt-injection input filter ──────────────────────────────────────────


# Allowlist: visible printable ASCII + common Unicode + newline + tab. The
# 4000-char message cap (chat router) handles bulk; this filter handles the
# narrow class of inputs that pass length but carry shell control chars,
# zero-width joins, or overlong-token exfil patterns.
#
# Reject characters in the C0 control range (U+0000–U+001F) EXCEPT \n (0x0A)
# and \t (0x09). Also reject DEL (0x7F) and the C1 control range. We do NOT
# reject all non-ASCII — international tickers and quoted news headlines may
# legitimately contain non-Latin characters.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]")

# Overlong identifier: any contiguous run of non-whitespace longer than 500
# chars. Real questions don't carry tokens that long; payloads encoding
# secrets, URLs with embedded args, or base64 chunks routinely do.
_OVERLONG_TOKEN = re.compile(r"\S{501,}")


def validate_chat_message(message: str) -> str:
    """Return ``message`` if it passes the input filter, raise ``ValueError``
    otherwise. Pydantic ``field_validator`` calls this; FastAPI converts the
    ``ValueError`` into a 422 response with the message text in the detail.
    """
    if _CONTROL_CHARS.search(message):
        raise ValueError(
            "message contains control characters; only printable text plus newline / tab is allowed"
        )
    if _OVERLONG_TOKEN.search(message):
        raise ValueError("message contains an overlong token (>500 chars)")
    return message


# ─── CORS origins (resolved from settings) ──────────────────────────────────


def cors_allow_origins() -> Iterable[str]:
    """Iterable of fixed CORS origins. Stripping empties tolerates the env-var
    quirk where ``CORS_ALLOWED_ORIGINS=`` (unset) parses as a single empty
    string list element on some pydantic-settings versions."""
    return [o for o in settings.CORS_ALLOWED_ORIGINS if o]


def cors_allow_origin_regex() -> str | None:
    """Project-pinned Vercel preview regex, or None if not configured."""
    pattern = settings.CORS_ALLOWED_ORIGIN_REGEX.strip()
    return pattern or None


__all__ = [
    "BurstAlerter",
    "TokenBudget",
    "budget",
    "burst_alerter",
    "client_ip",
    "cors_allow_origin_regex",
    "cors_allow_origins",
    "limiter",
    "record_breaker_trip",
    "record_burst_alert",
    "sentry_capture_exception",
    "validate_chat_message",
]
