"""Summary report — composes the technical / fundamental / news templates.

This is the "at a glance" surface the agent's summary tool reads. It does NOT
recompute anything; it delegates to the three source templates so the null/N/M
conventions and signal logic stay single-sourced.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import HTTPException
from shared.tickers import TICKER_METADATA, TICKERS

from api.templates.fundamental import build_fundamental_report
from api.templates.news import build_news_report
from api.templates.technical import build_technical_report


def _safe(build_fn: Callable[[str], str], ticker: str) -> str:
    """Render a sub-report, demoting 404s into an inline N/M block."""
    try:
        return build_fn(ticker)
    except HTTPException as exc:
        # Upstream section has no data yet — keep the summary well-formed.
        return f"N/M ({exc.detail})"


def build_summary_report(ticker: str) -> str:
    """Combined technical + fundamental + news report for ``ticker``."""
    if ticker not in TICKERS:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")

    meta = TICKER_METADATA.get(ticker, {})
    sections = [
        f"# SUMMARY REPORT — {ticker}",
        f"{meta.get('sector', 'Unknown sector')} / {meta.get('industry', 'Unknown industry')}",
        "",
        "---",
        "",
        _safe(build_technical_report, ticker),
        "",
        "---",
        "",
        _safe(build_fundamental_report, ticker),
        "",
        "---",
        "",
        _safe(build_news_report, ticker),
    ]
    return "\n".join(sections)
