# Frontend Design Feasibility Assessment

## Visual reference

- **Canonical mock**: [`design/v2-final.png`](design/v2-final.png) — TERMINAL/NINE v2, dated 2026-04-26.
- **Original draft (for archaeology)**: [`design/v1-original.png`](design/v1-original.png) — what we pushed back on.
- **Convention + source links**: [`design/README.md`](design/README.md).

## Context

The first-draft mock from Claude Design (TERMINAL/NINE — Bloomberg-style dashboard) is the visual target for Phase 6. This doc captures the assessment + final scope after triaging it against today's data, API, and agent layers.

The output is three lists:
1. **Push back to the designer** for v2 — assumptions that don't hold for our architecture.
2. **Add on our side** — what we'll build, scoped by cost (cheap / medium / dropped).
3. **Decisions** — the explicit choices made during triage.

## Decisions

- **Watchlist sparklines** — keep, extend `/api/v1/dashboard/summary` with a `sparkline: number[]` field.
- **Tool labels in agent chat** — human labels (`Reading technicals…`), not raw function names.
- **Data fields with no source** — drop, don't fake. Substitute where reasonable.
- **News source switch** — adopt one of two free options in place of Yahoo RSS (Finnhub `/company-news` or Alpha Vantage `NEWS_SENTIMENT` — recommendation below).
- **Sentiment classification** — if Finnhub: Groq Llama 3.3 70B via existing LiteLLM proxy ($0). If AV: built into the news response, no separate classifier.
- **Thesis persistence + workspace tab** — deferred. Tracked as [QNT-130](https://linear.app/noahwins/issue/QNT-130/deferred-thesis-persistence-workspace-tab) with explicit revisit triggers.
- **Real-time / 5s refresh framing** — drop entirely. We're EOD.
- **Sparkline shape** — designer's call (line / area / candle-mini all serveable from same data).
- **Sentiment chip pre-classification state** — show neutral placeholder, don't hide.
- **Empty-state language** — `N/A` as default for missing fields.
- **Article images from Finnhub** — ignore in v2; revisit if visual density needs them later.

## Push back to Claude Design (v2 brief)

1. **Drop the "LIVE / 8ms / 5s refresh" framing.** Reframe as an analyst workstation, not a trading terminal. Replace with `EOD · last close <date>` and `next ingest 02:00 ET`.
2. **Drop the FMP / Finnhub / TradingView / EDGAR source list in the footer.** Replace with what we actually run: `yfinance · Finnhub · Qdrant`.
3. **Drop the FWD column** in fundamentals and FWD P/E in the quote header. We won't pay for analyst consensus.
4. **Recast fundamentals tabs as `Quarterly · Annual · TTM`** (TTM is a derivation we'll add).
5. **Substitute fundamentals rows we can't compute:**
   - `Operating margin` → **EBITDA margin** (we have EBITDA from yfinance).
   - `ROIC` → **ROE / ROA** (already computed).
6. **Drop the `VWAP` indicator chip** — degenerate on daily bars. Replace with `ATR` or `OBV`.
7. **Compare overlays** — keep SPY only (drop SMH; sector-specific).
8. **News sentiment chips and publisher attribution stay** — Finnhub `/company-news` returns the original publisher in the `source` field plus an article image. Designer should know: prestige logos (Reuters/Bloomberg/WSJ/FT specifically) aren't guaranteed; design with the publisher *name* as the primitive, not a fixed logo set.
9. **Drop the `BUILD FULL THESIS` button + `THESIS DOC` workspace tab.** Tracked in QNT-130; revisit when we have a concrete consumer (someone asking to retrieve a prior thesis).
10. **Tool-call progress affordance** — show human labels (`Reading technicals…`, `Scanning news…`), not function names.

## Add on our side

### Cheap (pure transformations on existing data, ~half-day each)

- `SMA200`, `ADX(14)`, `ATR(14)`, `OBV`, `Bollinger %B` columns in `technical_indicators_daily`.
- `EBITDA margin` in `fundamental_summary` (EBITDA already in `equity_raw.fundamentals`).
- `bps deltas` on existing margins (window function over prior period).
- `MACD bullish-cross` boolean on indicators rows.
- Sparkline field on `GET /api/v1/dashboard/summary`.
- Add SPY to `shared.tickers` for benchmark overlay (price-only ingest).

### Medium (~1–2 days)

- **TTM rollup asset** — rolling-4Q sums of revenue / net income / FCF / EPS in `fundamental_summary`.
- **News source migration: Yahoo RSS → one of two free options.**

  | | Finnhub `/company-news` | Alpha Vantage `NEWS_SENTIMENT` |
  |---|---|---|
  | Tier | Free (verified — `premium: null` in their schema) | Free (verified — no premium-label in docs) |
  | Rate limit | **60 calls/min** (massive headroom) | **25 calls/day** (tight) |
  | Sentiment included | ✗ — pair with Groq classifier | ✓ — 5-class label + numeric score + per-ticker sentiment |
  | Publisher attribution | ✓ `source` field | ✓ `source` + `source_domain` |
  | Article image | ✓ | ✓ `banner_image` |
  | History | 1y free | unclear under quota |
  | Coverage | North American only (matches scope) | Multi-ticker comma-separated documented but unverified |

  **Recommendation: Finnhub + Groq classifier.** Rate-limit headroom enables a 1y backfill, no single-vendor dependency for the entire news pipeline, and Groq's free tier makes the sentiment ticket effectively free. AV is worth a 1-week pilot first if we want to check sentiment quality empirically — but not as the production path.

  Schema impact either way: extend `news_raw` with `publisher_name` + `image_url` columns; if AV, also `sentiment_score` + `sentiment_label`.

- **News sentiment classifier asset (only if Finnhub)** — Groq Llama 3.3 70B via existing LiteLLM proxy. $0 cost (Groq free tier: 30 req/min, 14.4k req/day — orders of magnitude over our need). Gemini 2.0 Flash via Google AI Studio is the fallback (also free, 15 req/min, 1500 req/day). FinBERT local is the no-vendor fallback if API access becomes an issue.
- **SSE streaming endpoint** for the agent (`POST /api/v1/agent/chat`) — already on roadmap as Phase 5 / QNT-60. Required for the chat UX.

### Dropped (no-new-paid-source rule + design pushback)

- ~~Forward EPS / FWD P/E / analyst consensus~~ — would require FMP.
- ~~Operating margin~~ — replaced by EBITDA margin.
- ~~ROIC~~ — replaced by ROE/ROA.
- ~~VWAP~~ — degenerate on daily bars.
- ~~Multi-publisher / Reuters-Bloomberg-WSJ logos as designed~~ — Finnhub gives publisher names but no guarantee on prestige set.
- ~~Earnings as a discrete event card~~ — would require new structured event ingestion.
- ~~Real-time / intraday tick stream~~ — would require new data source + architecture change.
- ~~Thesis persistence + THESIS DOC tab~~ → QNT-130.

## Critical files (for reference when building)

- News asset to rewrite: `packages/dagster-pipelines/src/dagster_pipelines/assets/news_raw.py`
- News feed config: `packages/dagster-pipelines/src/dagster_pipelines/news_feeds.py`
- Indicators asset: `packages/dagster-pipelines/src/dagster_pipelines/assets/indicators/technical_indicators.py`
- Fundamentals: `packages/dagster-pipelines/src/dagster_pipelines/assets/fundamentals.py`, `assets/fundamental_summary.py`
- API endpoints: `packages/api/src/api/routers/{tickers,data,reports,search}.py`
- Agent: `packages/agent/src/agent/{graph.py,tools.py,prompts/system.py}`
- Ticker registry: `packages/shared/src/shared/tickers.py`
- DDL: `db/migrations/`

## Verification (when we eventually build)

- New indicators: asset checks with domain bounds (`0 ≤ rsi ≤ 100`, ATR > 0, OBV monotonic-ish over short windows).
- TTM rollup: spot-check vs. published 10-K/10-Q figures for one ticker.
- Finnhub migration: side-by-side comparison of headline counts vs Yahoo RSS for one week before cutover; backfill 1y of history on first run.
- Sentiment classifier: hold out 50 hand-labeled headlines, check accuracy ≥ 80% before shipping the chip.
- Frontend: `make dev-frontend` → walk all 10 tickers, confirm sparklines + chart + technicals + fundamentals + news + agent thesis end-to-end.

## Linear

- [QNT-130](https://linear.app/noahwins/issue/QNT-130/deferred-thesis-persistence-workspace-tab) — deferred thesis persistence (Backlog, Low). Revisit triggers in description.
- [QNT-131](https://linear.app/noahwins/issue/QNT-131/add-pending-state-to-news-sentiment-schema-classifier-output) — add `pending` state to news sentiment schema + classifier output (Backlog, Medium). Triggered by `pend` chip in design v2.
- [QNT-132](https://linear.app/noahwins/issue/QNT-132/expose-subsystem-provenance-via-apiv1health-for-data-driven-ui) — expose subsystem provenance via `/api/v1/health` (Backlog, Medium). Trimmed 2026-04-28 to **SOURCES + JOBS** only. SENTIMENT row dropped (QNT-131 classifier deferred → `provenance.sentiment` stays `null`); AGENT row dropped as a constant not worth a contract for. The two-line strip still demonstrates the data-driven UI pattern on the values that actually change.
- [QNT-133](https://linear.app/noahwins/issue/QNT-133/restructure-agent-thesis-output-setup-bull-case-bear-case-verdict) — restructure agent thesis output to Setup / Bull Case / Bear Case / Verdict (Backlog, Medium, Phase 5). Triggered by the thesis card in design v2.
- [QNT-134](https://linear.app/noahwins/issue/QNT-134/phase-6-backend-support-indicator-fundamental-sparkline-spy-additions) — Phase 6 backend support: SMA200 / ADX / ATR / OBV / BB %B / EBITDA margin / bps deltas / TTM rollup / sparkline endpoint / SPY benchmark ingest (Backlog, Medium, Phase 6). Bundled cheap-tier additions; gates QNT-72 (sparklines + SPY) and QNT-73 (technicals + fundamentals panel).
- Phase 6 frontend tickets (QNT-71..75) descriptions updated 2026-04-26 to reflect the design v2 mock — pane ownership, dependencies on QNT-121/131/132/133/134, EOD framing, design-driven substitutions.
- A new ticket should be opened for the **news source migration** once Finnhub-vs-AV is decided — left to triage into the right cycle.
