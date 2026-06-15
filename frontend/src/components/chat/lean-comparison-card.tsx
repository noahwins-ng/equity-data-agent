// ─── Lean N-way comparison card (QNT-224) ─────────────────────────────────
//
// The rich ComparisonCard renders two fat aspect columns — that does not
// fit 3-4 tickers in the ~290-450px chat rail. The lean shape is a compact
// metrics table instead: tickers as columns, metrics as rows. N is capped at 4
// so the table is at most 5 columns (label + 4 tickers); overflow-x-auto saves
// the narrow md breakpoint and any long value. Every cell is a pre-formatted
// string copied verbatim from the API (ADR-003) — the panel computes nothing.

import type { DoneEvent, LeanComparisonPayload } from "@/lib/api";

import { ASPECT_LABEL_PILL } from "./aspect-block";

const LEAN_METRIC_ROWS: { key: "pe" | "rsi" | "net_margin" | "price"; label: string }[] = [
  { key: "pe", label: "P/E" },
  { key: "rsi", label: "RSI" },
  { key: "net_margin", label: "Net margin" },
  { key: "price", label: "Price" },
];

// QNT-224 follow-up: the interpretive verdicts (from the fundamental + technical
// reports) render as colored pills below the raw metrics, reusing the rich
// card's ASPECT_LABEL_PILL palette. null -> a muted dash.
const LEAN_LABEL_ROWS: {
  key: "valuation_label" | "trend_daily" | "trend_weekly";
  label: string;
}[] = [
  { key: "valuation_label", label: "Valuation" },
  { key: "trend_daily", label: "Trend (D)" },
  { key: "trend_weekly", label: "Trend (W)" },
];

export function LeanComparisonCard({
  comparison,
  stats,
}: {
  comparison: LeanComparisonPayload;
  stats: DoneEvent | null;
}) {
  const { rows } = comparison;
  const tickerHeader = rows.map((r) => r.ticker).join(" vs ");
  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40">
      <header className="flex items-baseline justify-between gap-2 border-b border-zinc-800 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-zinc-400">
        <span>Comparison · {tickerHeader || "session"}</span>
        {stats && (
          <span className="text-zinc-500">
            {stats.tools_count} sources · {stats.citations_count} cited
          </span>
        )}
      </header>

      <div className="overflow-x-auto p-3">
        <table className="w-full border-collapse font-mono text-[11px] tabular-nums">
          <thead>
            <tr className="text-zinc-300">
              <th className="px-2 py-1 text-left font-normal text-[10px] uppercase tracking-wider text-zinc-500">
                Metric
              </th>
              {rows.map((r) => (
                <th key={r.ticker} className="px-2 py-1 text-right font-semibold">
                  {r.ticker}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {LEAN_METRIC_ROWS.map(({ key, label }) => (
              <tr key={key} className="border-t border-zinc-800/60">
                <td className="px-2 py-1 text-left text-zinc-400">{label}</td>
                {rows.map((r) => (
                  <td key={r.ticker} className="px-2 py-1 text-right text-zinc-200">
                    {r[key]}
                  </td>
                ))}
              </tr>
            ))}
            {LEAN_LABEL_ROWS.map(({ key, label }, idx) => (
              <tr
                key={key}
                className={idx === 0 ? "border-t-2 border-zinc-700/80" : "border-t border-zinc-800/60"}
              >
                <td className="px-2 py-1 text-left text-zinc-400">{label}</td>
                {rows.map((r) => {
                  const value = r[key];
                  return (
                    <td key={r.ticker} className="px-2 py-1 text-right">
                      {value ? (
                        <span
                          className={`rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider ${ASPECT_LABEL_PILL[value]}`}
                        >
                          {value}
                        </span>
                      ) : (
                        <span className="text-zinc-600">—</span>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
