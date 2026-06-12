// Pure helpers for the chat run lifecycle, split out of chat-panel.tsx so they
// can be unit-tested under the node:test harness (no React renderer needed).

import type {
  ComparisonPayload,
  ConversationalPayload,
  ExplorationAnswerPayload,
  FocusedAnalysisPayload,
  Intent,
  LeanComparisonPayload,
  QuickFactPayload,
  RetrievedSource,
  ThesisPayload,
} from "@/lib/api";

// The subset of a ChatRun that determines whether the run produced anything to
// show. Typed structurally so callers (and tests) don't need the full ChatRun.
export type AnswerSurface = {
  thesis: ThesisPayload | null;
  quickFact: QuickFactPayload | null;
  comparison: ComparisonPayload | null;
  comparisonLean: LeanComparisonPayload | null;
  conversational: ConversationalPayload | null;
  focused: FocusedAnalysisPayload | null;
  exploration: ExplorationAnswerPayload | null;
  narrative: string;
  retrievedSources: RetrievedSource[];
};

// QNT-226: a run has an answer surface when it produced any structured card, an
// analyst-voice narrative, OR a retrieved-sources list. The last clause is what
// makes the targeted-news narrative-only shape (focused=None, sources present)
// a valid surface rather than a blank/errored run: even if narrate degrades, the
// clickable sources list is a real answer. Drives both the "errored vs done"
// status decision and the disclaimer footer in chat-panel.tsx.
export function hasAnswerSurface(run: AnswerSurface): boolean {
  return (
    Boolean(
      run.thesis ||
        run.quickFact ||
        run.comparison ||
        run.comparisonLean ||
        run.conversational ||
        run.focused ||
        run.exploration ||
        run.narrative,
    ) || run.retrievedSources.length > 0
  );
}

// ─── QNT-229 #2a: synthesize-phase inference (composing animation) ─────────
//
// The panel infers the synthesize ("composing") phase with NO backend signal:
// the intent has arrived, every tool row that started has a result, nothing
// has streamed yet (no narrative), and the run is still live. That is the
// dead-air window between the last tool returning and the analyst voice
// arriving. Short-circuit paths (followup / conversational) have no tool rows,
// so `.every` over an empty list holds and the same inference fires the instant
// intent arrives.
//
// Deliberately NOT gated on card arrival: QNT-229 #2b emits the card EARLY, and
// the composing shimmer holds the voice slot ABOVE that card until narration
// starts — so the card never gets shoved by a bubble appearing above it. Only
// the narrative starting (or the run ending) ends the composing state. The
// caller suppresses it on no-narrate paths (conversational / redirect / prose).

// The minimal slice of a ChatRun the phase inference needs — typed
// structurally so callers and tests don't need the full ChatRun.
export type ComposingSurface = {
  status: "streaming" | "done" | "errored";
  intent: Intent | null;
  // Each tool row carries an optional `result`; "complete" = result present.
  toolRows: { result?: unknown }[];
  narrative: string;
};

export function isComposing(run: ComposingSurface): boolean {
  if (run.status !== "streaming") return false;
  if (run.intent === null) return false;
  if (run.narrative.trim().length > 0) return false;
  // A tool-gathering intent has a transient plan-phase gap: intent has arrived
  // but the first tool_call hasn't yet, so toolRows is momentarily empty. We
  // must NOT flash the indicator then — wait for tools to arrive AND complete.
  // `followup` and `conversational` legitimately gather no tools (both
  // short-circuit past plan+gather), so for them an empty toolRows is terminal
  // and the indicator shows the instant intent arrives. The caller still hides
  // it once their prose/card begins.
  if (run.toolRows.length === 0) {
    return run.intent === "followup" || run.intent === "conversational";
  }
  return run.toolRows.every((row) => row.result != null);
}

// Clean shape names (no ellipsis) so the composing label reads "composing
// thesis…". Kept separate from chat-panel's streamingLabel so the composing
// copy can diverge from the terminal "streaming …" footer.
const COMPOSING_SHAPE: Record<Intent, string> = {
  thesis: "thesis",
  quick_fact: "quick fact",
  comparison: "comparison",
  conversational: "reply",
  fundamental: "fundamental analysis",
  technical: "technical analysis",
  news: "news read",
  followup: "follow-up",
  exploration: "scan",
};

export function composingLabel(intent: Intent | null): string {
  return `composing ${COMPOSING_SHAPE[intent ?? "thesis"]}…`;
}

// ─── QNT-229 #6: one prose surface per turn ────────────────────────────────
//
// When the narrate bubble streamed (narrative non-empty) it becomes THE prose
// surface for the turn, so the card's own prose field is hidden to stop the
// two surfaces restating the same sentence:
//   thesis      -> verdict_rationale
//   focused     -> summary
//   exploration -> headline
//   comparison  -> differences (rich 2-ticker shape only)
// When narrate degraded (empty narrative — best-effort by contract) the card
// prose renders as the fallback so the turn still reads. quick_fact is excluded
// (its trim is QNT-232); comparison_lean + conversational carry no demotable
// prose. The structured data (labels, verdict pill, bullets, chips, table) is
// never touched — only the one overlapping prose field.
export function showCardProse(narrative: string): boolean {
  return narrative.trim().length === 0;
}
