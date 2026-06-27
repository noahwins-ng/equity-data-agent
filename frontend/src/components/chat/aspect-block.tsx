// ─── QNT-208: four-aspect thesis card sub-block ───────────────────────────

import type { AspectLabel, AspectView } from "@/lib/api";

import { ProseBlock } from "./prose-block";
import type { DedupeState } from "./prose-parse";

// Per-aspect label chip palette. Premium / Uptrend = green; Discounted /
// Downtrend = red; Inline / Sideways = zinc; null label = no chip rendered.
// Shared with LeanComparisonCard and FocusedAnalysisCard.
export const ASPECT_LABEL_PILL: Record<AspectLabel, string> = {
  Premium: "border-amber-700/40 bg-amber-900/20 text-amber-300",
  Inline: "border-zinc-700 bg-zinc-900/40 text-zinc-300",
  Discounted: "border-emerald-700/40 bg-emerald-900/20 text-emerald-300",
  Uptrend: "border-emerald-700/40 bg-emerald-900/20 text-emerald-300",
  Sideways: "border-zinc-700 bg-zinc-900/40 text-zinc-300",
  Downtrend: "border-red-700/40 bg-red-900/20 text-red-300",
};

// QNT-213: the thesis planner narrows to a subset of report tools, so a
// skipped aspect comes back filled with a sentinel summary (the synthesis
// prompt instructs the LLM to emit "Not fetched for this question." verbatim
// with a null label and empty bullets). Render nothing for those aspects
// rather than an empty stub section — the card shows only what was researched.
const NOT_FETCHED_SUMMARY = "not fetched for this question";

function aspectWasFetched(aspect: AspectView): boolean {
  return !aspect.summary.trim().toLowerCase().startsWith(NOT_FETCHED_SUMMARY);
}

export function AspectBlock({ title, aspect }: { title: string; aspect: AspectView }) {
  if (!aspectWasFetched(aspect)) return null;
  // QNT-287: one de-dup carrier shared across this aspect's summary + bullets.
  // An aspect is single-source by construction (the News aspect cites news,
  // etc.) and the header already names it, so the inline chip repeats on every
  // line. Threading one carrier through the summary then each support/challenge
  // ProseBlock — which render in order — collapses it to a single chip. The
  // carrier is recreated on every AspectBlock render, so the threading stays
  // deterministic across re-renders.
  const dedupe: DedupeState = { last: null };
  return (
    <div>
      <div className="mb-1 flex items-baseline gap-2">
        <h4 className="font-mono text-[10px] uppercase tracking-wider text-zinc-400">
          {title}
        </h4>
        {aspect.label && (
          <span
            className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${ASPECT_LABEL_PILL[aspect.label]}`}
          >
            {aspect.label}
          </span>
        )}
      </div>
      <ProseBlock text={aspect.summary} dedupe={dedupe} />
      {aspect.supports.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-xs text-zinc-200">
          {aspect.supports.map((point, i) => (
            <li key={`s-${i}`} className="flex gap-1">
              <span className="text-emerald-500">+</span>
              <span className="min-w-0 flex-1">
                <ProseBlock text={point} dedupe={dedupe} />
              </span>
            </li>
          ))}
        </ul>
      )}
      {aspect.challenges.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-xs text-zinc-200">
          {aspect.challenges.map((point, i) => (
            <li key={`c-${i}`} className="flex gap-1">
              <span className="text-amber-500">·</span>
              <span className="min-w-0 flex-1">
                <ProseBlock text={point} dedupe={dedupe} />
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
