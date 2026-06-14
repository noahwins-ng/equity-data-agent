/**
 * Landing route — middle pane (ADR-014 §2).
 *
 * Static at build time: the only fetch is `/dashboard/summary` through the
 * apiFetch default (`cache: "force-cache"`), the same request the layout's
 * Watchlist makes. Both the layout and this page prerender at build time and
 * share Next's force-cache Data Cache entry for that URL, so the summary is
 * fetched once and pinned into the build output — this page stays force-static
 * with no per-request fetch (QNT-250). Freshness is deploy-hook driven like the
 * rest of the static shell.
 *
 * Replaces the lone "Select a ticker" line (audit #17) with a compact market
 * overview — top movers derived from the watchlist summary — so the pane reads
 * as a finished workspace rather than an empty void on first load.
 */
import Link from "next/link";

import { apiFetch } from "@/lib/api";

export const dynamic = "force-static";

type SummaryRow = {
  ticker: string;
  name: string;
  price: number;
  daily_change_pct: number | null;
};

async function loadSummary(): Promise<SummaryRow[]> {
  try {
    return await apiFetch<SummaryRow[]>("/api/v1/dashboard/summary");
  } catch {
    // Soft-fail: the overview collapses to the onboarding cue alone, matching
    // the watchlist's fail-soft behaviour rather than tearing down the page.
    return [];
  }
}

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

function MoverColumn({ title, rows }: { title: string; rows: SummaryRow[] }) {
  return (
    <div className="rounded border border-zinc-800 bg-zinc-950">
      <h2 className="border-b border-zinc-800 px-3 py-2 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
        {title}
      </h2>
      <ul className="divide-y divide-zinc-800/60">
        {rows.map((row) => (
          <li key={row.ticker}>
            <Link
              href={`/ticker/${row.ticker}`}
              className="flex items-baseline justify-between gap-3 px-3 py-2 transition hover:bg-zinc-900 focus:bg-zinc-900 focus:outline-none"
            >
              <span className="font-mono text-sm font-semibold text-zinc-100">
                {row.ticker}
              </span>
              <span className="flex items-baseline gap-2 font-mono text-sm tabular-nums">
                <span className="text-zinc-300">
                  {Number.isFinite(row.price) ? formatPrice(row.price) : "—"}
                </span>
                <span className={changeColorClass(row.daily_change_pct)}>
                  {formatChange(row.daily_change_pct)}
                </span>
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default async function Home() {
  const rows = await loadSummary();
  const ranked = rows
    .filter((r) => r.daily_change_pct !== null)
    .sort((a, b) => (b.daily_change_pct ?? 0) - (a.daily_change_pct ?? 0));
  const gainers = ranked.slice(0, 4);
  // Start losers no earlier than index 4 so the two columns never share a
  // ticker when fewer than 8 names have a non-null daily_change_pct (e.g. a
  // partial-ingest day on the 10-ticker universe).
  const losers = ranked.slice(Math.max(4, ranked.length - 4)).reverse();
  const hasMovers = ranked.length > 0;

  return (
    <div className="mx-auto flex h-full w-full max-w-3xl flex-col justify-center gap-10 p-6 md:p-10">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-100">
          Market overview
        </h1>
        <p className="max-w-xl text-sm leading-relaxed text-zinc-400">
          Select a ticker from the watchlist to view price action, indicators,
          fundamentals, and news — or jump straight into today&apos;s movers
          below.
        </p>
      </header>

      {hasMovers && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          {gainers.length > 0 && <MoverColumn title="Top gainers" rows={gainers} />}
          {losers.length > 0 && <MoverColumn title="Top losers" rows={losers} />}
        </div>
      )}

      <p className="font-mono text-[10px] uppercase tracking-wider text-zinc-600">
        10-ticker portfolio · end-of-day data
      </p>
    </div>
  );
}
