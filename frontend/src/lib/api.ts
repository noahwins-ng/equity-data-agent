/**
 * Typed fetch wrapper for the FastAPI backend.
 *
 * Per ADR-014 Anti-pattern #2: every server-side fetch must declare a cache
 * directive. Bare fetch(URL) is forbidden — Next.js 15+ dropped default
 * caching, so an unannotated fetch re-hits the API on every navigation.
 *
 * QNT-168: the default for server-side fetches is now `cache: "force-cache"`
 * because /ticker/[symbol] is statically rendered and Dagster triggers a
 * Vercel Deploy Hook on every successful ingest cycle (see
 * dagster_pipelines.vercel_deploy). Build time = freshness time. ISR is
 * gone; `next: { revalidate }` is no longer used here.
 *
 * Usage:
 *   const data = await apiFetch<DashboardSummary>("/api/v1/dashboard/summary");
 *   // SSE / per-request: opt out with `cache: "no-store"`.
 *   const res = await apiFetchRaw("/api/v1/agent/chat", { cache: "no-store", method: "POST", body });
 *
 * Cache vocabulary:
 *   - (default)            → cache: "force-cache" (build-time pin, deploy-hook driven)
 *   - cache: "no-store"    → per-request (SSE, status indicators, client toggles)
 *   - cache: "force-cache" → explicit form of the default
 */

