/**
 * Left-rail watchlist (server component, persistent across all routes).
 *
 * Per ADR-014 §1:
 *   - Lives in `app/layout.tsx`, not in any single page.
 *   - Server fetch via Next.js Data Cache, `revalidate: 60` (collates
 *     concurrent requests during a navigation burst, EOD freshness).
 *   - Ticker universe comes from `/api/v1/tickers` only — never hardcoded,
 *     never augmented client-side (Anti-pattern #3).
 *   - Both endpoints fail soft: a network error renders a "watchlist
 *     unavailable" banner rather than tearing down the layout.
 *
 * The status footer reads `/api/v1/health` provenance (QNT-132) so the
 * `EOD · 02:00 ET` line tracks the actual Dagster schedule. If `/health`
 * is down the footer falls back to a static "EOD" label rather than
 * blocking render.
 */

import Link from "next/link";

import { apiFetch } from "@/lib/api";

import { Sparkline } from "./sparkline";

type DashboardRow = {
  ticker: string;
  name: string;
  price: number;
  daily_change_pct: number | null;
  rsi_14: number | null;
  rsi_signal: string;
  trend_status: string;
  sparkline: number[];
};

type HealthProvenance = {
  jobs?: {
    next_ingest_local?: string;
  };
};

type HealthResponse = {
  provenance?: HealthProvenance;
};

const POSITIVE_STROKE = "#22c55e"; // tailwind emerald-500
const NEGATIVE_STROKE = "#ef4444"; // tailwind red-500
const NEUTRAL_STROKE = "#71717a"; // tailwind zinc-500

function formatPrice(price: number): string {
  return price.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatChange(pct: number | null): string {
  if (pct === null) return "—";
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct.toFixed(2)}%`;
}

function changeColorClass(pct: number | null): string {
  if (pct === null || pct === 0) return "text-zinc-400";
  return pct > 0 ? "text-emerald-400" : "text-red-400";
}

function sparklineStroke(pct: number | null): string {
  if (pct === null || pct === 0) return NEUTRAL_STROKE;
  return pct > 0 ? POSITIVE_STROKE : NEGATIVE_STROKE;
}

async function loadWatchlistData(): Promise<{
  rows: DashboardRow[];
  tickers: string[];
  error: string | null;
}> {
  try {
    const [tickers, summaryRows] = await Promise.all([
      apiFetch<string[]>("/api/v1/tickers"),
      apiFetch<DashboardRow[]>("/api/v1/dashboard/summary"),
    ]);
    // Render in /tickers order (the canonical universe), filling from
    // /summary by ticker. Any /tickers entry without a /summary row still
    // shows symbol + name with placeholder data — the alternative
    // (omit it) would silently disagree with the canonical universe.
    const summaryByTicker = new Map(summaryRows.map((row) => [row.ticker, row]));
    const rows = tickers.map<DashboardRow>(
      (ticker) =>
        summaryByTicker.get(ticker) ?? {
          ticker,
          name: ticker,
          price: Number.NaN,
          daily_change_pct: null,
          rsi_14: null,
          rsi_signal: "neutral",
          trend_status: "neutral",
          sparkline: [],
        },
    );
    return { rows, tickers, error: null };
  } catch (err) {
    const message = err instanceof Error ? err.message : "unknown error";
    return { rows: [], tickers: [], error: message };
  }
}

async function loadNextIngestLabel(): Promise<string> {
  try {
    const health = await apiFetch<HealthResponse>("/api/v1/health", {
      revalidate: 300,
    });
    return health.provenance?.jobs?.next_ingest_local ?? "—";
  } catch {
    return "—";
  }
}

export async function Watchlist() {
  const [{ rows, tickers, error }, nextIngest] = await Promise.all([
    loadWatchlistData(),
    loadNextIngestLabel(),
  ]);

  return (
    <aside
      aria-label="Watchlist"
      className="flex h-full flex-col border-r border-zinc-800 bg-zinc-950 text-zinc-100"
    >
      <header className="flex items-baseline justify-between border-b border-zinc-800 px-4 py-3">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-300">
          Watchlist · {tickers.length}
        </h2>
      </header>

      {error ? (
        <p className="px-4 py-6 text-xs text-red-400" role="alert">
          Watchlist unavailable.
        </p>
      ) : (
        <ul className="flex-1 divide-y divide-zinc-800/60 overflow-y-auto">
          {rows.map((row) => (
            <li key={row.ticker}>
              <Link
                href={`/ticker/${row.ticker}`}
                className="flex items-center gap-3 px-4 py-2 transition hover:bg-zinc-900 focus:bg-zinc-900 focus:outline-none"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="font-mono text-sm font-semibold text-zinc-100">
                      {row.ticker}
                    </span>
                    <span
                      className={`font-mono text-xs tabular-nums ${changeColorClass(row.daily_change_pct)}`}
                    >
                      {formatChange(row.daily_change_pct)}
                    </span>
                  </div>
                  <div className="flex items-baseline justify-between gap-2 text-xs text-zinc-500">
                    <span className="truncate">{row.name}</span>
                    <span className="font-mono tabular-nums text-zinc-400">
                      {Number.isFinite(row.price) ? formatPrice(row.price) : "—"}
                    </span>
                  </div>
                </div>
                <Sparkline
                  values={row.sparkline}
                  stroke={sparklineStroke(row.daily_change_pct)}
                  ariaLabel={`${row.ticker} 60-day price sparkline`}
                />
              </Link>
            </li>
          ))}
        </ul>
      )}

      <footer className="border-t border-zinc-800 px-4 py-2 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
        EOD · {nextIngest}
      </footer>
    </aside>
  );
}
