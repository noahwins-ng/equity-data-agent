"""FastAPI application entry point.

Exposes:
- ``/api/v1/reports/*``  — text reports for the LangGraph agent
- ``/api/v1/health``     — service connectivity + deploy identity (QNT-51)
- ``/health``            — legacy alias, identical payload, kept for prod monitoring
- ``/docs``, ``/openapi.json`` — OpenAPI documentation

The rich ``/health`` payload surfaces runtime identity (git SHA + Dagster asset/
check counts) so external monitoring can distinguish "API is up" from "API is
running the code we think it is" — the Apr-16 silent-stale-deploy failure mode
that QNT-88/89 addressed at the CD layer. This ticket moves the same signal
into the runtime surface.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from shared.config import settings
from slowapi.errors import RateLimitExceeded

from api.clickhouse import get_client
from api.routers import (
    agent_chat_router,
    data_router,
    logos_router,
    reports_router,
    search_router,
    tickers_router,
)
from api.routers.logos import prewarm_logo_cache
from api.security import (
    client_ip,
    cors_allow_origin_regex,
    cors_allow_origins,
    limiter,
    record_burst_alert,
    sentry_capture_exception,
)

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _dagster_counts() -> tuple[int, int]:
    """Return (assets, asset_checks) registered in the Dagster definitions module.

    Lazy + cached — first call resolves the asset graph, subsequent calls are free.
    Falls back to (0, 0) if the dagster-pipelines package isn't importable in
    this container (e.g. a future minimal api-only image). A failure here must
    not take ``/health`` down.
    """
    try:
        from dagster_pipelines.definitions import defs  # type: ignore[import-not-found]

        ag = defs.resolve_asset_graph()
        return len(ag.get_all_asset_keys()), len(list(ag.asset_check_keys))
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("dagster counts unavailable: %s", exc)
        return 0, 0


@lru_cache(maxsize=1)
def _ohlcv_schedule_cron_tz() -> tuple[str, str] | None:
    """Resolve (cron, timezone) for ``ohlcv_daily_schedule`` once per process.

    The cron string is constant after deploy, so cache it; only the per-call
    "next firing" math runs each request. Returns None on any failure so the
    caller can fall back to ``settings.PROVENANCE_NEXT_INGEST_FALLBACK``.
    """
    try:
        from dagster_pipelines.definitions import defs  # type: ignore[import-not-found]

        sched = defs.get_schedule_def("ohlcv_daily_schedule")
        cron = sched.cron_schedule
        tz = sched.execution_timezone
        if not isinstance(cron, str) or not tz:
            return None
        return (cron, tz)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("ohlcv schedule introspection failed: %s", exc)
        return None


# Stable user-facing timezone labels. Financial convention is the DST-agnostic
# two-letter code ("ET", "PT", "CT") rather than ``strftime('%Z')`` — the latter
# flips between EDT/EST twice a year and would make the strip flap. Anything
# outside the mapping falls back to ``%Z`` so the suffix still tracks the live
# tz, which is what AC-1 (single source of truth) requires.
_TZ_ABBREV = {
    "America/New_York": "ET",
    "America/Chicago": "CT",
    "America/Denver": "MT",
    "America/Los_Angeles": "PT",
}


def _next_ingest_local() -> str:
    """Format the next firing of ``ohlcv_daily_schedule`` as ``HH:MM <TZ>``.

    The strip surfaces the user-facing recurring time; the cron emits the same
    ``HH:MM`` every day so a time-of-day slice carries the same information as
    a full timestamp without locale-specific date formatting on the frontend.
    The tz suffix is derived from the schedule's ``execution_timezone`` so a
    cron / tz change in ``schedules.py`` propagates without an API code edit.
    On any failure (introspection, croniter, tz lookup) we surface the static
    fallback from settings — provenance must never take /health down.
    """
    pair = _ohlcv_schedule_cron_tz()
    if pair is None:
        return settings.PROVENANCE_NEXT_INGEST_FALLBACK
    cron, tz = pair
    try:
        from dagster._utils.schedules import (  # type: ignore[import-not-found]
            cron_string_iterator,
        )

        now = datetime.now(ZoneInfo(tz))
        next_dt = next(cron_string_iterator(now.timestamp(), cron, tz))
        abbrev = _TZ_ABBREV.get(tz) or next_dt.strftime("%Z") or tz
        return f"{next_dt.strftime('%H:%M')} {abbrev}"
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("next_ingest_local computation failed: %s", exc)
        return settings.PROVENANCE_NEXT_INGEST_FALLBACK


def _provenance() -> dict[str, Any]:
    """Subsystem provenance for the Phase 6 data-driven UI strip (QNT-132).

    SOURCES + JOBS only — SENTIMENT row dropped post-QNT-131 deferral, AGENT
    row dropped as a hardcoded constant. Field shape stays forward-compatible
    so reviving QNT-131 just adds a ``sentiment`` key here.
    """
    return {
        "sources": list(settings.PROVENANCE_SOURCES),
        "jobs": {
            "runtime": "Dagster",
            "schedule": "daily",
            "next_ingest_local": _next_ingest_local(),
        },
    }


def _check_clickhouse() -> str:
    try:
        get_client().query("SELECT 1")
        return "ok"
    except Exception:
        return "down"


def _check_qdrant() -> str:
    """Probe Qdrant Cloud if credentials are configured, else report 'down'.

    Qdrant is optional until Phase 4 ships news embeddings. Absent credentials
    report as 'down' so the overall status degrades transparently rather than
    masquerading as healthy.
    """
    if not settings.QDRANT_URL or not settings.QDRANT_API_KEY:
        return "down"
    try:
        from qdrant_client import QdrantClient

        QdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY,
            timeout=3,
        ).get_collections()
        return "ok"
    except Exception:
        return "down"


def _health_payload(response: Response) -> dict[str, Any]:
    clickhouse = _check_clickhouse()
    qdrant = _check_qdrant()
    assets, checks = _dagster_counts()

    if clickhouse == "down":
        status = "down"
        response.status_code = 503
    elif qdrant == "down":
        status = "degraded"
    else:
        status = "ok"

    return {
        "status": status,
        "services": {"clickhouse": clickhouse, "qdrant": qdrant},
        "deploy": {
            "git_sha": settings.GIT_SHA or "unknown",
            "dagster_assets": assets,
            "dagster_checks": checks,
        },
        "provenance": _provenance(),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001 — signature required by FastAPI
    """Warm the ClickHouse client + Dagster count cache + logo cache on startup."""
    try:
        get_client().query("SELECT 1")
    except Exception:
        # /health will report the outage; don't block app startup
        logger.warning("ClickHouse unreachable at startup")
    _dagster_counts()  # cache the Dagster import cost before the first request
    # Pre-warm the logo cache in a daemon thread so the first /api/v1/logos
    # request lands on a populated cache. Daemon=True so a stuck Finnhub
    # call can't block process shutdown — the request path falls back to
    # an inline fetch if the thread hasn't finished by then.
    threading.Thread(target=prewarm_logo_cache, daemon=True).start()
    yield
    get_client.cache_clear()


app = FastAPI(
    title="Equity Data Agent API",
    description=(
        "FastAPI serving pre-computed indicators as JSON for the frontend and "
        "human-readable reports as text/plain for the LangGraph agent."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# QNT-86: Sentry init guarded by SENTRY_DSN. Builds on the QNT-161 hook
# scaffolding (record_burst_alert, record_breaker_trip in api.security) by
# adding release tagging via GIT_SHA, a starter performance sample rate,
# session tracking for release health, and explicit PII scrubbing. The
# FastAPI integration auto-installs from sentry-sdk[fastapi] — unhandled
# exceptions are captured before our cors_aware_exception_handler converts
# them to 500 responses. Init failures are tolerated: a Sentry outage
# must not block the API.
if settings.SENTRY_DSN:
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.ENV,
            release=settings.GIT_SHA or None,
            traces_sample_rate=0.1,
            auto_session_tracking=True,
            send_default_pii=False,
        )
    except Exception as exc:  # noqa: BLE001 — Sentry init must not block startup
        logger.warning("sentry init failed (continuing without): %s", exc)

# QNT-161: SlowAPI rate limiter — applied per-route via @limiter.limit on the
# chat endpoint. ``state.limiter`` and the RateLimitExceeded handler are the
# integration shape SlowAPI documents; the handler here both serves the 429
# and records the event for the burst alerter.
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_exceeded_handler(
    request: Request,
    exc: RateLimitExceeded,
) -> JSONResponse:
    """QNT-161: 429 handler with Retry-After + burst-alert hook.

    The detail string is intentionally friendly — the chat panel reads the
    body and surfaces a "demo limit" card that points the user to the repo.
    Retry-After is sourced from the SlowAPI exception (in seconds) so the
    panel can display "try again in N seconds".
    """
    record_burst_alert(client_ip(request), time.monotonic())
    retry_after = str(int(getattr(exc, "retry_after", 60) or 60))
    return JSONResponse(
        status_code=429,
        content={
            "detail": (
                "You've hit the demo rate limit for this IP. This portfolio "
                "demo runs on a free LLM tier; the limit protects daily uptime "
                "for other visitors. Try again in a moment, or fork the repo "
                "to run the agent against your own keys."
            ),
            "code": "rate-limited",
            "retry_after": retry_after,
        },
        headers={"Retry-After": retry_after},
    )


# QNT-161: CORS — explicit allowlist + project-pinned Vercel preview regex.
# Defaults are dev-only (localhost:3001); prod sets CORS_ALLOWED_ORIGINS and
# CORS_ALLOWED_ORIGIN_REGEX so leaked Vercel previews for unrelated projects
# can't drive traffic to this API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(cors_allow_origins()),
    allow_origin_regex=cors_allow_origin_regex(),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(reports_router)
app.include_router(data_router)
app.include_router(search_router)
app.include_router(tickers_router)
app.include_router(logos_router)
app.include_router(agent_chat_router)


@app.exception_handler(Exception)
async def cors_aware_exception_handler(
    request: Request,
    _exc: Exception,
) -> JSONResponse:
    """Catch otherwise-unhandled exceptions so the 500 response still carries
    CORS headers.

    Starlette's ``ServerErrorMiddleware`` wraps the *entire* user middleware
    stack — including ``CORSMiddleware`` — so an exception that bubbles past
    ``ExceptionMiddleware`` produces a 500 with no ``Access-Control-Allow-
    Origin``. The browser then surfaces the 500 as a misleading
    "blocked by CORS policy" error and the actual root-cause stack is hidden.

    Registering this handler at the app level moves the conversion *inside*
    ``ExceptionMiddleware`` so the CORS layer still adds its headers on the
    way out. The error body is intentionally generic — the underlying
    exception is logged by Starlette before this fires.

    QNT-86: explicitly forward the exception to Sentry. The FastAPI
    integration auto-captures unhandled exceptions before user handlers
    run, but registering a handler for ``Exception`` can short-circuit
    that path on some sentry-sdk paths (the integration treats a handled
    exception as "expected"); explicit capture is the documented
    workaround. Sentry deduplicates by stack-trace fingerprint so a
    redundant call is harmless. No-op when ``SENTRY_DSN`` is unset.
    """
    logger.exception("unhandled exception on %s %s", request.method, request.url.path)
    sentry_capture_exception(_exc)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


@app.api_route("/api/v1/health", methods=["GET", "HEAD"], tags=["health"])
def health(response: Response) -> dict[str, Any]:
    """Service + deploy-identity health check.

    - ``status: "ok"``       — ClickHouse and Qdrant both reachable
    - ``status: "degraded"`` — ClickHouse up, Qdrant down (HTTP 200)
    - ``status: "down"``     — ClickHouse unreachable (HTTP 503)

    GET returns the full JSON payload; HEAD returns headers + status code only
    (Starlette strips the body on HEAD). HEAD is required by free-tier uptime
    probes like UptimeRobot that only support HEAD requests — see QNT-106.
    """
    return _health_payload(response)


@app.api_route("/health", methods=["GET", "HEAD"], include_in_schema=False)
def health_legacy(response: Response) -> dict[str, Any]:
    """Legacy path kept alive for prod monitoring (`scripts/health-monitor.sh`,
    `deploy.yml` verify-deploy step, `make check-prod`). Same payload as
    /api/v1/health — consumers checking only HTTP status continue to work.
    """
    return _health_payload(response)


@app.get("/api/v1/_debug/sentry", include_in_schema=False)
def debug_sentry() -> None:
    """QNT-86: force a synthetic exception so Sentry receives a verification
    event with full stack trace + request URL + git_sha release tag.

    Gated in prod: returns 404 unless ``ENABLE_SENTRY_TEST=1`` is set in the
    environment. Without the gate a scraper hitting this path in a loop
    would burn the Sentry monthly quota (free tier: 5k errors/mo). In dev
    the route always raises so a developer can verify wiring locally.
    """
    if settings.is_prod and not settings.ENABLE_SENTRY_TEST:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Not Found")
    raise RuntimeError("QNT-86 Sentry verification — synthetic exception")
