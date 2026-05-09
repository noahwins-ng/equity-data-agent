"""HTTP tool wrappers for the LangGraph agent (QNT-57).

Each tool fetches a pre-rendered text report from the FastAPI layer and
returns the body as a string for LLM consumption. Tools never raise —
every failure path (HTTP error, unreachable endpoint, timeout, unknown
ticker) returns a descriptive error string so the synthesize prompt can
note the gap the way it already notes missing reports. The retry /
error-recording machinery in ``agent.graph._gather_reports`` is still
exercised if a tool throws for any other reason (import failure, URL
construction bug), so the never-raise contract is the first line of
defence, not the only one.

Tool-contract block (ADR-003 / QNT-57 Phase 4 lesson):

get_summary_report
  Input:    ticker ∈ shared.tickers.TICKERS
  Upstream: GET /api/v1/reports/summary/{ticker}
  Return:   text report body. Degraded: "[error] <kind>: <detail>".

get_technical_report / get_fundamental_report / get_news_report
  Same shape as get_summary_report — only the URL path segment differs.

search_news
  Input:    ticker ∈ TICKERS, query (1..512 chars).
  Upstream: GET /api/v1/search/news?ticker={ticker}&query={query}&limit=5
  Return:   pretty-serialised JSON array of {headline, source, date, score,
            url}. Degraded: "[]" on Qdrant outage, HTTP error, empty match
            set, or invalid ticker / query.

``search_news`` degrades to ``"[]"`` rather than ``[error] ...`` on purpose:
the FastAPI endpoint already maps Qdrant outages to an empty 200 list
(QNT-55), so the agent reads "Qdrant unreachable" and "no matches" the same
way. The four report tools return ``[error] ...`` because their upstream
endpoints return real text on success, and the LLM needs a distinct signal
to tell "degraded" apart from "clean report."
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Final

import httpx
from shared.config import settings
from shared.tickers import TICKERS

# Tool wrappers are pure HTTP-to-FastAPI calls, not LLM calls — no
# `@observe` decorators here. The four agent-graph node spans (classify
# / plan / gather / synthesize) carry the agent's behavioural shape;
# tool-call correctness is asserted by the eval's `tool_call_ok` axis,
# tool latency / errors live in Sentry + FastAPI access logs. Tracing
# every tool call adds 4-6 Langfuse observations per chat run with no
# debug signal a span doesn't already give us.

logger = logging.getLogger(__name__)

# Tight timeout — the graph retries each tool up to _MAX_TOOL_ATTEMPTS times,
# so a 30s hang would stall the whole run past the SSE client's patience.
_TIMEOUT_SEC: Final[float] = 10.0
_SEARCH_LIMIT: Final[int] = 5
_QUERY_MAX_LEN: Final[int] = 512


def _base_url() -> str:
    return settings.API_BASE_URL.rstrip("/")


def _format_error(kind: str, detail: str) -> str:
    """LLM-legible error string. The ``[error]`` prefix is a stable token the
    synthesize prompt can grep for when explaining a missing report."""
    return f"[error] {kind}: {detail}"


def _normalize_ticker(ticker: str) -> tuple[str, str | None]:
    """Return ``(upper_ticker, None)`` on success or ``(upper_ticker, error)``
    if the ticker is not in ``shared.tickers.TICKERS``. Callers treat a
    non-None second element as a short-circuit return value."""
    upper = ticker.upper()
    if upper not in TICKERS:
        return upper, _format_error("unknown-ticker", upper)
    return upper, None


def _fetch_text(url: str, name: str) -> str:
    try:
        response = httpx.get(url, timeout=_TIMEOUT_SEC)
    except httpx.TimeoutException as exc:
        logger.warning("%s: timeout fetching %s", name, url)
        return _format_error("timeout", str(exc) or url)
    except httpx.HTTPError as exc:
        logger.warning("%s: http error fetching %s: %s", name, url, exc)
        return _format_error("unreachable", f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 — never-raise contract (QNT-57 AC #2)
        # Catches everything outside the HTTPError tree (httpx.InvalidURL from a
        # malformed API_BASE_URL, UnicodeDecodeError from a non-text body, etc).
        # The graph's retry loop treats exceptions as "try again" — a config
        # bug like a bad URL would burn both attempts before surfacing, so we
        # convert those to a descriptive string here instead.
        logger.warning("%s: unexpected error fetching %s: %s", name, url, exc)
        return _format_error("unexpected", f"{type(exc).__name__}: {exc}")

    try:
        body = response.text
    except Exception as exc:  # noqa: BLE001 — see above; response.text can raise on decode
        logger.warning("%s: could not decode body from %s: %s", name, url, exc)
        return _format_error("decode", f"{type(exc).__name__}: {exc}")

    if response.status_code >= 400:
        snippet = (body or "").strip().splitlines()[0:1]
        detail = snippet[0][:200] if snippet else url
        return _format_error(f"http-{response.status_code}", detail)
    return body


def _report_tool(kind: str, ticker: str) -> str:
    ticker_upper, err = _normalize_ticker(ticker)
    if err is not None:
        return err
    url = f"{_base_url()}/api/v1/reports/{kind}/{ticker_upper}"
    return _fetch_text(url, name=f"{kind}-report")


def get_summary_report(ticker: str) -> str:
    """High-level snapshot report. Phase-5 convention: the agent reads this
    first to orient itself before selecting deeper reports in the plan step."""
    return _report_tool("summary", ticker)


def get_technical_report(ticker: str) -> str:
    """Technical-indicator report: RSI, MACD, SMAs, and trend summary."""
    return _report_tool("technical", ticker)


def get_fundamental_report(ticker: str) -> str:
    """Fundamental report: latest P/E, revenue, and earnings surprises."""
    return _report_tool("fundamental", ticker)


def get_news_report(ticker: str) -> str:
    """Recent news headlines (pre-aggregated by the API template layer)."""
    return _report_tool("news", ticker)


def get_company_report(ticker: str) -> str:
    """Static company business profile: description, competitors, risks, watch."""
    return _report_tool("company", ticker)


def search_news(ticker: str, query: str) -> str:
    """Semantic news search against Qdrant, returned as pretty JSON.

    Degrades to ``"[]"`` for every failure mode — Qdrant outage, HTTP error,
    malformed JSON, empty result set, invalid ticker, or empty / over-long
    query. The FastAPI endpoint already maps Qdrant outages to an empty 200
    list (QNT-55), so the agent reads "unreachable" and "no matches" the same
    way rather than needing two code paths.
    """
    ticker_upper, err = _normalize_ticker(ticker)
    if err is not None:
        return "[]"
    if not query or len(query) > _QUERY_MAX_LEN:
        return "[]"

    url = f"{_base_url()}/api/v1/search/news"
    params = {"ticker": ticker_upper, "query": query, "limit": _SEARCH_LIMIT}
    try:
        response = httpx.get(url, params=params, timeout=_TIMEOUT_SEC)
    except Exception as exc:  # noqa: BLE001 — never-raise contract (QNT-57 AC #2)
        # Blanket catch: TimeoutException, HTTPError, and the non-HTTPError-tree
        # cases (httpx.InvalidURL for a malformed API_BASE_URL, etc). The
        # endpoint's own contract already maps every degraded case to [] so
        # conflating them here is consistent with what the agent already sees.
        logger.warning("search_news: request failed ticker=%s: %s", ticker_upper, exc)
        return "[]"

    if response.status_code >= 400:
        logger.warning(
            "search_news: http %d ticker=%s",
            response.status_code,
            ticker_upper,
        )
        return "[]"

    try:
        payload = response.json()
    except ValueError:
        logger.warning("search_news: invalid JSON ticker=%s", ticker_upper)
        return "[]"
    if not payload:
        return "[]"
    return json.dumps(payload, indent=2)


def default_report_tools() -> dict[str, Callable[[str], str]]:
    """REPORT_TOOLS-shaped tool map for ``agent.graph.build_graph``.

    QNT-175 adds ``company`` — a static business-profile tool that grounds
    every thesis in the company's actual operating model (description, key
    competitors, key risks, watch metrics). Unlike the other three, the
    company endpoint never queries the warehouse, so the tool stays
    available even when ClickHouse is unreachable.

    ``get_summary_report`` and ``search_news`` are exported for direct use
    (e.g. QNT-60's CLI / SSE endpoint) but intentionally not added to the
    plan surface. Widening the plan is a QNT-67-evidenced call, not a
    drive-by change.
    """
    return {
        "company": get_company_report,
        "technical": get_technical_report,
        "fundamental": get_fundamental_report,
        "news": get_news_report,
    }


__all__ = [
    "default_report_tools",
    "get_company_report",
    "get_fundamental_report",
    "get_news_report",
    "get_summary_report",
    "get_technical_report",
    "search_news",
]
