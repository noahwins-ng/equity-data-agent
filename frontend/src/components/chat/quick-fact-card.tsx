// ─── Quick-fact compact card (QNT-149) ────────────────────────────────────
//
// The quick-fact path returns a short prose answer plus exactly one cited
// value. We render the answer the same way as thesis prose (so inline
// (source: …) chips work), and surface the structured cited value as a
// monospaced chip below the answer when present. The thesis card is
// intentionally absent for this run shape.

import type { DoneEvent, QuickFactPayload } from "@/lib/api";

import { ProseBlock } from "./prose-block";

export function QuickFactCard({
  ticker,
  quickFact,
  stats,
}: {
  ticker: string | null;
  quickFact: QuickFactPayload;
  stats: DoneEvent | null;
}) {
  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40">
      <header className="flex items-baseline justify-between gap-2 border-b border-zinc-800 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-zinc-400">
        <span>Quick fact · {ticker ?? "session"}</span>
        {stats && (
          <span className="text-zinc-500">
            {stats.tools_count} sources · {stats.citations_count} cited
          </span>
        )}
      </header>

      <div className="space-y-2 p-3">
        <ProseBlock text={quickFact.answer} />
        {quickFact.cited_value && quickFact.source && (
          <div className="flex items-baseline gap-2">
            <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              Value
            </span>
            <span className="rounded border border-zinc-700 bg-zinc-950 px-1.5 py-0.5 font-mono text-[11px] tabular-nums text-zinc-100">
              {quickFact.cited_value}
            </span>
            <span className="rounded border border-zinc-700 bg-zinc-900 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-zinc-400">
              {quickFact.source}
            </span>
          </div>
        )}
      </div>
    </section>
  );
}
