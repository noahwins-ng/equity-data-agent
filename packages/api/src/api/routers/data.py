"""Data endpoints — JSON arrays consumed by the Next.js frontend.

These are the counterpart to the report endpoints: the reports return
pre-rendered text for the LangGraph agent, while these return structured
arrays for chart rendering (TradingView Lightweight Charts, etc.).
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from shared.tickers import ALL_OHLCV_TICKERS, TICKER_METADATA, TICKERS

from api.clickhouse import get_client

router = APIRouter(prefix="/api/v1", tags=["data"])


class Timeframe(StrEnum):
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"


_TIMEFRAME_QUERY: dict[Timeframe, tuple[str, str]] = {
    Timeframe.daily: ("equity_raw.ohlcv_raw", "date"),
    Timeframe.weekly: ("equity_derived.ohlcv_weekly", "week_start"),
    Timeframe.monthly: ("equity_derived.ohlcv_monthly", "month_start"),
}

_INDICATOR_COLUMNS = (
    "sma_20",
    "sma_50",
    "sma_200",
    "ema_12",
    "ema_26",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "macd_bullish_cross",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "bb_pct_b",
    "adx_14",
    "atr_14",
    "obv",
)

_INDICATOR_TIMEFRAME_QUERY: dict[Timeframe, tuple[str, str]] = {
    Timeframe.daily: ("equity_derived.technical_indicators_daily", "date"),
    Timeframe.weekly: ("equity_derived.technical_indicators_weekly", "week_start"),
    Timeframe.monthly: ("equity_derived.technical_indicators_monthly", "month_start"),
}

_RSI_OVERBOUGHT = 70.0
_RSI_OVERSOLD = 30.0

# Sparkline window length on the dashboard (~3 trading months) — matches the
# 60-bar context the design v2 watchlist cards render. Computed server-side
# in one query so the frontend page-load avoids the N+1 fan-out per ticker.
_SPARKLINE_BARS = 60


def _rsi_signal(rsi: float | None) -> str:
    if rsi is None:
        return "neutral"
    if rsi >= _RSI_OVERBOUGHT:
        return "overbought"
    if rsi <= _RSI_OVERSOLD:
        return "oversold"
    return "neutral"


def _trend_status(price: float, sma_50: float | None) -> str:
    if sma_50 is None:
        return "neutral"
    return "bullish" if price >= sma_50 else "bearish"


_FUNDAMENTAL_COLUMNS = (
    "ticker",
    "period_end",
    "period_type",
    "pe_ratio",
    "ev_ebitda",
    "price_to_book",
    "price_to_sales",
    "eps",
    "revenue_yoy_pct",
    "net_income_yoy_pct",
    "fcf_yoy_pct",
    "net_margin_pct",
    "gross_margin_pct",
    "ebitda_margin_pct",
    "gross_margin_bps_yoy",
    "net_margin_bps_yoy",
    "roe",
    "roa",
    "fcf_yield",
    "debt_to_equity",
    "current_ratio",
    "revenue_ttm",
    "net_income_ttm",
    "fcf_ttm",
)

# News card window — matches design v2's "Last 7d" framing on the news card.
_NEWS_WINDOW_DAYS = 7
_NEWS_DEFAULT_LIMIT = 25
_NEWS_MAX_LIMIT = 100


@router.get("/dashboard/summary")
def get_dashboard_summary() -> list[dict[str, Any]]:
    """Return a compact summary row per ticker for the dashboard landing page.

    One JSON array covering all configured tickers — avoids the N+1 request
    fan-out the frontend would otherwise need on page load. Each row carries
    the company short ``name`` (sourced from ``TICKER_METADATA``), today's
    actual ``close`` (not ``adj_close`` — we want market price), the
    day-over-day change, the latest RSI-14 + SMA-50, pre-categorized
    ``rsi_signal`` / ``trend_status`` labels, and a 60-bar ``sparkline``
    array (recent daily closes, oldest first) so the watchlist sparkline
    chart renders without an extra ``/ohlcv`` round-trip per ticker. Tickers
    without at least one OHLCV row are omitted.
    """
    query = """
        WITH
        ohlcv_ranked AS (
            SELECT
                ticker,
                date,
                close,
                row_number() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
            FROM equity_raw.ohlcv_raw FINAL
            WHERE ticker IN %(tickers)s
        ),
        ohlcv_recent AS (
            SELECT
                ticker,
                anyIf(close, rn = 1) AS price,
                anyIf(close, rn = 2) AS prior_close
            FROM ohlcv_ranked
            WHERE rn <= 2
            GROUP BY ticker
        ),
        sparkline_recent AS (
            -- ClickHouse's groupArray is documented as having an implementation-
            -- defined order, so we materialize (date, close) pairs, sort by date
            -- ascending, then project close. Ascending date = oldest-first =
            -- left-to-right chart order.
            SELECT
                ticker,
                arrayMap(t -> t.2, arraySort(t -> t.1, groupArray((date, close)))) AS sparkline
            FROM ohlcv_ranked
            WHERE rn <= %(bars)s
            GROUP BY ticker
        ),
        indicators_latest AS (
            SELECT
                ticker,
                argMax(rsi_14, date) AS rsi_14,
                argMax(sma_50, date) AS sma_50
            FROM equity_derived.technical_indicators_daily FINAL
            WHERE ticker IN %(tickers)s
            GROUP BY ticker
        )
        SELECT
            o.ticker AS ticker,
            o.price AS price,
            o.prior_close AS prior_close,
            i.rsi_14 AS rsi_14,
            i.sma_50 AS sma_50,
            s.sparkline AS sparkline
        FROM ohlcv_recent AS o
        LEFT JOIN indicators_latest AS i ON o.ticker = i.ticker
        LEFT JOIN sparkline_recent AS s ON o.ticker = s.ticker
    """
    result = get_client().query(
        query,
        parameters={"tickers": list(TICKERS), "bars": _SPARKLINE_BARS},
    )

    order = {ticker: idx for idx, ticker in enumerate(TICKERS)}
    rows: list[dict[str, Any]] = []
    for row in result.result_rows:
        record = dict(zip(result.column_names, row, strict=True))
        price = float(record["price"])
        prior_close = record["prior_close"]
        rsi = record["rsi_14"]
        sma_50 = record["sma_50"]

        daily_change_pct: float | None = None
        if prior_close is not None and float(prior_close) != 0.0:
            daily_change_pct = (price - float(prior_close)) / float(prior_close) * 100

        sparkline = record["sparkline"] or []
        meta = TICKER_METADATA.get(record["ticker"], {})
        rows.append(
            {
                "ticker": record["ticker"],
                "name": meta.get("name", record["ticker"]),
                "price": price,
                "daily_change_pct": daily_change_pct,
                "rsi_14": rsi,
                "rsi_signal": _rsi_signal(rsi),
                "trend_status": _trend_status(price, sma_50),
                "sparkline": [float(v) for v in sparkline],
            }
        )
    rows.sort(key=lambda r: order.get(r["ticker"], len(order)))
    return rows


@router.get("/ohlcv/{ticker}")
def get_ohlcv(
    ticker: str,
    timeframe: Timeframe = Timeframe.daily,
) -> list[dict[str, Any]]:
    """Return OHLCV rows for ``ticker`` at the requested ``timeframe``.

    Response shape matches TradingView Lightweight Charts' candlestick input:
    ``{time, open, high, low, close, adj_close, volume}[]`` where ``time`` is
    an ISO date string (``YYYY-MM-DD``) — the library accepts this directly, so
    the frontend needs no transformation. Benchmark tickers (SPY) are valid
    here but rejected by ``/fundamentals`` and ``/search/news``.
    """
    ticker = ticker.upper()
    if ticker not in ALL_OHLCV_TICKERS:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")

    table, date_col = _TIMEFRAME_QUERY[timeframe]
    query = f"""
        SELECT {date_col} AS time, open, high, low, close, adj_close, volume
        FROM {table} FINAL
        WHERE ticker = %(ticker)s
        ORDER BY {date_col} ASC
    """
    result = get_client().query(query, parameters={"ticker": ticker})

    rows: list[dict[str, Any]] = []
    for row in result.result_rows:
        record = dict(zip(result.column_names, row, strict=True))
        time_value = record["time"]
        if isinstance(time_value, date):
            record["time"] = time_value.isoformat()
        rows.append(record)
    return rows


@router.get("/fundamentals/{ticker}")
def get_fundamentals(ticker: str) -> list[dict[str, Any]]:
    """Return computed fundamental ratios for ``ticker``.

    Response shape is ``{ticker, period_end, period_type, pe_ratio, ev_ebitda,
    price_to_book, price_to_sales, eps, revenue_yoy_pct, net_income_yoy_pct,
    fcf_yoy_pct, net_margin_pct, gross_margin_pct, roe, roa, fcf_yield,
    debt_to_equity, current_ratio}[]`` where ``period_end`` is an ISO date
    string (``YYYY-MM-DD``). Rows are returned most-recent-first to match the
    ticker-detail ratios table layout, and every ratio column is nullable
    (undefined when the denominator is zero or data is missing).
    """
    ticker = ticker.upper()
    if ticker not in TICKERS:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")

    # Two LEFT JOINs onto equity_raw.fundamentals:
    #   r  — same (ticker, period_end, period_type) → surfaces absolute
    #        revenue / net_income / fcf / ebitda for quarterly + annual rows
    #        (fundamental_summary only stores ratios + the rolling-4Q TTM
    #        aggregates, not raw period values).
    #   bs — TTM rows joined to the quarterly raw row at the same period_end
    #        → surfaces balance-sheet values (total_assets, total_liabilities,
    #        current_assets, current_liabilities, total_debt) so we can
    #        compute ROE / ROA / Debt-to-Equity / Current Ratio on TTM rows.
    #        The asset's `_build_ttm_rows` doesn't populate these, so without
    #        this join the TTM card shows several "—" cells.
    #
    # Override columns (roe / roa / debt_to_equity / current_ratio) come
    # *before* their summary counterparts in the SELECT list and use
    # COALESCE — quarterly + annual rows keep the asset-computed value;
    # TTM rows fall back to the BS-derived calculation.
    override_cols = {"roe", "roa", "debt_to_equity", "current_ratio", "gross_margin_pct"}
    # Explicit `AS` aliases — without them ClickHouse retains the `s.` table
    # prefix in result.column_names when joins introduce name ambiguity, and
    # the dict-zipping below would produce keys like "s.period_end".
    summary_cols = ", ".join(
        f"s.{c} AS {c}" for c in _FUNDAMENTAL_COLUMNS if c not in override_cols
    )
    # `gp_ttm` CTE rolls 4 quarters of gross_profit per ticker so we can
    # synthesize TTM gross margin (= 4Q gross_profit / revenue_ttm). The
    # asset's `_build_ttm_rows` doesn't compute gross_margin_pct because it
    # would need this same rolling sum, so we'd otherwise show "—" for
    # gross margin on every TTM row. `qcount = 4` guards against the first
    # three quarters where the window is undefined.
    query = f"""
        WITH gp_ttm AS (
            SELECT
                ticker,
                period_end,
                sum(gross_profit) OVER (
                    PARTITION BY ticker
                    ORDER BY period_end
                    ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
                ) AS gross_profit_ttm,
                count() OVER (
                    PARTITION BY ticker
                    ORDER BY period_end
                    ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
                ) AS qcount
            FROM equity_raw.fundamentals FINAL
            WHERE period_type = 'quarterly'
        )
        SELECT
            {summary_cols},
            COALESCE(s.gross_margin_pct, IF(
                s.period_type = 'ttm'
                AND gp.qcount = 4
                AND s.revenue_ttm > 0,
                gp.gross_profit_ttm / s.revenue_ttm * 100,
                NULL
            )) AS gross_margin_pct,
            COALESCE(s.roe, IF(
                s.period_type = 'ttm'
                AND (bs.total_assets - bs.total_liabilities) > 0
                AND s.net_income_ttm IS NOT NULL,
                s.net_income_ttm / (bs.total_assets - bs.total_liabilities) * 100,
                NULL
            )) AS roe,
            COALESCE(s.roa, IF(
                s.period_type = 'ttm'
                AND bs.total_assets > 0
                AND s.net_income_ttm IS NOT NULL,
                s.net_income_ttm / bs.total_assets * 100,
                NULL
            )) AS roa,
            COALESCE(s.debt_to_equity, IF(
                s.period_type = 'ttm'
                AND (bs.total_assets - bs.total_liabilities) > 0,
                bs.total_debt / (bs.total_assets - bs.total_liabilities),
                NULL
            )) AS debt_to_equity,
            COALESCE(s.current_ratio, IF(
                s.period_type = 'ttm'
                AND bs.current_liabilities > 0,
                bs.current_assets / bs.current_liabilities,
                NULL
            )) AS current_ratio,
            r.revenue        AS revenue,
            r.net_income     AS net_income,
            r.free_cash_flow AS free_cash_flow,
            r.ebitda         AS ebitda
        FROM equity_derived.fundamental_summary AS s FINAL
        LEFT JOIN (
            SELECT ticker, period_end, period_type, revenue, net_income,
                   free_cash_flow, ebitda
            FROM equity_raw.fundamentals FINAL
        ) AS r
            ON s.ticker = r.ticker
            AND s.period_end = r.period_end
            AND s.period_type = r.period_type
        LEFT JOIN (
            SELECT ticker, period_end, total_assets, total_liabilities,
                   current_assets, current_liabilities, total_debt
            FROM equity_raw.fundamentals FINAL
            WHERE period_type = 'quarterly'
        ) AS bs
            ON s.ticker = bs.ticker
            AND s.period_end = bs.period_end
        LEFT JOIN gp_ttm AS gp
            ON s.ticker = gp.ticker
            AND s.period_end = gp.period_end
        WHERE s.ticker = %(ticker)s
        ORDER BY s.period_end DESC, s.period_type ASC
    """
    result = get_client().query(query, parameters={"ticker": ticker})

    rows: list[dict[str, Any]] = []
    for row in result.result_rows:
        record = dict(zip(result.column_names, row, strict=True))
        period_end = record["period_end"]
        if isinstance(period_end, date):
            record["period_end"] = period_end.isoformat()
        rows.append(record)
    return rows


@router.get("/indicators/{ticker}")
def get_indicators(
    ticker: str,
    timeframe: Timeframe = Timeframe.daily,
) -> list[dict[str, Any]]:
    """Return pre-computed technical indicator rows for ``ticker``.

    Response shape is ``{time, sma_20, sma_50, ema_12, ema_26, rsi_14, macd,
    macd_signal, macd_hist, bb_upper, bb_middle, bb_lower}[]`` where ``time``
    is an ISO date string (``YYYY-MM-DD``). Indicator fields are nullable during
    the warm-up period (e.g. SMA-50 needs 50 prior closes) and those nulls are
    preserved in the response — the frontend whitepaints them on the overlay.
    """
    ticker = ticker.upper()
    if ticker not in TICKERS:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")

    table, date_col = _INDICATOR_TIMEFRAME_QUERY[timeframe]
    columns = ", ".join(_INDICATOR_COLUMNS)
    query = f"""
        SELECT {date_col} AS time, {columns}
        FROM {table} FINAL
        WHERE ticker = %(ticker)s
        ORDER BY {date_col} ASC
    """
    result = get_client().query(query, parameters={"ticker": ticker})

    rows: list[dict[str, Any]] = []
    for row in result.result_rows:
        record = dict(zip(result.column_names, row, strict=True))
        time_value = record["time"]
        if isinstance(time_value, date):
            record["time"] = time_value.isoformat()
        rows.append(record)
    return rows


@router.get("/news/{ticker}")
def get_news(
    ticker: str,
    days: int = Query(default=_NEWS_WINDOW_DAYS, ge=1, le=90),
    limit: int = Query(default=_NEWS_DEFAULT_LIMIT, ge=1, le=_NEWS_MAX_LIMIT),
) -> list[dict[str, Any]]:
    """Return recent ``ticker`` news rows for the design-v2 news card.

    Sourced directly from ``equity_raw.news_raw`` — bypasses Qdrant because
    the news card is a chronological feed, not a semantic search. The 7-day
    default matches the card header (``NEWS Last 7d``); response shape is
    ``{id, headline, body, publisher, publisher_name, image_url, url,
    source, published_at, sentiment_label}[]`` ordered most-recent-first.

    Per ADR-014 anti-pattern §5: empty list is a valid 200 response and is
    rendered identically to "service down" by the frontend (an "empty news"
    panel) — the card consumer must not differentiate.

    Per QNT-148 / ADR-016, two non-obvious behaviours:

    * ``publisher`` is the canonical pill label, computed once here:
      prefer ``resolved_host`` (set at ingest), fall back to the URL host
      when the URL is already a direct outlet, fall back to the trimmed
      Finnhub-supplied label, finally empty string. The frontend reads
      ``item.publisher`` with no fallback chain. (``publisher_name`` and
      ``url`` stay in the payload so a future debug surface can show the
      raw inputs without another query.)
    * Same-ticker rows are de-duplicated by article ``id`` (URL hash):
      Finnhub occasionally returns the same article URL across multiple
      ticks with a slightly shifted ``datetime`` epoch, which lands as
      multiple rows because the ReplacingMergeTree key is
      ``(ticker, published_at, id)``. We keep the most recent
      ``published_at`` per ``id`` so the card doesn't show duplicates.
      Cross-ticker dedup is intentionally not done here — the same URL
      under AAPL and MSFT lands as two distinct rows by design (one per
      mention) and would surface to a future "Related across portfolio"
      view rather than silently collapsing under one ticker.
    """
    ticker = ticker.upper()
    if ticker not in TICKERS:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")

    # The argMax(field, published_at) per column means a re-fetch with a
    # later published_at wins — including for ``resolved_host``. This is
    # intentional: if Finnhub rewrites the redirect target between ticks,
    # the pill silently updates to the new outlet on the next render. The
    # alternative (pin to first-seen value) would persist a stale host past
    # the article's actual outlet, which is worse for credit accuracy.
    query = """
        WITH deduped AS (
            SELECT
                id,
                argMax(headline, published_at) AS headline,
                argMax(body, published_at) AS body,
                argMax(publisher_name, published_at) AS publisher_name,
                argMax(image_url, published_at) AS image_url,
                argMax(url, published_at) AS url,
                argMax(source, published_at) AS source,
                max(published_at) AS published_at,
                argMax(sentiment_label, published_at) AS sentiment_label,
                argMax(resolved_host, published_at) AS resolved_host
            FROM equity_raw.news_raw FINAL
            WHERE ticker = %(ticker)s
              AND published_at >= now() - INTERVAL %(days)s DAY
            GROUP BY id
        )
        SELECT
            toString(id) AS id,
            headline,
            body,
            publisher_name,
            image_url,
            url,
            source,
            published_at,
            sentiment_label,
            multiIf(
                resolved_host != '',
                    resolved_host,
                domain(url) NOT IN ('finnhub.io', ''),
                    replaceRegexpOne(domain(url), '^www\\.', ''),
                trim(BOTH ' ' FROM publisher_name) != '',
                    trim(BOTH ' ' FROM publisher_name),
                ''
            ) AS publisher
        FROM deduped
        ORDER BY published_at DESC
        LIMIT %(limit)s
    """
    result = get_client().query(
        query,
        parameters={"ticker": ticker, "days": days, "limit": limit},
    )

    rows: list[dict[str, Any]] = []
    for row in result.result_rows:
        record = dict(zip(result.column_names, row, strict=True))
        published = record["published_at"]
        if isinstance(published, datetime):
            record["published_at"] = published.isoformat()
        rows.append(record)
    return rows


@router.get("/quote/{ticker}")
def get_quote(ticker: str) -> dict[str, Any]:
    """Return the design-v2 quote-header bundle for ``ticker``.

    One round-trip — the alternative is the frontend stitching ``/ohlcv``
    (last bar + 30-bar avg volume) and ``/fundamentals`` (TTM P/E + raw
    market cap) on every navigation. Computing it server-side keeps the
    quote header on a single ``revalidate: 60`` cache key.

    Response shape:

    ``{ticker, name, sector, industry, price, prev_close, open, day_high,
    day_low, volume, avg_volume_30d, market_cap, pe_ratio_ttm, as_of}``

    where ``as_of`` is the ISO date of the latest bar (the ``close`` framing
    on the design v2 mock — EOD only, NOT a live timestamp). All numeric
    fields are nullable: a brand-new ticker without 30 prior bars surfaces
    ``avg_volume_30d=null`` rather than a partial average.
    """
    ticker = ticker.upper()
    if ticker not in TICKERS:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")

    # Aliases must NOT collide with input column names — ClickHouse resolves
    # the alias before re-reading the column inside another aggregate, which
    # raises "aggregate inside aggregate" (code 184). Hence `today_*`/`price`.
    ohlcv_query = """
        WITH ranked AS (
            SELECT
                date,
                open,
                high,
                low,
                close,
                volume,
                row_number() OVER (ORDER BY date DESC) AS rn
            FROM equity_raw.ohlcv_raw FINAL
            WHERE ticker = %(ticker)s
        )
        SELECT
            anyIf(date,   rn = 1) AS as_of,
            anyIf(open,   rn = 1) AS today_open,
            anyIf(high,   rn = 1) AS day_high,
            anyIf(low,    rn = 1) AS day_low,
            anyIf(close,  rn = 1) AS price,
            anyIf(close,  rn = 2) AS prev_close,
            anyIf(volume, rn = 1) AS today_volume,
            avgIf(volume, rn <= 30) AS avg_volume_30d,
            countIf(rn <= 30) AS bars_in_window
        FROM ranked
    """
    fundamentals_query = """
        SELECT pe_ratio
        FROM equity_derived.fundamental_summary FINAL
        WHERE ticker = %(ticker)s
          AND period_type = 'ttm'
          AND pe_ratio IS NOT NULL
        ORDER BY period_end DESC
        LIMIT 1
    """
    market_cap_query = """
        SELECT market_cap
        FROM equity_raw.fundamentals FINAL
        WHERE ticker = %(ticker)s
          AND market_cap > 0
        ORDER BY period_end DESC, period_type ASC
        LIMIT 1
    """

    client = get_client()
    ohlcv = client.query(ohlcv_query, parameters={"ticker": ticker})
    if not ohlcv.result_rows:
        raise HTTPException(status_code=404, detail=f"No OHLCV data for {ticker}")
    ohlcv_row = dict(zip(ohlcv.column_names, ohlcv.result_rows[0], strict=True))

    pe_rows = client.query(fundamentals_query, parameters={"ticker": ticker}).result_rows
    pe_ratio_ttm = float(pe_rows[0][0]) if pe_rows and pe_rows[0][0] is not None else None

    cap_rows = client.query(market_cap_query, parameters={"ticker": ticker}).result_rows
    market_cap = float(cap_rows[0][0]) if cap_rows and cap_rows[0][0] is not None else None

    as_of = ohlcv_row.get("as_of")
    if isinstance(as_of, date):
        as_of = as_of.isoformat()

    avg_volume = ohlcv_row.get("avg_volume_30d")
    bars_in_window = int(ohlcv_row.get("bars_in_window") or 0)
    # avgIf with no rows still emits 0; surface null so the UI labels it
    # "—" instead of pretending the average is a real, tiny number.
    if bars_in_window < 30:
        avg_volume = None

    meta = TICKER_METADATA.get(ticker, {})
    return {
        "ticker": ticker,
        "name": meta.get("name", ticker),
        "sector": meta.get("sector"),
        "industry": meta.get("industry"),
        "price": float(ohlcv_row["price"]) if ohlcv_row.get("price") is not None else None,
        "prev_close": (
            float(ohlcv_row["prev_close"]) if ohlcv_row.get("prev_close") is not None else None
        ),
        "open": (
            float(ohlcv_row["today_open"]) if ohlcv_row.get("today_open") is not None else None
        ),
        "day_high": (
            float(ohlcv_row["day_high"]) if ohlcv_row.get("day_high") is not None else None
        ),
        "day_low": (float(ohlcv_row["day_low"]) if ohlcv_row.get("day_low") is not None else None),
        "volume": (
            int(ohlcv_row["today_volume"]) if ohlcv_row.get("today_volume") is not None else None
        ),
        "avg_volume_30d": float(avg_volume) if avg_volume is not None else None,
        "market_cap": market_cap,
        "pe_ratio_ttm": pe_ratio_ttm,
        "as_of": as_of,
    }
