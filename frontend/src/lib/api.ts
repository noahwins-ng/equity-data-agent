/**
 * Typed fetch wrapper for the FastAPI backend.
 *
 * Per ADR-014 Anti-pattern #2: every server-side fetch must declare a cache
 * directive. Bare fetch(URL) is forbidden — Next.js 15+ dropped default
 * caching, so an unannotated fetch re-hits the API on every navigation.
 *
 * Usage:
 *   const data = await apiFetch<DashboardSummary>("/api/v1/dashboard/summary");
 *   // SSE: use apiFetchRaw to keep the Response body as a ReadableStream.
 *   const res = await apiFetchRaw("/api/v1/agent/chat", { cache: "no-store", method: "POST", body });
 *
 * Cache vocabulary:
 *   - revalidate: number  → ISR / Data Cache TTL in seconds (default for daily data)
 *   - cache: "no-store"   → opt out (SSE, per-request data, client toggles)
 *   - cache: "force-cache" → cache indefinitely (rare)
 */

// Default to 127.0.0.1 (not "localhost") so browser fetches reach the API
// reliably on macOS — `localhost` resolves to both IPv4 and IPv6, and Chrome's
// fetch may try `::1` first; uvicorn binds IPv4-only by default. Override via
// NEXT_PUBLIC_API_URL in any prod / preview deploy where the API lives off-host.
const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

/**
 * Default revalidation window for daily-cadence data.
 *
 * Underlying data is EOD-cadence (02:00 ET Dagster ingest), so the TTL is
 * pinned at 24 h — the highest value that's still semantically equivalent
 * to "as fresh as the data can possibly be." The original 60 s default
 * burned ~150K Vercel ISR Writes/30d (QNT-166) for data that changes once
 * per day; an interim 1 h fix cut the rate ~60x but still allowed ~24
 * regenerations/day. Pinning at 24 h matches the existing logos endpoint
 * and gives ~1 regeneration/page/day. Pages that need sub-hour freshness
 * (charts, technicals timeframe toggles) bypass this via `cache: "no-store"`.
 */
export const DEFAULT_REVALIDATE_SECONDS = 86_400;

export type ApiFetchOptions = Omit<RequestInit, "cache"> & {
  /** ISR / Data Cache TTL in seconds. Defaults to DEFAULT_REVALIDATE_SECONDS (24 hours). Mutually exclusive with `cache`. */
  revalidate?: number;
  /** Override Next's data cache (e.g. "no-store" for SSE). Mutually exclusive with `revalidate`. */
  cache?: RequestCache;
  /** Cache tags for on-demand revalidation via revalidateTag(). */
  tags?: string[];
};

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly path: string,
    message: string,
  ) {
    super(`[${status}] ${path}: ${message}`);
    this.name = "ApiError";
  }
}

/**
 * Fetch JSON from the API with explicit cache semantics.
 * Throws ApiError on non-2xx responses.
 */
export async function apiFetch<T>(
  path: string,
  options: ApiFetchOptions = {},
): Promise<T> {
  const response = await apiFetchRaw(path, options);
  if (!response.ok) {
    const body = await response.text().catch(() => "<no body>");
    throw new ApiError(response.status, path, body);
  }
  return response.json() as Promise<T>;
}

/**
 * Fetch raw Response from the API (use for SSE / streaming endpoints).
 * Caller is responsible for inspecting `response.ok` and reading the body.
 */
export async function apiFetchRaw(
  path: string,
  options: ApiFetchOptions = {},
): Promise<Response> {
  const { revalidate, cache, tags, ...rest } = options;

  if (revalidate !== undefined && cache !== undefined) {
    throw new Error(
      "apiFetch: pass either `revalidate` or `cache`, not both — they conflict.",
    );
  }

  const init: RequestInit = { ...rest };

  if (cache !== undefined) {
    init.cache = cache;
  } else {
    init.next = {
      revalidate: revalidate ?? DEFAULT_REVALIDATE_SECONDS,
      ...(tags ? { tags } : {}),
    };
  }

  return fetch(`${API_BASE_URL}${path}`, init);
}

export { API_BASE_URL };

/**
 * Build-time prerender flag — true when running `next build` so server fetches
 * inside `generateStaticParams` / page components can fall back gracefully if
 * FastAPI is unreachable. Without this, a Vercel preview build for a PR that
 * only touches the frontend would fail because the prod API is firewalled to
 * a different origin during CI. At runtime the flag is false and missing data
 * surfaces normally.
 */
export const IS_PRERENDER =
  process.env.NEXT_PHASE === "phase-production-build" ||
  process.env.NEXT_PHASE === "phase-export";

// ─── Typed response shapes ──────────────────────────────────────────────────
//
// These mirror packages/api/src/api/routers/data.py. Field names + nullability
// must stay aligned with the FastAPI response models — `make types` is the
// long-term plan; for now we hand-maintain the surfaces the ticker page reads.

export type Timeframe = "daily" | "weekly" | "monthly";
export type PeriodType = "quarterly" | "annual" | "ttm";

export type OhlcvRow = {
  time: string; // ISO date, YYYY-MM-DD
  open: number;
  high: number;
  low: number;
  close: number;
  adj_close: number;
  volume: number;
};

