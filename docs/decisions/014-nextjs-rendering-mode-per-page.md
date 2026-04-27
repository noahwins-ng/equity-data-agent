# ADR-014: Next.js rendering mode per page (SSG / SSR / CSR / ISR) + cache strategy

**Date**: 2026-04-27
**Status**: Accepted

## Context

Phase 6 (TERMINAL/NINE — `docs/design-frontend-plan.md`) is the first frontend ticket. Before the first page component is written, we need to choose a rendering mode and cache strategy for every data-fetching surface. This ADR is the frontend counterpart of ADR-010 (Dagster production topology) and ADR-011 (LLM routing): the "pre-decide before the first commit" guardrail Phase 4/5 retros recommended after watching `simplest thing that works` framework defaults silently miss production constraints (`feedback_vendor_prod_docs.md`).

Three retro lessons feed directly into this ADR:

1. Dagster QNT-100 -> QNT-116 (~17 h, 4 incidents): shipped the quickstart single-process topology, ratcheted `mem_limit` three times, ended at the canonical production topology that the prod docs prescribed all along. Cost of skipping the prod page once: ~11 h vs ~6 h.
2. Qdrant point IDs QNT-54 -> QNT-120: shipped `id = hash(url)` because the quickstart used it, silently overwrote cross-ticker mentions until QNT-93's check fired in prod. The fix was a one-line ID change but the lesson was structural: write the cross-store identity invariant down before shipping.
3. `feedback_pre_design_cross_store_identity.md`: extended the Qdrant lesson to any bridge between two stores, including frontend <-> API.

Next.js 15 App Router has the same shape of trap. The defaults look benign, then collide with our actual requirements:

- Forgetting `revalidate` on a server `fetch` -> Next.js 15 default is *no caching*. Every navigation hits FastAPI; the dashboard amplifies request load 10-50x for boring EOD data.
- Defaulting `dynamic = "force-dynamic"` on `/ticker/[symbol]` -> wastes compute on data that changes once per day at 02:00 ET.
- Trying to render the chat SSE stream inside an RSC boundary -> RSC produces one HTML payload; SSE is a long-lived event stream. They do not compose. Discovering this after writing the chat is rework.
- Hardcoding the ticker list in `generateStaticParams()` -> repeats the project-wide "ticker scope = one string" rule violation that `shared.tickers` exists to prevent.
- Treating chat as a route (`/chat`) -> tears down + remounts on every ticker navigation, losing in-flight SSE connections. Design v2 makes chat a persistent right-rail panel; the implementation must match.

## Decision

The app shell (per design v2) has four data surfaces. For each, name (1) rendering mode, (2) data-fetch location, (3) cache directive, (4) failure mode, and (5) the upstream-PK -> downstream-PK identity statement.

```
+------------------------------------------------------------------+
|  app/layout.tsx (persistent shell)                               |
|  +------------+  +-------------------------------+  +----------+ |
|  | Watchlist  |  | route slot:                   |  |  Chat    | |
|  | (left)     |  |   / (landing)                 |  |  (right) | |
|  | server     |  |   /ticker/[symbol]            |  |  client  | |
|  | component  |  |                               |  |  (SSE)   | |
|  +------------+  +-------------------------------+  +----------+ |
+------------------------------------------------------------------+
```

### 1. Watchlist (left rail, in `app/layout.tsx`)

| Field | Value |
|---|---|
| Surface | Persistent server component inside the root layout — visible on every route. |
| Rendering | Server Component with cached `fetch` (Next.js Data Cache, 60 s TTL). Note: this is the Data Cache, not "ISR" in the route-segment sense -- ISR is a property of `page.tsx` / `route.ts`, not a layout-level fetch. |
| Data fetch | Server `fetch(NEXT_PUBLIC_API_URL + "/api/v1/dashboard/summary", { next: { revalidate: 60 } })` |
| Cache | `revalidate: 60`. Underlying data is daily-cadence (Dagster ingest at `02:00 ET`), so any TTL up to ~24 h is equally fresh; 60 s is chosen to collate concurrent requests during a navigation burst and bound debug-feedback latency when iterating, not to track ingest. Explicit, because Next.js 15 dropped default `fetch` caching. |
| Failure mode | `fetch` rejects -> RSC throws -> nearest `error.tsx` boundary renders fallback ("watchlist unavailable"). Never block route render on a watchlist failure. |
| Identity | upstream: `/dashboard/summary` rows keyed by `ticker` -> downstream: React `<li>` keyed by `ticker`. The full ticker set (10 portfolio + SPY benchmark) comes from `/api/v1/tickers` -- never hardcoded. Pre-condition: SPY must be added to `packages/shared/src/shared/tickers.py` (tracked under QNT-134, "Add SPY to `shared.tickers` for benchmark overlay"); until that lands, `/api/v1/tickers` returns 10 and the SPY benchmark row is absent rather than synthesised. |

