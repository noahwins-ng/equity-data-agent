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
import os
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from shared.config import settings

from api.clickhouse import get_client
from api.routers import data_router, reports_router, tickers_router

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
            "git_sha": os.environ.get("GIT_SHA", "unknown"),
            "dagster_assets": assets,
            "dagster_checks": checks,
        },
    }


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001 — signature required by FastAPI
    """Warm the ClickHouse client + Dagster count cache on startup."""
    try:
        get_client().query("SELECT 1")
    except Exception:
        # /health will report the outage; don't block app startup
        logger.warning("ClickHouse unreachable at startup")
    _dagster_counts()  # cache the Dagster import cost before the first request
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

# CORS — allow local dev, Vercel preview deploys, and (future) prod domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3001"],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(reports_router)
app.include_router(data_router)
app.include_router(tickers_router)


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