export type IndicatorRow = {
  time: string;
  sma_20: number | null;
  sma_50: number | null;
  sma_200: number | null;
  ema_12: number | null;
  ema_26: number | null;
  rsi_14: number | null;
  macd: number | null;
  macd_signal: number | null;
  macd_hist: number | null;
  macd_bullish_cross: number; // 0/1 flag
  bb_upper: number | null;
  bb_middle: number | null;
  bb_lower: number | null;
  bb_pct_b: number | null;
  adx_14: number | null;
  atr_14: number | null;
  obv: number | null;
};

export type FundamentalRow = {
  ticker: string;
  period_end: string;
  period_type: PeriodType;
  pe_ratio: number | null;
  ev_ebitda: number | null;
  price_to_book: number | null;
  price_to_sales: number | null;
  eps: number | null;
  revenue_yoy_pct: number | null;
  net_income_yoy_pct: number | null;
  fcf_yoy_pct: number | null;
  net_margin_pct: number | null;
  gross_margin_pct: number | null;
  ebitda_margin_pct: number | null;
  gross_margin_bps_yoy: number | null;
  net_margin_bps_yoy: number | null;
  roe: number | null;
  roa: number | null;
  fcf_yield: number | null;
  debt_to_equity: number | null;
  current_ratio: number | null;
  revenue_ttm: number | null;
  net_income_ttm: number | null;
  fcf_ttm: number | null;
  // Absolute period values from equity_raw.fundamentals — populated for
  // quarterly + annual rows; null on TTM rows (use *_ttm fields for those).
  revenue: number | null;
  net_income: number | null;
  free_cash_flow: number | null;
  ebitda: number | null;
};

export type NewsRow = {
  id: string;
  headline: string;
  body: string;
  publisher_name: string;
  image_url: string;
  url: string;
  source: string;
  published_at: string; // ISO datetime
  sentiment_label: string;
  // Canonical publisher label, computed server-side once (QNT-148 / ADR-016):
  // prefers the resolved outlet from a Finnhub redirect, falls back to the
  // direct URL host, falls back to `publisher_name`, finally empty string.
  // The card renders `publisher || "—"` with no further fallback chain.
  publisher: string;
};

export type QuoteResponse = {
  ticker: string;
  name: string;
  sector: string | null;
  industry: string | null;
  price: number | null;
  prev_close: number | null;
  open: number | null;
  day_high: number | null;
  day_low: number | null;
  volume: number | null;
  avg_volume_30d: number | null;
  market_cap: number | null;
  pe_ratio_ttm: number | null;
  as_of: string | null;
};

export type HealthProvenance = {
  sources: string[];
  jobs: {
    runtime: string;
    schedule: string;
    next_ingest_local: string;
  };
};

export type HealthResponse = {
  status: "ok" | "degraded" | "down";
  provenance?: HealthProvenance;
};

// ─── Agent chat (QNT-74) ────────────────────────────────────────────────────
//
// Mirrors `packages/api/src/api/routers/agent_chat.py` SSE event payloads.
// The chat panel parses raw SSE frames into these typed shapes — one place
// owns the contract so a router-side change surfaces as a TypeScript error
// in the panel rather than a silent UI drift.

export type ChatRequest = {
  ticker: string;
  message: string;
  tools_enabled?: boolean;
  cite_sources?: boolean;
};

export type ToolCallEvent = {
  name: string;
  label: string;
  args: Record<string, unknown>;
  started_at: number;
};

export type ToolResultEvent = {
  name: string;
  label: string;
  latency_ms: number;
  summary: string;
  ok: boolean;
};

export type ProseChunkEvent = {
  delta: string;
};

export type VerdictStance = "constructive" | "cautious" | "negative" | "mixed";

export type ThesisPayload = {
  setup: string;
  bull_case: string[];
  bear_case: string[];
  verdict_stance: VerdictStance;
  verdict_action: string;
};

// QNT-149 / QNT-156: classifier picks one of these shapes for each run.
// The panel uses ``intent`` to swap layout (thesis card vs. compact
// quick-fact / comparison / conversational) and the matching payload is
// delivered on the corresponding event. ``conversational`` also serves as
// the deterministic fallback when ANY synthesize path fails — the panel
// always renders an in-domain reply, never a blank state or stack trace.
export type Intent =
  | "thesis"
  | "quick_fact"
  | "comparison"
  | "conversational";

export type IntentEvent = {
  intent: Intent;
};

export type QuickFactSource = "technical" | "fundamental" | "news";

export type QuickFactPayload = {
  answer: string;
  cited_value: string;
  source: QuickFactSource | null;
};

// QNT-156: comparison response shape. Two per-ticker sections (in user-named
// order) plus a qualitative differences paragraph. No cross-ticker numeric
// claims by contract — every cited value comes verbatim from one ticker's
// reports.
export type ComparisonSource = "technical" | "fundamental" | "news";

export type ComparisonValue = {
  label: string;
  value: string;
  source: ComparisonSource;
};

export type ComparisonSection = {
  ticker: string;
  summary: string;
  key_values: ComparisonValue[];
};

export type ComparisonPayload = {
  sections: ComparisonSection[];
  differences: string;
};

// QNT-156: conversational response shape — short prose answer + an
// optional list of 3 example questions. Used both for greetings/off-domain
// asks AND as the deterministic fallback when any other intent fails to
// produce its primary payload (no reports gathered, structured-output
// crash, comparison parser couldn't find two tickers).
export type ConversationalPayload = {
  answer: string;
  suggestions: string[];
};

export type DoneEvent = {
  tools_count: number;
  citations_count: number;
  confidence: number;
  intent?: Intent;
};

export type ChatErrorEvent = {
  detail: string;
  code: string;
};
