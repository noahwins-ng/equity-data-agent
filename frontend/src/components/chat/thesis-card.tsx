// ─── QNT-208: four-aspect thesis card ─────────────────────────────────────

import type { DoneEvent, ThesisPayload, Verdict } from "@/lib/api";

import { AspectBlock } from "./aspect-block";
import { ProseBlock } from "./prose-block";

// Verdict pill palette. Overweight = emerald (constructive), Neutral = zinc
// (balanced), Underweight = red (negative). Pydantic bounds the verdict on
// the server side; an exhaustive map means a future verdict value lights up
// a type error rather than a missing className at runtime.
const VERDICT_PILL: Record<Verdict, string> = {
  Overweight: "border-emerald-700/40 bg-emerald-900/20 text-emerald-300",
  Neutral: "border-zinc-700 bg-zinc-900/40 text-zinc-300",
  Underweight: "border-red-700/40 bg-red-900/20 text-red-300",
};

export function ThesisCard({
  ticker,
  thesis,
  stats,
  // QNT-229 #6: render verdict_rationale only when the narrative bubble is
  // absent (narrate degraded). Otherwise the bubble is the prose surface.
  showProse = true,
}: {
  ticker: string | null;
  thesis: ThesisPayload;
  stats: DoneEvent | null;
  showProse?: boolean;
}) {
  const confidencePct = stats
    ? Math.max(0, Math.min(100, Math.round(stats.confidence * 100)))
    : null;
  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40">
      <header className="flex items-baseline justify-between gap-2 border-b border-zinc-800 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-zinc-400">
        <span>Thesis · {ticker ?? "session"} · this session</span>
        {stats && (
          <span className="text-zinc-500">
            {stats.tools_count} sources · {stats.citations_count} cited
          </span>
        )}
      </header>

      <div className="space-y-3 p-3">
        <AspectBlock title="Company" aspect={thesis.company} />
        <AspectBlock title="Fundamental" aspect={thesis.fundamental} />
        <AspectBlock title="Technical" aspect={thesis.technical} />
        <AspectBlock title="News" aspect={thesis.news} />

        <div className="rounded border border-zinc-800 bg-zinc-950/60 p-2">
          <div className="mb-1 flex items-center gap-2">
            <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              Verdict
            </span>
            <span
              className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${VERDICT_PILL[thesis.verdict]}`}
            >
              {thesis.verdict}
            </span>
          </div>
          {showProse && <ProseBlock text={thesis.verdict_rationale} />}
          {confidencePct !== null && (
            <div className="mt-2">
              <div className="mb-0.5 flex justify-between font-mono text-[10px] uppercase tracking-wider text-zinc-500">
                <span>Answer groundedness</span>
                <span>{confidencePct}%</span>
              </div>
              {/* QNT-286: filled in zinc, not sky. `sky` is the reserved
                  "SPY benchmark" hue (the SPY price-chart line + chip and the
                  focused-read accent); a groundedness meter has nothing to do
                  with the benchmark, so borrowing sky here was a semantic
                  collision. Neutral zinc reads as a plain quality gauge.
                  (Brand-vs-gain emerald overlap was reviewed and kept: green
                  == "up" is a terminal convention a green brand reinforces.) */}
              <div className="h-1 w-full overflow-hidden rounded bg-zinc-800">
                <div
                  className="h-full bg-zinc-400"
                  style={{ width: `${confidencePct}%` }}
                />
              </div>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
