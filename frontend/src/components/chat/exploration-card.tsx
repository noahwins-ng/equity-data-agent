// ─── Exploration-scan card (QNT-220 follow-up) ────────────────────────────
//
// Renders a broad anchored "what's interesting" scan: a headline of what
// stands out, cross-lens observation bullets, and verbatim cited-value chips.
// Deliberately verdict-free — a scan surfaces what is notable, it does not
// take a buy/sell stance — and carries no forward "watch next" (no report
// exposes dated catalysts to copy from). The chip table mirrors the focused
// card's cited-values block.

import type { DoneEvent, ExplorationAnswerPayload } from "@/lib/api";

import { ProseBlock } from "./prose-block";

export function ExplorationCard({
  ticker,
  exploration,
  stats,
  // QNT-229 #6: render the headline only when the narrative bubble is absent
  // (narrate degraded). Observations + cited values always render.
  showProse = true,
}: {
  ticker: string | null;
  exploration: ExplorationAnswerPayload;
  stats: DoneEvent | null;
  showProse?: boolean;
}) {
  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40">
      <header className="flex items-baseline justify-between gap-2 border-b border-zinc-800 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-zinc-400">
        <span className="flex items-baseline gap-2">
          <span>Scan · {ticker ?? "session"}</span>
          <span className="rounded border border-violet-700/40 bg-violet-900/20 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-violet-300">
            Exploration
          </span>
        </span>
        {stats && (
          <span className="text-zinc-500">
            {stats.tools_count} sources · {stats.citations_count} cited
          </span>
        )}
      </header>

      <div className="space-y-3 p-3">
        {showProse && <ProseBlock text={exploration.headline} />}

        {exploration.observations.length > 0 && (
          <div>
            <h4 className="mb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              What stands out
            </h4>
            <ul className="space-y-0.5 text-xs text-zinc-200">
              {exploration.observations.map((o, i) => (
                <li key={i} className="flex gap-1">
                  <span className="text-violet-500">·</span>
                  <span className="min-w-0 flex-1">
                    <ProseBlock text={o} />
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {exploration.cited_values.length > 0 && (
          <div className="rounded border border-zinc-800 bg-zinc-950/60 p-2">
            <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              Cited values
            </div>
            <ul className="space-y-1">
              {exploration.cited_values.map((kv, i) => (
                <li
                  key={i}
                  className="flex items-baseline justify-between gap-2 font-mono text-[10px]"
                >
                  <span className="uppercase tracking-wider text-zinc-500">{kv.label}</span>
                  <span className="flex items-baseline gap-1">
                    <span className="rounded border border-zinc-700 bg-zinc-950 px-1 py-px font-mono text-[10px] tabular-nums text-zinc-100">
                      {kv.value}
                    </span>
                    <span className="rounded border border-zinc-700 bg-zinc-900 px-1 py-px text-[9px] uppercase tracking-wide text-zinc-400">
                      {kv.source}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </section>
  );
}
