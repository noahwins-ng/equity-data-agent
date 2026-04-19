"""Ticker registry endpoint — the source of truth for the frontend selector.

Returns the in-process ``shared.tickers.TICKERS`` list so the frontend never
hardcodes the universe. When a ticker is added to ``shared/tickers.py`` it
propagates everywhere (Dagster partitions, API validators, frontend selector)
on the next deploy.
"""

from __future__ import annotations

from fastapi import APIRouter
from shared.tickers import TICKERS

router = APIRouter(prefix="/api/v1", tags=["tickers"])


@router.get("/tickers")
def list_tickers() -> list[str]:
    return list(TICKERS)