// Default to 127.0.0.1 (not "localhost") so browser fetches reach the API
// reliably on macOS — `localhost` resolves to both IPv4 and IPv6, and Chrome's
// fetch may try `::1` first; uvicorn binds IPv4-only by default. Override via
// NEXT_PUBLIC_API_URL in any prod / preview deploy where the API lives off-host.
const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export type ApiFetchOptions = Omit<RequestInit, "cache"> & {
  /** Override Next's data cache (e.g. "no-store" for SSE / per-request data). */
  cache?: RequestCache;
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

const inFlightJson = new Map<string, Promise<unknown>>();

function inFlightKey(path: string, options: ApiFetchOptions): string | null {
  if (typeof window === "undefined") return null;
  if (options.body !== undefined) return null;
  if (options.signal !== undefined) return null;
  const method = options.method?.toUpperCase() ?? "GET";
  if (method !== "GET") return null;
  return JSON.stringify({
    path,
    cache: options.cache ?? "force-cache",
    credentials: options.credentials,
    headers: options.headers,
  });
}

/**
 * Fetch JSON from the API with explicit cache semantics.
 * Throws ApiError on non-2xx responses.
 */
export async function apiFetch<T>(
  path: string,
  options: ApiFetchOptions = {},
): Promise<T> {
  const key = inFlightKey(path, options);
  if (key) {
    const existing = inFlightJson.get(key);
    if (existing) return existing as Promise<T>;
  }

  const request = apiFetchRaw(path, options).then(async (response) => {
    if (!response.ok) {
      const body = await response.text().catch(() => "<no body>");
      throw new ApiError(response.status, path, body);
    }
    return response.json() as Promise<T>;
  });

  if (!key) return request;

  inFlightJson.set(key, request);
  return request.finally(() => inFlightJson.delete(key));
}

/**
 * Fetch raw Response from the API (use for SSE / streaming endpoints).
 * Caller is responsible for inspecting `response.ok` and reading the body.
 */
export async function apiFetchRaw(
  path: string,
  options: ApiFetchOptions = {},
): Promise<Response> {
  const { cache, ...rest } = options;
  const init: RequestInit = { ...rest, cache: cache ?? "force-cache" };
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
// must stay aligned with the FastAPI endpoints by hand — this contract is
// deliberately hand-maintained, not codegen'd.
//
// QNT-384: an earlier plan called for `make types` (OpenAPI → TS via
// openapi-typescript). It was dropped because it can't actually cover this
// file: the data endpoints return raw `list[dict[str, Any]]` with no
// `response_model`, so their shapes never reach the OpenAPI schema, and the
// dominant surface below — the SSE streaming chat events (RetrievedSource,
// RetrievedSourcesEvent, DoneEvent, …) — is not expressible in OpenAPI at all.
// So these types are the source of truth: when a `data.py` endpoint or an SSE
// event shape changes, update the matching type here. Off-schema fields (e.g.
// RetrievedSource.corpus below) are called out inline where they occur.

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
  // QNT-209: opaque per-(session, ticker) memory key. Generated on the
  // frontend via crypto.randomUUID() inside ChatPanel; lives in component
  // state only — refresh discards it and the agent sees no prior turn.
  // Omitted on tests / non-frontend callers; backend then runs ephemeral
  // (no checkpointer, no sidecar touch).
  thread_id?: string;
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
  // QNT-252: server clock (seconds) captured at the matching tool_call. Echoed
  // back on the result so the panel can bind it to the exact call row even when
  // two concurrent calls to the same tool are in flight (see bindToolResult).
  started_at: number;
};

export type ProseChunkEvent = {
  delta: string;
};

// QNT-211: token-level deltas from the narrate node. Streamed AS the graph
// runs (between the structured payload composing and the post-graph
// thesis/quick_fact/etc. event firing) so the frontend can render a
// 1-4 sentence analyst-voice prose bubble ABOVE the structured card
// while the card itself is still being assembled.
export type NarrativeChunkEvent = {
  delta: string;
};

// QNT-298: the thesis planner's (or exploration's) analyst-voice rationale
// sentence, streamed as soon as the plan resolves -- BEFORE gather's tool
// calls land for a fresh thesis, right after them for exploration (which
// gathers inline in one node). Absent turns (quick_fact/focused/comparison
// plans carry no rationale) emit nothing.
export type PlanRationaleEvent = {
  text: string;
};

// QNT-208 v2: final verdict is a closed three-state set.
export type Verdict = "Overweight" | "Neutral" | "Underweight";

// QNT-208 v2: per-aspect label. Fundamental: Premium / Inline / Discounted.
// Technical: Uptrend / Sideways / Downtrend. Company and News are
// narrative-only (label === null).
export type AspectLabel =
  | "Premium"
  | "Inline"
  | "Discounted"
  | "Uptrend"
  | "Sideways"
  | "Downtrend";

// QNT-208 v2: one aspect inside a four-aspect Thesis or ComparisonSection.
export type AspectView = {
  label: AspectLabel | null;
  summary: string;
  supports: string[];
  challenges: string[];
};

// QNT-208 v2: thesis is four aspect blocks + verdict + rationale.
// v1 list/stance fields removed in this milestone.
export type ThesisPayload = {
  company: AspectView;
  fundamental: AspectView;
  technical: AspectView;
  news: AspectView;
  verdict: Verdict;
  verdict_rationale: string;
};

// QNT-149 / QNT-156: classifier picks one of these shapes for each run.
// The panel uses ``intent`` to swap layout (thesis card vs. compact
// quick-fact / comparison / conversational) and the matching payload is
// delivered on the corresponding event. ``conversational`` also serves as
// the deterministic fallback when ANY synthesize path fails -- the panel
// always renders an in-domain reply, never a blank state or stack trace.
// QNT-176: ``fundamental`` / ``technical`` / ``news`` are the
// focused-analysis intents (QNT-208 renamed ``news_sentiment`` -> ``news``).
// QNT-209: ``followup`` reuses the QuickFactAnswer schema so the panel
// renders it through the existing quick-fact card.
export type Intent =
  | "thesis"
  | "quick_fact"
  | "comparison"
  | "conversational"
  | "fundamental"
  | "technical"
  | "news"
  | "followup"
  // QNT-220 follow-up: broad anchored exploratory scans. Set by
  // explore_supervisor (never the classifier); renders the exploration card.
  | "exploration";

export type FocusKind = "fundamental" | "technical" | "news";

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
//
// QNT-208 v2: each section carries four AspectView blocks mirroring the
// thesis card (was: a single ``key_values`` list).
export type ComparisonSource = "company" | "technical" | "fundamental" | "news";

// QNT-358: the non-company aspects are optional — an axis-focused comparison
// ("compare TSLA vs AMD on technical momentum") narrows the plan to
// company + one axis for both tickers, so the other aspects arrive null. The
// card renders only the aspects present. company stays required (grounding).
export type ComparisonSection = {
  ticker: string;
  company: AspectView;
  fundamental: AspectView | null;
  technical: AspectView | null;
  news: AspectView | null;
};

export type ComparisonPayload = {
  sections: ComparisonSection[];
  differences: string;
};

// QNT-224: lean N-way comparison (3-4 tickers). Distinct from the rich
// two-ticker ComparisonPayload above — a compact metrics row per ticker
// (pre-formatted strings; math in SQL, formatted in the API) rendered as a
// table. Delivered on its own ``comparison_lean`` event; the qualitative
// contrast arrives as the narrate bubble, so there is no differences field.
export type LeanComparisonRow = {
  ticker: string;
  pe: string;
  rsi: string;
  net_margin: string;
  price: string;
  // QNT-224 follow-up: interpretive verdicts from the fundamental + technical
  // reports, rendered as colored pills. null when the report suppressed it.
  // Trend is split by timeframe (daily = short-term, weekly = medium-term).
  valuation_label: AspectLabel | null;
  trend_daily: AspectLabel | null;
  trend_weekly: AspectLabel | null;
};

export type LeanComparisonPayload = {
  rows: LeanComparisonRow[];
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

// QNT-176: focused-analysis response shape. Mirrors
// ``agent.focused.FocusedAnalysis``. ``focus`` selects the card accent;
// the body fields are identical across all three focuses.
export type FocusedSource = "company" | "technical" | "fundamental" | "news";

export type FocusedValue = {
  label: string;
  value: string;
  source: FocusedSource;
};

// QNT-208 v2: per-focus verdict. For focus=fundamental: Premium / Inline /
// Discounted. For focus=technical: Uptrend / Sideways / Downtrend. For
// focus=news: null (the catalyst fields carry the payload instead).
export type FocusedVerdict =
  | "Premium"
  | "Inline"
  | "Discounted"
  | "Uptrend"
  | "Sideways"
  | "Downtrend";

export type FocusedAnalysisPayload = {
  focus: FocusKind;
  summary: string;
  key_points: string[];
  cited_values: FocusedValue[];
  verdict: FocusedVerdict | null;
  // News-focus fields (null/empty for other focuses).
  existing_development: string | null;
  positive_catalysts: string[];
  negative_catalysts: string[];
};

// QNT-220 follow-up: exploration-scan response shape. Mirrors
// ``agent.exploration.ExplorationAnswer``. A verdict-free, multi-lens scan —
// a headline of what stands out, cross-lens observations, and verbatim cited
// value chips. No verdict and no forward "watch next" by contract (no report
// carries dated catalysts to copy from).
export type ExplorationSource = "company" | "technical" | "fundamental" | "news";

export type ExplorationValue = {
  label: string;
  value: string;
  source: ExplorationSource;
};

export type ExplorationAnswerPayload = {
  headline: string;
  observations: string[];
  cited_values: ExplorationValue[];
};

// QNT-226: one retrieved article surfaced by the agent's semantic news
// search. Mirrors the rows the `/search/news` endpoint returns (minus the
// score + body the prompt uses) — the provenance list shows what RAG found.
// QNT-301: `id` is the stable claim-anchor tag (`R1`, `R2`, ... in list order)
// the agent stamps on each hit; an inline citation `(source: news R1)` links to
// the row carrying that id. Optional so a pre-QNT-301 cached run still parses.
export type RetrievedSource = {
  id?: string;
  headline: string;
  source: string;
  date: string;
  url: string;
  // QNT-263: the Qdrant corpus this hit came from ("news" | "earnings"). Emitted
  // as a raw dict field (not part of the OpenAPI schema), so it is declared here
  // by hand. QNT-305 follow-up: the prose parser reads it to de-anchor a citation
  // whose source name does not match the id's corpus (e.g. `fundamental R1` on a
  // news row).
  corpus?: string;
};

// QNT-226: emitted once after the graph completes when a targeted news ask
// surfaced semantic-search hits. The panel renders a compact clickable
// "Retrieved sources" list. Absent when no search ran this turn.
export type RetrievedSourcesEvent = {
  sources: RetrievedSource[];
};

export type DoneEvent = {
  tools_count: number;
  citations_count: number;
  confidence: number;
  grounding_rate?: number;
  grounding_unsupported?: string[];
  intent?: Intent;
  supervisor_iterations?: number;
  // QNT-209: echoed by the backend for confirmation. Null when the request
  // omitted thread_id (ephemeral path).
  thread_id?: string | null;
  // QNT-298: 2-3 deterministic follow-up chips under the landed analytical
  // card (thesis / quick_fact / comparison / focused / exploration). Empty
  // for conversational/followup turns and for any turn whose card failed to
  // land (synthesize-failure conversational fallback carries its own
  // suggestions instead).
  suggestions?: string[];
  // QNT-299: the ticker the agent actually resolved "it" to this turn --
  // may differ from the page ticker on a rebase (e.g. "compare to AAPL"
  // while on /ticker/NVDA). Source for the composer's context-anchor chip.
  analysis_ticker?: string;
  // QNT-299: bare report-tool names that degraded this turn -- a required
  // tool error, or an optional tool (news) silently dropped after retry
  // exhaustion. Empty when nothing degraded.
  degraded_tools?: string[];
  // QNT-299: the oldest AS_OF footer date among this turn's gathered
  // reports (ISO "YYYY-MM-DD"), i.e. the staleness bottleneck for the
  // answer. null/absent when no gathered report carried a parseable footer.
  data_as_of?: string | null;
};

export type ChatErrorEvent = {
  detail: string;
  code: string;
};
