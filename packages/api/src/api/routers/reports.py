"""Report endpoints — text strings consumed by the LangGraph agent."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from api.templates import (
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
