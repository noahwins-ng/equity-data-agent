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

export type CardProseSurface = {
  status: "streaming" | "done" | "errored";
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
// Card prose is hidden while a narrating run is still streaming. That prevents
// the early card from briefly showing fallback prose and then retracting it as
// soon as the narrative bubble starts. After the run finishes, empty narrative
// means narrate degraded, so the card prose renders as the fallback:
//   thesis      -> verdict_rationale
//   focused     -> summary
//   exploration -> headline
//   comparison  -> differences (rich 2-ticker shape only)
// quick_fact is excluded: QNT-232 #3 skips narrate for it, so its card answer
// is always the lone prose surface (never demoted). comparison_lean +
// conversational carry no demotable prose. The structured data (labels,
// verdict pill, bullets, chips, table) is never touched — only the one
// overlapping prose field.
export function showCardProse(run: CardProseSurface): boolean {
  return run.status !== "streaming" && run.narrative.trim().length === 0;
}

// ─── QNT-247: screen-reader announcement text ──────────────────────────────
//
// The analyst ANSWER is what a screen-reader user currently never hears — it
// renders into a plain <article> with no live region (frontend audit #2). This
// returns the text to feed a debounced aria-live=polite region. Precedence:
//   1. narrative (analyst voice) — thesis / focused / exploration / comparison
//   2. standalone streamed prose — conversational
//   3. quick_fact answer — quick_fact skips narrate (QNT-232 #3) and streams no
//      prose, so its card answer is the ONLY prose surface; without this clause
//      a quick-fact run announces nothing (verified live: the QuickFactCard
//      rendered the RSI answer while the live region stayed silent).
// Pure-table shapes (comparison_lean) and degraded card-only runs carry no
// flat prose answer; their structured DOM is reachable by normal SR navigation.
export type AnnounceSurface = {
  narrative: string;
  proseChunks: string[];
  quickFact: { answer: string } | null;
};

export function announceableAnswer(run: AnnounceSurface): string {
  const narrative = run.narrative.trim();
  if (narrative) return narrative;
  const prose = run.proseChunks.join("").trim();
  if (prose) return prose;
  return run.quickFact?.answer.trim() ?? "";
}

// ─── QNT-252: bind a tool_result to its tool_call by started_at ────────────
//
// The SSE stream emits a tool_call, then (once the tool returns) a tool_result.
// The panel pairs them to hang latency/summary/ok off the right row. Binding by
// `name` alone — "first unmatched row of that name" — mis-pairs when two
// concurrent calls to the SAME tool are in flight: a result that completes
// first would bind to whichever same-name row was unmatched first, not to the
// call it belongs to. `started_at` (the server clock captured per tool_call and
// echoed back on tool_result — QNT-252 backend) is a unique correlation key, so
// we match on it for an exact bind. Name is kept in the predicate as a cheap
// guard; started_at alone is already unambiguous.
export function bindToolResult<
  Result extends { name: string; started_at: number },
  Row extends { name: string; started_at: number; result?: Result },
>(rows: Row[], result: Result): Row[] {
  return rows.map((row) =>
    row.name === result.name && row.started_at === result.started_at
      ? { ...row, result }
      : row,
  );
}

// ─── QNT-299: degraded-tool note ───────────────────────────────────────────
//
// `degraded_tools` on the done event carries bare report-tool names that
// either hit a required-tool error or (news) were silently dropped after
// retry exhaustion this turn. Mirrors the backend's `_TOOL_LABELS` mapping
// (agent_chat.py) so the copy the user sees matches the tool-call rows they
// already watched stream in — never the raw error internals.
const DEGRADED_TOOL_LABELS: Record<string, string> = {
  company: "Company",
  technical: "Technicals",
  fundamental: "Fundamentals",
  news: "News",
  comparison_metrics: "Comparison metrics",
};

// Returns null when nothing degraded (the clean-turn case renders no note).
export function degradedToolsNote(degradedTools: string[]): string | null {
  if (degradedTools.length === 0) return null;
  const labels = degradedTools.map((name) => DEGRADED_TOOL_LABELS[name] ?? name);
  return `${labels.join(", ")} unavailable this turn.`;
}
