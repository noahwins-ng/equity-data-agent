// Pure helpers for the chat run lifecycle, split out of chat-panel.tsx so they
// can be unit-tested under the node:test harness (no React renderer needed).

import type {
  ComparisonPayload,
  ConversationalPayload,
  ExplorationAnswerPayload,
  FocusedAnalysisPayload,
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
        run.conversational ||
        run.focused ||
        run.exploration ||
        run.narrative,
    ) || run.retrievedSources.length > 0
  );
}
