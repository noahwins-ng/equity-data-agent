// Local message-tape model for the chat panel, split out of chat-panel.tsx
// (QNT-253) so the card components and RunBlock can share it without importing
// from the container.
//
// One run = one user-prompted exchange. The tape stores at most one in-flight
// run plus the prior runs in this session (no cross-session persistence --
// out of scope per QNT-130). Each run carries the tool rows it observed,
// the streamed prose deltas, the final thesis (if any), and terminal stats.

import type {
  ChatErrorEvent,
  ComparisonPayload,
  ConversationalPayload,
  DoneEvent,
  ExplorationAnswerPayload,
  FocusedAnalysisPayload,
  Intent,
  LeanComparisonPayload,
  QuickFactPayload,
  RetrievedSource,
  ThesisPayload,
  ToolCallEvent,
  ToolResultEvent,
} from "@/lib/api";

export type RunStatus = "streaming" | "done" | "errored";

export type ToolRow = ToolCallEvent & {
  result?: ToolResultEvent;
};

export type ChatRun = {
  id: string;
  ticker: string | null;
  prompt: string;
  startedAt: number;
  status: RunStatus;
  intent: Intent | null;
  toolRows: ToolRow[];
  proseChunks: string[];
  // QNT-211: accumulated narrative_chunk deltas -- rendered as a prose
  // bubble ABOVE the structured card. Empty string means "no narrative
  // yet" (narrate hasn't started or failed silently); the bubble only
  // renders when non-empty.
  narrative: string;
  thesis: ThesisPayload | null;
  quickFact: QuickFactPayload | null;
  comparison: ComparisonPayload | null;
  comparisonLean: LeanComparisonPayload | null;
  conversational: ConversationalPayload | null;
  focused: FocusedAnalysisPayload | null;
  exploration: ExplorationAnswerPayload | null;
  // QNT-226: articles the semantic news search surfaced this turn. Rendered
  // as a compact clickable provenance list. Empty when no search ran.
  retrievedSources: RetrievedSource[];
  errors: ChatErrorEvent[];
  stats: DoneEvent | null;
};