### 2. `/` (Dashboard landing — middle pane empty state)

| Field | Value |
|---|---|
| Surface | `app/page.tsx` |
| Rendering | SSG. `dynamic = "force-static"` -- no per-request data, just the "select a ticker" prompt. |
| Data fetch | None on the page itself. The watchlist (in the layout above) provides the only data on this view. |
| Cache | Static at build time; no revalidate needed. |
| Failure mode | n/a (no data). |
| Identity | n/a. |

### 3. `/ticker/[symbol]` (Ticker detail page — middle pane)

| Field | Value |
|---|---|
| Surface | `app/ticker/[symbol]/page.tsx` |
| Rendering | ISR (route-segment). `generateStaticParams()` reads `/api/v1/tickers` at build time -- single source, no hardcoded augmentation. The endpoint returns 10 today and 11 once QNT-134 adds SPY to `shared.tickers`; either way the frontend code is unchanged. `revalidate = 60` keeps each page warm without thundering FastAPI. |
| Data fetch | Server component fetches the four reports in parallel via `Promise.all`: `/reports/summary`, `/reports/technical`, `/reports/fundamental`, `/reports/news`. Provenance strip fetches `/api/v1/health` with `revalidate: 300` (changes only on backend deploys). |
| Cache | All server fetches use `next: { revalidate: 60 }`. The chart and indicator-aggregation toggles are client-side (see below) and bypass the server cache. |
| Failure mode | Per QNT-55 lesson: any single report endpoint that 200s with empty data renders the same empty state as "service down" (`N/A`, "no recent news"). A `fetch` rejection bubbles to `error.tsx`. Per-card boundaries are an iteration target, not v1. |
| Identity | upstream: `(ticker)` from URL param `symbol` -> downstream: each report keyed by `ticker`. Normalize to upper-case before fetch (URL is case-permissive, ClickHouse `ticker` is upper-case). |

Client-component sub-surfaces inside this route:

- **Candlestick chart (TradingView Lightweight Charts)** -- `"use client"`. Date-range toggle (`1M / 3M / 6M / YTD / 1Y / 5Y / MAX`) and SPY overlay toggle re-fetch via `fetch(API_URL + "/api/v1/ohlcv/" + symbol + "?range=...")` with `cache: "no-store"`. No server pre-render of the chart payload (TradingView ingests it client-side anyway).
- **Indicator timeframe tabs (`Daily / Weekly / Monthly`)** -- client; same pattern as chart.
- **Fundamentals period tabs (`Quarterly / Annual / TTM`)** -- client; same pattern.

### 4. Chat panel (right rail, in `app/layout.tsx`)

| Field | Value |
|---|---|
| Surface | `<ChatPanel />` client component imported into `app/layout.tsx`. Persistent across `/` <-> `/ticker/[symbol]` navigation. |
| Rendering | CSR. `"use client"` from the panel root. No server component anywhere in the SSE path. |
| Data fetch | Direct `fetch(API_URL + "/api/v1/agent/chat", { method: "POST", body: ..., headers: { "Accept": "text/event-stream" } })` returning a `ReadableStream`. Parsed with `eventsource-parser` (~2 KB). Per ADR-008, no Vercel AI SDK. |
| Cache | None. SSE is request-scoped. |
| Failure mode | Stream errors / disconnects -> error event rendered inline ("connection lost, retry"). Re-submit recreates the stream. No mid-stream resume in v1. |
| Identity | upstream: SSE event ordering (server-emitted sequence) -> downstream: React message array, index = receipt order. Lost event on disconnect -> partial thesis; user re-submits. |

Active ticker is read from `usePathname()` -- the panel observes the route, never owns it. The composer placeholder ("Ask the analyst about NVDA...") and source list come from `/api/v1/health` provenance (QNT-132), fetched once at panel mount via client-side `fetch`.

