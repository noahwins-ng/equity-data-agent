/**
 * Ticker detail page (`/ticker/[symbol]`) — middle pane of the design v2 shell.
 *
 * Per ADR-014 §3:
 *   - ISR via `generateStaticParams()` reading `/api/v1/tickers` at build time
 *     — no hardcoded universe (Anti-pattern §3).
 *   - `revalidate: 3600` on every server fetch — data is EOD-cadence
 *     (02:00 ET Dagster ingest), so a 1-hour TTL still surfaces fresh data
 *     within the freshness budget while keeping ISR Writes proportional to
 *     the daily change rate (QNT-166). Sub-hour freshness on interactive
 *     surfaces (chart range, indicator timeframe, fundamentals period tabs)
 *     comes from `cache: "no-store"` on those client fetches.
 *   - 200-with-empty is rendered the same as service-down (Anti-pattern §5).
 */

import { notFound } from "next/navigation";

import { FundamentalsCard } from "@/components/ticker/fundamentals-card";
import { NewsCard } from "@/components/ticker/news-card";
import { PriceChart } from "@/components/ticker/price-chart";
import { ProvenanceStrip } from "@/components/ticker/provenance-strip";
import { QuoteHeader } from "@/components/ticker/quote-header";
import { TechnicalsCard } from "@/components/ticker/technicals-card";
import {
  API_BASE_URL,
  IS_PRERENDER,
  apiFetch,
  type HealthResponse,
  type NewsRow,
  type QuoteResponse,
} from "@/lib/api";

// ISR window for the route segment — same value as the cached fetches inside.
// 1 hour matches the EOD freshness budget (data updates once per day at
// 02:00 ET) and prevents ISR Writes from outpacing the underlying change
// rate. See QNT-166 for the prior 60 s overrun.
export const revalidate = 3600;
// `dynamicParams = false` would 404 any ticker not enumerated in
// `generateStaticParams`; we want graceful 404s for typos but allow new
// tickers to render lazily on the first request after a registry change.
// Defaulting to `true` (Next.js default) achieves both.

export async function generateStaticParams(): Promise<{ symbol: string }[]> {
  // Build-time fetch of the canonical universe from the API. If the API is
  // unreachable during build (preview deploy without backend), fall back to
  // an empty array — Next.js then renders pages on demand.
  try {
    const res = await fetch(`${API_BASE_URL}/api/v1/tickers`, {
      next: { revalidate },
    });
    if (!res.ok) return [];
    const tickers = (await res.json()) as string[];
    return tickers.map((symbol) => ({ symbol: symbol.toUpperCase() }));
  } catch {
    if (IS_PRERENDER) return [];
    throw new Error("Unable to read /api/v1/tickers");
  }
}

async function loadQuote(ticker: string): Promise<QuoteResponse | null> {
  try {
    return await apiFetch<QuoteResponse>(`/api/v1/quote/${ticker}`, { revalidate });
  } catch {
    return null;
  }
}

async function loadNews(ticker: string): Promise<NewsRow[]> {
  try {
    return await apiFetch<NewsRow[]>(`/api/v1/news/${ticker}?days=7&limit=25`, {
      revalidate,
    });
  } catch {
    return [];
  }
}

async function loadProvenance(): Promise<HealthResponse["provenance"] | null> {
  try {
    const health = await apiFetch<HealthResponse>("/api/v1/health", {
      revalidate: 300,
    });
    return health.provenance ?? null;
  } catch {
    return null;
  }
}

async function loadLogo(ticker: string): Promise<string | null> {
  // The logos endpoint returns the full ticker→URL map; the watchlist
  // already fetches it on every navigation so this hits the same Next
  // Data Cache key (24h TTL) — no extra API roundtrip in steady state.
  try {
    const logos = await apiFetch<Record<string, string | null>>("/api/v1/logos", {
      revalidate: 86_400,
    });
    return logos[ticker] ?? null;
  } catch {
    return null;
  }
}

type Params = Promise<{ symbol: string }>;

export default async function TickerDetailPage({ params }: { params: Params }) {
  const { symbol } = await params;
  const ticker = symbol.toUpperCase();

  const [quote, news, provenance, logoUrl] = await Promise.all([
    loadQuote(ticker),
    loadNews(ticker),
    loadProvenance(),
    loadLogo(ticker),
  ]);

  // Quote 404 → unknown ticker. Treat anything else (e.g. transient API
  // outage that returns null) the same way per anti-pattern §5 — better to
  // 404 cleanly than render half a page with empty stats.
  if (!quote) {
    notFound();
  }

  return (
    <div className="flex h-full flex-col">
      <QuoteHeader quote={quote} logoUrl={logoUrl} />
      <PriceChart ticker={ticker} />
      {/*
        The 3-card row fills whatever vertical space is left below the chart
        and lets each card scroll independently. `min-h-0` on the grid track
        is the magic incantation: without it the implicit `min-height: auto`
        on flex items lets the news card's tall content push the page taller
        than the viewport, which is exactly the awkward scrolling we want to
        avoid.
      */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 overflow-hidden px-6 py-2 lg:grid-cols-3">
        <TechnicalsCard ticker={ticker} />
        <FundamentalsCard ticker={ticker} currentPrice={quote.price} />
        <NewsCard items={news} />
      </div>
      <ProvenanceStrip provenance={provenance ?? null} />
    </div>
  );
}
