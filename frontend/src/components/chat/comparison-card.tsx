// ─── Side-by-side comparison card (QNT-156) ───────────────────────────────
//
// Two columns, one per ticker, with a verbatim cited-values table beneath
// the prose summary. The differences paragraph renders as a single block
// below the two columns. ADR-003: every value here was copied verbatim from
// one ticker's reports — the rendering layer never computes deltas.

import type { ComparisonPayload, DoneEvent } from "@/lib/api";

import { AspectBlock } from "./aspect-block";
import { ProseBlock } from "./prose-block";

export function ComparisonCard({
  comparison,
  stats,
  // QNT-229 #6: render the differences paragraph only when the narrative
  // bubble is absent (narrate degraded) — otherwise the bubble speaks it.
  showProse = true,
}: {
  comparison: ComparisonPayload;
  stats: DoneEvent | null;
  showProse?: boolean;
}) {
  const tickerHeader = comparison.sections.map((s) => s.ticker).join(" vs ");
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

      <div className="space-y-3 p-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {comparison.sections.map((section) => (
            <div
              key={section.ticker}
              className="space-y-3 rounded border border-zinc-800 bg-zinc-950/60 p-2"
            >
              <div className="font-mono text-[11px] uppercase tracking-wider text-zinc-300">
                {section.ticker}
              </div>
              <AspectBlock title="Company" aspect={section.company} />
              <AspectBlock title="Fundamental" aspect={section.fundamental} />
              <AspectBlock title="Technical" aspect={section.technical} />
              <AspectBlock title="News" aspect={section.news} />
            </div>
          ))}
        </div>

        {showProse && (
          <div className="rounded border border-zinc-800 bg-zinc-950/60 p-2">
            <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              Differences
            </div>
            <ProseBlock text={comparison.differences} />
          </div>
        )}
      </div>
    </section>
  );
}
