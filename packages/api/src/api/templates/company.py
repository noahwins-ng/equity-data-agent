"""Company knowledge report — static business profile, no DB query.

Every other report template in this package pulls from ClickHouse; this one
intentionally does not. ``TICKER_METADATA`` already encodes the editorial
context (description, key competitors, key risks, watch-metrics) the agent
needs to ground a thesis in the company's actual business — fetching it from
SQL would just round-trip a literal. Keeping it static also means the company
tool stays available when the warehouse tunnel is down (the only static-only
report in ``default_report_tools``).
"""

from __future__ import annotations

from typing import cast

from fastapi import HTTPException
from shared.tickers import TICKER_METADATA, TICKERS


def _format_bullets(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items] if items else ["- (none recorded)"]


def build_company_report(ticker: str) -> str:
    """Build a static business-context report for ``ticker``."""
    if ticker not in TICKERS:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")

    meta = TICKER_METADATA.get(ticker, {})
    name = cast(str, meta.get("name", ticker))
    sector = cast(str, meta.get("sector", "Unknown sector"))
    industry = cast(str, meta.get("industry", "Unknown industry"))
    description = cast(str, meta.get("description", "No description recorded."))
    competitors = cast(list[str], meta.get("key_competitors", []))
    risks = cast(list[str], meta.get("key_risks", []))
    watch = cast(list[str], meta.get("watch", []))

    lines = [
        f"# COMPANY REPORT — {ticker}",
        f"{name} — {sector}, {industry}",
        "",
        "## BUSINESS",
        description,
        "",
        "## KEY COMPETITORS",
        *_format_bullets(competitors),
        "",
        "## KEY RISKS",
        *_format_bullets(risks),
        "",
        "## WATCH",
        *_format_bullets(watch),
    ]
    return "\n".join(lines)
