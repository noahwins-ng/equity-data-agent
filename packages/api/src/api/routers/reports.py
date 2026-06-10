"""Report endpoints — text strings consumed by the LangGraph agent."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from api.comparison_metrics import (
    _MIN_TICKERS,
    ComparisonMetricsResponse,
    _resolve_tickers,
    build_comparison_metrics,
)
from api.templates import (
    build_company_report,
    build_fundamental_report,
    build_news_report,
    build_summary_report,
    build_technical_report,
)

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


@router.get("/technical/{ticker}", response_class=PlainTextResponse)
def technical_report(ticker: str) -> str:
    return build_technical_report(ticker.upper())


@router.get("/fundamental/{ticker}", response_class=PlainTextResponse)
def fundamental_report(ticker: str) -> str:
    return build_fundamental_report(ticker.upper())


@router.get("/news/{ticker}", response_class=PlainTextResponse)
def news_report(ticker: str) -> str:
    return build_news_report(ticker.upper())


@router.get("/summary/{ticker}", response_class=PlainTextResponse)
def summary_report(ticker: str) -> str:
    return build_summary_report(ticker.upper())


@router.get("/company/{ticker}", response_class=PlainTextResponse)
def company_report(ticker: str, profile: Literal["full", "compact"] = "full") -> str:
    # QNT-220 (#8): ``?profile=compact`` trims the static prose for the agent
    # thesis/comparison hot path; ``full`` (default) keeps the complete profile.
    # QNT-224: ``profile`` tightened from ``str`` to a Literal so the allowed
    # values surface in the OpenAPI schema / generated TS types; FastAPI now
    # 422s a bad value instead of relying on build_company_report's 400.
    return build_company_report(ticker.upper(), profile)


@router.get("/comparison-metrics", response_model=ComparisonMetricsResponse)
def comparison_metrics(tickers: str) -> ComparisonMetricsResponse:
    """QNT-224: lean N-way comparison — one compact metrics row per ticker.

    ``tickers`` is a comma-separated list (``?tickers=AAPL,MSFT,GOOGL``).
    Unknown symbols are dropped and the list is capped at four; fewer than two
    valid tickers is a 400 (a comparison needs at least a pair). This is the
    cheap 3-4 way path — the rich two-ticker comparison keeps its full bundle.
    """
    resolved = _resolve_tickers(tickers)
    if len(resolved) < _MIN_TICKERS:
        raise HTTPException(
            status_code=400,
            detail="comparison-metrics needs at least two known tickers",
        )
    return build_comparison_metrics(resolved)
