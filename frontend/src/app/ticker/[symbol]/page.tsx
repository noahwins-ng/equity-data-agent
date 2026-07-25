/**
 * Ticker detail page (`/ticker/[symbol]`) — middle pane of the design v2 shell.
 *
 * Per ADR-014 §3 (revised QNT-168):
 *   - Statically rendered at build time. `dynamic = "force-static"` makes the
 *     intent explicit; every server fetch below uses the apiFetch default
 *     (`cache: "force-cache"`) so the responses are pinned into the build
 *     output. Freshness is driven by Vercel Deploy Hook calls from
 *     dagster_pipelines.vercel_deploy after each ingest cycle, not by ISR.
 *   - `generateStaticParams()` reads `/api/v1/tickers` at build time so the
 *     ticker universe is canonical (Anti-pattern §3, no hardcoded list).
 *   - Sub-deploy-cycle freshness on interactive surfaces (chart range,
 *     indicator timeframe, fundamentals period tabs) still comes from
 *     `cache: "no-store"` on those client fetches.
 *   - 200-with-empty is rendered the same as service-down (Anti-pattern §5).
 *   - QNT-424 carves one exception out of that rule for the *quote* fetch
 *     during a production build: a static page is written once and cannot
 *     self-repair, so an outage that masquerades as "unknown ticker" would be
 *     frozen into the deploy. Build-time infrastructure failures therefore
 *     fail the build (see `loadQuote` / Anti-pattern §7).
 */

import { notFound } from "next/navigation";

import { FundamentalsCard } from "@/components/ticker/fundamentals-card";
import { NewsCard } from "@/components/ticker/news-card";
import { PriceChart } from "@/components/ticker/price-chart";
import { ProvenanceStrip } from "@/components/ticker/provenance-strip";
import { QuoteHeader } from "@/components/ticker/quote-header";
import { TechnicalsCard } from "@/components/ticker/technicals-card";
import { MobileSectionTabs } from "@/components/mobile-section-tabs";
import {
  API_BASE_URL,
  ApiError,
  IS_PRERENDER,
  IS_PRODUCTION_PRERENDER,
  apiFetch,
  type NewsRow,
  type QuoteResponse,
} from "@/lib/api";

export const dynamic = "force-static";
// `dynamicParams = false` is required under SSG: with `force-static` the
// page cannot do request-time rendering, so leaving `dynamicParams` at its
// `true` default would error on unknown slugs instead of returning 404.
// New tickers become available on the next deploy after Dagster ingests
// them — same data-availability point as every other surface in the
// SSG + Vercel Deploy Hook model (QNT-168).
export const dynamicParams = false;

/**
 * QNT-424: retry a build-time fetch through a transient backend outage.
 *
 * A push to main fans out to two independent deploys — this Vercel build and
 * the Hetzner CD run that recreates the API container. The observed hole was
 * ~31 s (container `Recreate` at 09:36:29 to `Healthy` at 09:37:00).
 *
 * `vercel.json` + the CD deploy-hook step now order the two deploys so this
 * window should not exist at all; this is the belt-and-braces layer for any
 * residual blip (and for the Dagster ingest hook, which fires independently).
 *
 * Budget: attempts at 0/2/4/8/16 s — 30 s of backoff — each capped at 8 s.
 * Neither number is arbitrary:
 *
 *   - The per-attempt cap exists because a *hung* connection (TCP accepted,
 *     no response) never rejects, unlike the connection-refused case a
 *     container restart produces. Without it, one stalled attempt swallows the
 *     entire page-render window and the ladder below never runs — the retry
 *     policy would be decorative exactly when it is needed. "Sick but still up"
 *     containers are a real failure mode here (see ops-runbook.md).
 *   - The total is bounded by Next's `staticPageGenerationTimeout`, which kills
 *     a page render outright. Worst case here is 5x8 s + 30 s = ~70 s, so
 *     next.config.ts raises that ceiling to 120 s. An earlier 6-attempt/62 s
 *     ladder against the 60 s default had a final attempt that could never run.
 *     Change one of these three numbers and re-derive the other two.
 *
 * Next retries a failed page up to 3 times on top of this.
 *
 * Retries only during a prerender — at runtime a caller wants a fast answer,
 * not half a minute of backoff. A genuine 404 is an answer, not an outage, so
 * it short-circuits immediately.
 */
const BUILD_FETCH_ATTEMPTS = 5;
const BUILD_FETCH_TIMEOUT_MS = 8_000;

async function withBuildTimeRetry<T>(
  label: string,
  fn: () => Promise<T>,
): Promise<T> {
  let delayMs = 2_000;
  for (let attempt = 1; ; attempt++) {
    try {
      return await fn();
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) throw err;
      if (!IS_PRERENDER || attempt >= BUILD_FETCH_ATTEMPTS) throw err;
      console.warn(
        `[QNT-424] ${label} failed (attempt ${attempt}/${BUILD_FETCH_ATTEMPTS}), ` +
          `retrying in ${delayMs}ms: ${String(err)}`,
      );
      await new Promise((resolve) => setTimeout(resolve, delayMs));
      delayMs *= 2;
    }
  }
}

