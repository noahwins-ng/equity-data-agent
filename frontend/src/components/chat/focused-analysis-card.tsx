// ─── Focused-analysis card (QNT-176) ──────────────────────────────────────
//
// One card shape covers all three focused intents (fundamental / technical /
// news). The ``focus`` discriminator drives the header label and
// the accent palette so a glance tells the user which read they got. The
// body fields are rendered the same way the comparison card renders its
// per-ticker section: prose summary with chips, a bullet list of key
// points, and a chip table of cited values.

import type { DoneEvent, FocusKind, FocusedAnalysisPayload } from "@/lib/api";

import { ASPECT_LABEL_PILL } from "./aspect-block";
import { ProseBlock } from "./prose-block";

const FOCUS_PILL: Record<FocusKind, { label: string; className: string }> = {
  fundamental: {
    label: "Fundamentals",
    className: "border-sky-700/40 bg-sky-900/20 text-sky-300",
  },
  technical: {
    label: "Technicals",
    className: "border-emerald-700/40 bg-emerald-900/20 text-emerald-300",
  },
  news: {
    label: "News",
    className: "border-amber-700/40 bg-amber-900/20 text-amber-300",
  },
};

function focusPill(focus: FocusKind): { label: string; className: string } {
  return (
    FOCUS_PILL[focus] ?? {
      label: focus,
      className: "border-zinc-700 bg-zinc-900/40 text-zinc-300",
    }
  );
}

export function FocusedAnalysisCard({
  ticker,
  focused,
  stats,
  // QNT-229 #6: render the top-level summary only when the narrative bubble is
  // absent (narrate degraded). Key points, catalysts, cited values are
  // structured data and always render.
  showProse = true,
}: {
  ticker: string | null;
  focused: FocusedAnalysisPayload;
  stats: DoneEvent | null;
  showProse?: boolean;
}) {
  const pill = focusPill(focused.focus);
  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40">
      <header className="flex items-baseline justify-between gap-2 border-b border-zinc-800 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-zinc-400">
        <span className="flex items-baseline gap-2">
          <span>Analysis · {ticker ?? "session"}</span>
          <span
            className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${pill.className}`}
          >
            {pill.label}
          </span>
        </span>
        {stats && (
          <span className="text-zinc-500">
            {stats.tools_count} sources · {stats.citations_count} cited
          </span>
        )}
      </header>

      <div className="space-y-3 p-3">
        {focused.verdict && (
          <div className="flex items-center gap-2">
            <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              Verdict
            </span>
            <span
              className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${ASPECT_LABEL_PILL[focused.verdict]}`}
            >
              {focused.verdict}
            </span>
          </div>
        )}
        {showProse && <ProseBlock text={focused.summary} />}

        {focused.focus === "news" && focused.existing_development && (
          <div>
            <h4 className="mb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              Running story
            </h4>
            <ProseBlock text={focused.existing_development} />
          </div>
        )}

        {focused.focus === "news" && focused.positive_catalysts.length > 0 && (
          <div>
            <h4 className="mb-1 font-mono text-[10px] uppercase tracking-wider text-emerald-400">
              Positive catalysts
            </h4>
            <ul className="space-y-0.5 text-xs text-zinc-200">
              {focused.positive_catalysts.map((c, i) => (
                <li key={`pc-${i}`} className="flex gap-1">
                  <span className="text-emerald-500">+</span>
                  <span className="min-w-0 flex-1">
                    <ProseBlock text={c} />
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {focused.focus === "news" && focused.negative_catalysts.length > 0 && (
          <div>
            <h4 className="mb-1 font-mono text-[10px] uppercase tracking-wider text-red-400">
              Negative catalysts
            </h4>
            <ul className="space-y-0.5 text-xs text-zinc-200">
              {focused.negative_catalysts.map((c, i) => (
                <li key={`nc-${i}`} className="flex gap-1">
                  <span className="text-red-500">-</span>
                  <span className="min-w-0 flex-1">
                    <ProseBlock text={c} />
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {focused.key_points.length > 0 && (
          <div>
            <h4 className="mb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              Key points
            </h4>
            <ol className="space-y-1 pl-4 text-xs text-zinc-200">
              {focused.key_points.map((point, i) => (
                <li key={i} className="list-decimal">
                  <ProseBlock text={point} />
                </li>
              ))}
            </ol>
          </div>
        )}

        {focused.cited_values.length > 0 && (
          <div className="rounded border border-zinc-800 bg-zinc-950/60 p-2">
            <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              Cited values
            </div>
            <ul className="space-y-1">
              {focused.cited_values.map((kv, i) => (
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