## Alternatives Considered

**Default Next.js 15 fetch behavior (no `revalidate`).** Every navigation re-hits FastAPI. For 10 tickers x ~5 reports per page x N visitors, this is a 50x amplifier on a backend whose budget already includes a daily Dagster fan-out. Free-tier hosting (Vercel + Hetzner CX41) absorbs the cost in latency, not bills, but the page would feel slow for no reason. *Rejected.*

**`dynamic = "force-dynamic"` on every page.** Conceptually simple ("always fetch fresh"), matches the SSR-everywhere mental model. But our data is daily-cadence: the chart, indicators, fundamentals, and news change once per day at 02:00 ET. Force-dynamic is wasted server work + slower TTFB for zero freshness gain. *Rejected.*

**Pure CSR / SPA-style (no server fetch, every page hydrates and fetches client-side).** Simpler mental model -- one rendering mode for the whole app. Loses SEO and the first-paint advantage on `/ticker/[symbol]`. The portfolio framing (recruiter clones the repo, opens the deployed URL) wants a fast first paint of a populated ticker page, not a loading skeleton. *Rejected for the ticker page; the chat panel is CSR for unrelated reasons (SSE).* 

**Chat as a route (`/chat`).** What the original QNT-121 description proposed and what design v1 implied. Each `/ticker -> /chat` navigation tears down the panel and any in-flight SSE stream. Lost messages, lost typing state, ugly transition. Design v2 explicitly reframes chat as a persistent right-rail panel; the rendering decision must match. *Rejected -- chat is a panel, not a route.*

**Vercel AI SDK `useChat` for the chat panel.** Already covered by ADR-008. Mentioning here only because the chat-panel decision is the place a future contributor would re-ask the question. The agent runs in Python behind a FastAPI SSE endpoint; `useChat` assumes the LLM is callable from a Next.js route handler. Wrapping the Python endpoint in the SDK's transport adapter is fighting the framework. *Rejected (per ADR-008).*

**Cache reports in Vercel KV / Edge Config.** Overkill for our scale. ISR with `revalidate: 60` on the same Vercel runtime gets 99 % of the win without adding a state store to operate. *Out of scope; revisit if `revalidate` proves insufficient.*

**Server Actions for the agent chat.** Idiomatic React 19. But Server Actions tie streaming to a single server-component lifecycle; we need fine-grained event types (`tool_call`, `prose_chunk`, `thesis`, `done`) rendered differently. ADR-008 already dispositions this. *Rejected (per ADR-008).*

## Anti-patterns

These are the specific traps this ADR prevents -- name them so a future contributor recognises the smell before re-introducing one:

1. **SSE inside an RSC boundary.** React Server Components produce a one-time HTML payload; SSE is a long-lived event stream. They do not compose. Any component that opens a `ReadableStream` reader must be a client component, all the way up through its enclosing layout segment. If you find yourself passing an `AsyncIterable` to a server component prop, stop -- you've crossed the boundary the wrong way.

2. **Forgetting `revalidate` on a server `fetch`.** Next.js 15 dropped default caching. A bare `fetch(URL)` in a server component re-hits the upstream on every navigation. Every server-side `fetch` in this app must explicitly state `next: { revalidate: N }` or `cache: "no-store"`; "I forgot" is the bug.

3. **Hardcoding the ticker list in `generateStaticParams` (or augmenting it client-side).** The canonical ticker set lives in `packages/shared/src/shared/tickers.py` (Python) and is exposed via `/api/v1/tickers`. `generateStaticParams` calls that endpoint at build time and uses the result verbatim -- no `[...await tickers, "SPY"]`-style extension, no string literals. Anything else is a copy of the registry that will drift the next time someone adds a ticker. If a ticker isn't in the API response, the fix is to add it to `shared.tickers`, not to splice it in on the frontend.

4. **`dynamic = "force-dynamic"` on a route whose data is daily-cadence.** Force-dynamic is for per-request data (auth-gated, user-specific). Daily-cadence data is ISR (or layout `fetch` with `revalidate`) plus a TTL bounded by the ingest cadence. Defaulting to force-dynamic is a CPU-cost tax that buys nothing.

5. **Treating "200 with empty array" as success.** Per QNT-55: `/search/news` and similar endpoints degrade to `[]` with a 200 when the upstream is unreachable, so "no results" and "service unavailable" look identical at the API boundary. The frontend must render them identically too -- empty state copy, no toast, no error banner. Differentiating them client-side requires extra signal the backend doesn't currently emit and probably never will.

