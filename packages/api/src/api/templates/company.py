"""Company knowledge report — static business profile + live CONTEXT NOW block.

Pre-QNT-207 this template was DB-free: ``TICKER_METADATA`` already encoded
the editorial context the agent needed to ground a thesis. QNT-207 adds a
``## CONTEXT NOW`` block at the top, stitched from latest readings the
fundamental + technical templates already expose:

  - Latest P/E + premium / inline / discounted label (latest quarterly row)
  - Latest revenue YoY % (latest quarterly row)
  - Daily TREND label (latest two daily indicator rows)

All values are surfaced verbatim — no arithmetic in this template (ADR-003).
If the warehouse is unreachable or rows are missing, each line falls back to
``N/A`` so the static portion still renders (this report is the only static
report in ``default_report_tools`` and should stay available when the tunnel
is flaky).
"""

from __future__ import annotations

import logging
import math
from datetime import date
from typing import Any, cast

from fastapi import HTTPException
from shared.tickers import TICKER_METADATA, TICKERS

from api.clickhouse import get_client
from api.templates.fundamental import _multiple_label
from api.templates.technical import _trend_label

logger = logging.getLogger(__name__)


def _format_bullets(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items] if items else ["- (none recorded)"]


def _fetch_latest_quarterly(ticker: str) -> dict[str, Any] | None:
    """Return the latest quarterly row's pe_ratio / revenue_yoy_pct / pe_history."""
    try:
        client = get_client()
        result = client.query(
            """
            SELECT pe_ratio, revenue_yoy_pct
            FROM equity_derived.fundamental_summary FINAL
            WHERE ticker = %(ticker)s AND period_type = 'quarterly'
            ORDER BY period_end DESC
            LIMIT 20
            """,
            parameters={"ticker": ticker},
        )
    except Exception as exc:
        logger.warning("company.CONTEXT_NOW fundamentals fetch failed for %s: %s", ticker, exc)
        return None
    if not result.result_rows:
        return None
    rows = [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]
    latest = rows[0]
    pe_history = [
        r["pe_ratio"] for r in rows if r["pe_ratio"] is not None and math.isfinite(r["pe_ratio"])
    ]
    return {
        "pe_ratio": latest.get("pe_ratio"),
        "revenue_yoy_pct": latest.get("revenue_yoy_pct"),
        "pe_history": pe_history,
    }


def _fetch_daily_trend(ticker: str) -> str | None:
    """Return the daily TREND label (Uptrend / Sideways / Downtrend) or None."""
    try:
        client = get_client()
        result = client.query(
            """
            SELECT i.date AS as_of, i.sma_20 AS sma_20, i.sma_50 AS sma_50,
                   o.close AS close
            FROM equity_derived.technical_indicators_daily AS i FINAL
            INNER JOIN equity_raw.ohlcv_raw AS o FINAL
              ON i.ticker = o.ticker AND i.date = o.date
            WHERE i.ticker = %(ticker)s
            ORDER BY i.date DESC
            LIMIT 2
            """,
            parameters={"ticker": ticker},
        )
    except Exception as exc:
        logger.warning("company.CONTEXT_NOW daily-trend fetch failed for %s: %s", ticker, exc)
        return None
    if not result.result_rows:
        return None
    rows = [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]
    latest = rows[0]
    prior = rows[1] if len(rows) > 1 else None
    return _trend_label(
        float(latest["close"]),
        float(prior["close"]) if prior else None,
        latest["sma_20"],
        latest["sma_50"],
    )


def _context_now_lines(ticker: str) -> list[str]:
    fundamentals = _fetch_latest_quarterly(ticker)
    trend = _fetch_daily_trend(ticker)
    lines: list[str] = ["## CONTEXT NOW"]

    if fundamentals is None:
        lines.append("Latest P/E: N/A (fundamentals unavailable)")
        lines.append("Latest revenue YoY: N/A (fundamentals unavailable)")
    else:
        pe = fundamentals["pe_ratio"]
        if pe is None or not math.isfinite(pe):
            lines.append("Latest P/E: N/A (no quarterly P/E reported)")
        else:
            label = _multiple_label(pe, fundamentals["pe_history"], None)
            label_suffix = f" — {label}" if label else ""
            lines.append(f"Latest P/E: {pe:.2f}{label_suffix}")
        rev = fundamentals["revenue_yoy_pct"]
        if rev is None or not math.isfinite(rev):
            lines.append("Latest revenue YoY: N/A (no quarterly revenue reported)")
        else:
            lines.append(f"Latest revenue YoY: {rev:+.2f}%")

    if trend is None:
        lines.append("Daily trend: N/A (no recent indicator rows)")
    else:
        lines.append(f"Daily trend: {trend}")
    return lines


def build_company_report(ticker: str, profile: str = "full") -> str:
    """Build a static business-context report for ``ticker`` with a live CONTEXT NOW block.

    ``profile`` selects how much static prose to render (QNT-220 #8). The company
    report is force-injected into every thesis and comparison (QNT-175), so its
    size is a fixed per-turn tax on ~84% of agent turns.

    * ``"full"`` (default) -- the complete profile: CONTEXT NOW, business
      description, competitors, risks, and watch metrics. Used by focused
      fundamental/company asks and any direct ``/reports/company`` consumer.
    * ``"compact"`` -- trims the static lists the thesis rarely cites
      (competitors, watch) while keeping the ``## CONTEXT NOW`` numeric block
      **verbatim** (every number the hallucination scorer traces) plus the
      business description and key risks that ground the qualitative aspects.
      Consumed by the agent on the thesis/comparison/exploration hot path.
    """
    if ticker not in TICKERS:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")
    if profile not in ("full", "compact"):
        raise HTTPException(status_code=400, detail=f"Unknown profile: {profile}")

    meta = TICKER_METADATA.get(ticker, {})
    name = cast(str, meta.get("name", ticker))
    sector = cast(str, meta.get("sector", "Unknown sector"))
    industry = cast(str, meta.get("industry", "Unknown industry"))
    description = cast(str, meta.get("description", "No description recorded."))
    competitors = cast(list[str], meta.get("key_competitors", []))
    risks = cast(list[str], meta.get("key_risks", []))
    watch = cast(list[str], meta.get("watch", []))

    today = date.today().isoformat()
    context_lines = _context_now_lines(ticker)

    header = [
        f"# COMPANY REPORT — {ticker}",
        f"{name} — {sector}, {industry}",
        f"As of {today}",
        "",
        *context_lines,
        "",
        "## BUSINESS",
        description,
    ]
    if profile == "compact":
        # Numbers (CONTEXT NOW) verbatim + business + risks; drop the
        # competitor / watch lists the thesis rarely quotes.
        return "\n".join([*header, "", "## KEY RISKS", *_format_bullets(risks)])

    lines = [
        *header,
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