export async function generateStaticParams(): Promise<{ symbol: string }[]> {
  // Build-time fetch of the canonical universe from the API.
  //
  // QNT-424: an empty fallback here is *not* harmless — with
  // `dynamicParams = false`, returning [] ships a production deploy with zero
  // ticker routes, so all 10 pages 404. That degradation is correct only for a
  // preview build with no backend reachable; in production an unreadable
  // ticker universe must fail the build.

  // Announce whether the fail-loud guard is armed. `IS_PRODUCTION_PRERENDER`
  // keys off VERCEL_ENV, which Vercel only injects when the project has
  // "Enable access to System Environment Variables" checked — a dashboard
  // toggle, i.e. state this repo cannot declare. If it were ever unchecked the
  // guard would silently revert to the pre-QNT-424 behaviour, so print the
  // state into the build log rather than assume it.
  //
  // (Disabling it would also break the `ignoreCommand` in vercel.json, which
  // tests the same variable — production builds of non-frontend commits would
  // start being skipped. So the setting fails loudly before it fails silently.)
  if (IS_PRERENDER) {
    console.log(
      `[QNT-424] build-time guard ${IS_PRODUCTION_PRERENDER ? "ARMED" : "not armed"} ` +
        `(VERCEL_ENV=${process.env.VERCEL_ENV ?? "<unset>"}) — ` +
        `${IS_PRODUCTION_PRERENDER ? "API failures will fail this build" : "API failures degrade gracefully"}`,
    );
  }

  try {
    return await withBuildTimeRetry("/api/v1/tickers", async () => {
      const res = await fetch(`${API_BASE_URL}/api/v1/tickers`, {
        cache: "force-cache",
        signal: AbortSignal.timeout(BUILD_FETCH_TIMEOUT_MS),
      });
      if (!res.ok) {
        throw new ApiError(res.status, "/api/v1/tickers", await res.text());
      }
      const tickers = (await res.json()) as string[];
      // A 200 carrying an empty universe would slip past the `res.ok` check
      // above and still ship zero ticker routes — the exact outcome the catch
      // below refuses. ADR-014 anti-pattern §5 says don't *render* 200-empty
      // differently from service-down; it does not say accept it as the build
      // input. Treat it as a failure so it retries, then fails the build.
      if (tickers.length === 0) {
        throw new ApiError(200, "/api/v1/tickers", "empty ticker universe");
      }
      return tickers.map((symbol) => ({ symbol: symbol.toUpperCase() }));
    });
  } catch (err) {
    if (IS_PRODUCTION_PRERENDER) {
      throw new Error(
        `[QNT-424] Unable to read /api/v1/tickers during a production build — ` +
          `refusing to ship a deploy with no ticker pages. Cause: ${String(err)}`,
      );
    }
    if (IS_PRERENDER) return [];
    throw new Error("Unable to read /api/v1/tickers");
  }
}

async function loadQuote(ticker: string): Promise<QuoteResponse | null> {
  try {
    return await withBuildTimeRetry(`/api/v1/quote/${ticker}`, () =>
      apiFetch<QuoteResponse>(`/api/v1/quote/${ticker}`, {
        signal: AbortSignal.timeout(BUILD_FETCH_TIMEOUT_MS),
      }),
    );
  } catch (err) {
    // A 404 is the API positively answering "no such ticker" — render the
    // not-found page, which is what that genuinely means.
    if (err instanceof ApiError && err.status === 404) return null;
    // Anything else (connection refused, 5xx, timeout) is infrastructure
    // failing, not a verdict about the ticker. QNT-424: during a production
    // prerender that distinction is the whole ballgame — swallowing it here is
    // what baked "Ticker not found" into 8 live pages. Fail the build instead.
    if (IS_PRODUCTION_PRERENDER) throw err;
    return null;
  }
}

async function loadNews(ticker: string): Promise<NewsRow[]> {
  try {
    return await apiFetch<NewsRow[]>(`/api/v1/news/${ticker}?days=7&limit=25`);
  } catch {
    return [];
  }
}

async function loadLogo(ticker: string): Promise<string | null> {
  // The logos endpoint returns the full ticker→URL map; the watchlist
  // already fetches it on every navigation so this hits the same Next
  // Data Cache key (force-cache) — no extra API roundtrip per build.
  try {
    const logos = await apiFetch<Record<string, string | null>>("/api/v1/logos");
    return logos[ticker] ?? null;
  } catch {
    return null;
  }
}

type Params = Promise<{ symbol: string }>;

export default async function TickerDetailPage({ params }: { params: Params }) {
  const { symbol } = await params;
  const ticker = symbol.toUpperCase();

  const [quote, news, logoUrl] = await Promise.all([
    loadQuote(ticker),
    loadNews(ticker),
    loadLogo(ticker),
  ]);

  // A null quote now means one of exactly two things, both of which should
  // 404: the API positively answered 404 (unknown ticker), or a non-production
  // render hit an outage (QNT-424 — a production *build* throws instead of
  // reaching here, so a transient failure can never be frozen into the HTML).
  // Better to 404 cleanly than render half a page with empty stats.
  if (!quote) {
    notFound();
  }

  return (
    <div className="flex h-full flex-col">
      <QuoteHeader quote={quote} logoUrl={logoUrl} />
      <PriceChart ticker={ticker} />
      <MobileSectionTabs
        technicals={<TechnicalsCard ticker={ticker} />}
        fundamentals={<FundamentalsCard ticker={ticker} currentPrice={quote.price} />}
        news={<NewsCard items={news} />}
      />
      <ProvenanceStrip />
    </div>
  );
}
