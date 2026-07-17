// ─── Run-event reducer (QNT-379) ──────────────────────────────────────────
//
// The startRun SSE event-reducer, extracted from chat-panel.tsx into a
// framework-free module so the run lifecycle — including the two paths that
// used to leave a zombie ``status: "streaming"`` run (abort mid-stream, clean
// EOF without a ``done`` frame) — is covered by node:test.
//
// Relative value imports carry explicit .ts extensions
// (allowImportingTsExtensions) so the module resolves under Node's built-in
// TS loader (`npm test`) as well as the bundler.

import type {
  ChatErrorEvent,
  ComparisonPayload,
  ConversationalPayload,
  DoneEvent,
  ExplorationAnswerPayload,
  FocusedAnalysisPayload,
  IntentEvent,
  LeanComparisonPayload,
  NarrativeChunkEvent,
  PlanRationaleEvent,
  ProseChunkEvent,
  QuickFactPayload,
  RetrievedSourcesEvent,
  ThesisPayload,
  ToolCallEvent,
  ToolResultEvent,
} from "@/lib/api";

import { bindToolResult, hasAnswerSurface } from "../chat-run.ts";
import {
  annotateUnsupportedDeep,
  annotateUnsupportedNumbers,
} from "./annotate-unsupported.ts";
import type { ChatRun } from "./types.ts";

// Canonical initial state for a run entering the tape. Callers overlay the
// no-ticker errored variant on top (chat-panel's landing-route guard).
export function initialRun(fields: {
  id: string;
  ticker: string | null;
  prompt: string;
  startedAt: number;
}): ChatRun {
  return {
    ...fields,
    status: "streaming",
    intent: null,
    toolRows: [],
    proseChunks: [],
    narrative: "",
    planRationale: null,
    thesis: null,
    quickFact: null,
    comparison: null,
    comparisonLean: null,
    conversational: null,
    focused: null,
    exploration: null,
    retrievedSources: [],
    errors: [],
    stats: null,
  };
}

// One SSE frame → next run state. Unknown events are a no-op.
export function reduceRunEvent(run: ChatRun, event: string, data: unknown): ChatRun {
  if (event === "tool_call") {
    const ev = data as ToolCallEvent;
    return { ...run, toolRows: [...run.toolRows, { ...ev }] };
  }
  if (event === "tool_result") {
    const ev = data as ToolResultEvent;
    // QNT-252: bind by started_at, not first-unmatched-by-name.
    return { ...run, toolRows: bindToolResult(run.toolRows, ev) };
  }
  if (event === "prose_chunk") {
    const ev = data as ProseChunkEvent;
    return { ...run, proseChunks: [...run.proseChunks, ev.delta] };
  }
  if (event === "narrative_chunk") {
    const ev = data as NarrativeChunkEvent;
    return { ...run, narrative: run.narrative + ev.delta };
  }
  if (event === "plan_rationale") {
    const ev = data as PlanRationaleEvent;
    return { ...run, planRationale: ev.text };
  }
  if (event === "intent") {
    const ev = data as IntentEvent;
    return { ...run, intent: ev.intent };
  }
  if (event === "thesis") {
    return { ...run, thesis: data as ThesisPayload };
  }
  if (event === "quick_fact") {
    return { ...run, quickFact: data as QuickFactPayload };
  }
  if (event === "comparison") {
    return { ...run, comparison: data as ComparisonPayload };
  }
  if (event === "comparison_lean") {
    return { ...run, comparisonLean: data as LeanComparisonPayload };
  }
  if (event === "conversational") {
    return { ...run, conversational: data as ConversationalPayload };
  }
  if (event === "focused") {
    return { ...run, focused: data as FocusedAnalysisPayload };
  }
  if (event === "exploration") {
    return { ...run, exploration: data as ExplorationAnswerPayload };
  }
  if (event === "retrieved_sources") {
    const ev = data as RetrievedSourcesEvent;
    return { ...run, retrievedSources: ev.sources };
  }
  if (event === "done") {
    const ev = data as DoneEvent;
    const unsupported = ev.grounding_unsupported ?? [];
    return {
      ...run,
      stats: ev,
      narrative: annotateUnsupportedNumbers(run.narrative, unsupported),
      // Annotate the JOINED prose once (QNT-361 follow-up 5): SSE
      // chunks split on token boundaries, so per-chunk annotation
      // could miss a number straddling the split — or worse, dagger
      // a trailing fragment mid-number ("...a 45" + ".4%" →
      // "45†.4%"). RunBlock renders the join, so one chunk is
      // equivalent.
      proseChunks: run.proseChunks.length
        ? [annotateUnsupportedNumbers(run.proseChunks.join(""), unsupported)]
        : run.proseChunks,
      // QNT-361 follow-up 3: the grounding check scores the whole
      // answer, so the structured card fields get daggers too — a
      // miss in a card summary/key point used to render unmarked
      // while the banner claimed "Numbers marked †".
      thesis: annotateUnsupportedDeep(run.thesis, unsupported),
      quickFact: annotateUnsupportedDeep(run.quickFact, unsupported),
      comparison: annotateUnsupportedDeep(run.comparison, unsupported),
      comparisonLean: annotateUnsupportedDeep(run.comparisonLean, unsupported),
      conversational: annotateUnsupportedDeep(run.conversational, unsupported),
      focused: annotateUnsupportedDeep(run.focused, unsupported),
      exploration: annotateUnsupportedDeep(run.exploration, unsupported),
      // A run is "errored" only when it hit a terminal error AND
      // produced no answer surface at all (QNT-226: retrieved sources
      // count as a surface — see hasAnswerSurface).
      status: run.errors.length > 0 && !hasAnswerSurface(run) ? "errored" : "done",
    };
  }
  if (event === "error") {
    const ev = data as ChatErrorEvent;
    return { ...run, errors: [...run.errors, ev] };
  }
  return run;
}

// ─── Terminal settlement (QNT-379) ────────────────────────────────────────
//
// Guarantees every run leaves "streaming". Two paths reach here without a
// ``done`` frame: the in-flight controller was aborted (a newer send took
// over, or the panel unmounted), or the server/proxy closed the stream
// cleanly before emitting ``done`` (plausible through the Cloudflare tunnel
// ingress, ADR-018). A partial answer surface settles as "done" — the
// streamed content stands; a surfaceless run settles as "errored" with a
// reason-specific rail entry so it never renders as a blank success.
// No-op on runs already terminal, so the post-stream `.finally` in
// chat-panel can call this unconditionally.

export type SettleReason = "aborted" | "eof";

export function settleRun(run: ChatRun, reason: SettleReason): ChatRun {
  if (run.status !== "streaming") return run;
  if (hasAnswerSurface(run)) return { ...run, status: "done" };
  const detail =
    reason === "aborted"
      ? "Interrupted by a new question."
      : "The stream ended before an answer arrived.";
  const code = reason === "aborted" ? "aborted" : "stream-truncated";
  return { ...run, status: "errored", errors: [...run.errors, { detail, code }] };
}