6. **Chat panel as a route.** It tears down on every ticker navigation. Chat is part of `app/layout.tsx`, observes `usePathname()` to learn which ticker is active, and never owns the URL.

## Consequences

**Easier:**

- **Single source of truth per surface.** Each route / panel has exactly one rendering mode + cache directive named in code, matching this ADR by section. New contributors don't choose; they look up.
- **Free-tier-friendly.** ISR + `revalidate: 60` means Vercel caches the rendered HTML and re-uses it across the visitor population. FastAPI sees ~1 request per ticker per minute, not per navigation. Fits inside Hetzner CX41 + Vercel hobby tier comfortably.
- **The eval harness (QNT-67) and the chat panel hit the same SSE contract.** No frontend-specific transport, no SDK lock-in. Per ADR-008.
- **`generateStaticParams` from `/api/v1/tickers`.** When QNT-134 / future work adds a ticker, the build regenerates the static set automatically -- no frontend code change.
- **Anti-patterns section gives reviewers a checklist.** Phase-6 PRs can grep this list before merging.

**Harder:**

- **Every server `fetch` needs an explicit cache directive.** A grep in CI for `fetch\(.*\)\s*[^,]\)` (a `fetch` call with no second arg) catches the regression. Add this to the Phase 6 sanity-check.
- **`/ticker/[symbol]` ISR re-validation has a coordination cost.** If a ticker is removed from `shared.tickers`, the stale ISR page persists until next deploy. Acceptable -- removal is rare and a deploy is the natural reset.
- **Client-side toggles (chart range, indicator timeframe, fundamentals period) bypass the server cache.** Each toggle is a `cache: "no-store"` fetch. Negligible at our scale (10 tickers, single-digit visitors), but the chart can fire 5-10 ranges per session if a user is exploring; revisit if FastAPI shows hot endpoints.
- **The chat panel observes `usePathname()`, not a typed prop.** A future restructure that renames the route segment (`/ticker/[symbol]` -> `/t/[symbol]`) silently breaks the active-ticker inference unless the panel's path parser is updated alongside. Add a regex constant the panel and the route share.

## Revisit triggers

Reopen this ADR if any of these fire:

- Vercel build time exceeds 5 min from `generateStaticParams` (we have 11 tickers; this is hard to hit unless the report endpoints become slow).
- A new surface lands that has per-request data (auth-gated user view, real-time intraday) -- that is force-dynamic territory; document it as a §5 addition rather than amending the existing rules.
- Real-time / intraday tick stream is added (out of scope per `docs/design-frontend-plan.md`) -- forces a CSR-with-WebSocket surface.
- We add an authentication layer -- the watchlist + ticker page may move from ISR to SSR (per-user data).
- FastAPI proves to be the bottleneck even with `revalidate: 60` -- step up to Vercel KV / Edge Config caching.
- The `/api/v1/health` provenance shape changes (QNT-132) -- the ticker page provenance strip and the chat panel composer placeholder both consume it; coordinate the change.

## References

- ADR-005 -- Next.js + Vercel over Python-native frontend (the prior decision this ADR builds on).
- ADR-008 -- No Vercel AI SDK (covers the chat-panel transport choice).
- ADR-010 -- Dagster production topology (the backend "pre-decide before shipping" counterpart).
- ADR-011 -- LLM routing (the agent-layer "pre-decide before shipping" counterpart, drafted at the same time).
- `docs/design-frontend-plan.md` -- final Phase 6 scope after triaging the TERMINAL/NINE v2 mock; the surface inventory in this ADR matches that doc.
- `feedback_pre_design_cross_store_identity.md` -- the cross-store identity invariant heuristic used in each surface row.
- `feedback_vendor_prod_docs.md` -- read Next.js production-mode docs (`dynamic`, `revalidate`, `cache: "no-store"`, streaming) before writing components, not after.
- QNT-72 / QNT-73 / QNT-74 -- Phase 6 implementation tickets that reference this ADR by section.
- QNT-55 -- the empty-array-with-200 lesson informing anti-pattern #5.
- QNT-100 -> QNT-116, QNT-54 -> QNT-120 -- the production-vs-tutorial-gap incidents that motivated the pre-design pattern.
