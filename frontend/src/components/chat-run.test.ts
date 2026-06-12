// Run with: `npm test` (from `frontend/`) — Node's built-in TS loader + node:test.

import test from "node:test";
import assert from "node:assert/strict";

import type { Intent, RetrievedSource } from "@/lib/api";

import {
  type AnswerSurface,
  type ComposingSurface,
  composingLabel,
  hasAnswerSurface,
  isComposing,
  showCardProse,
} from "./chat-run.ts";

function surface(overrides: Partial<AnswerSurface> = {}): AnswerSurface {
  return {
    thesis: null,
    quickFact: null,
    comparison: null,
    comparisonLean: null,
    conversational: null,
    focused: null,
    exploration: null,
    narrative: "",
    retrievedSources: [],
    ...overrides,
  };
}

const source: RetrievedSource = {
  headline: "NVDA strikes Micron HBM4 supply deal",
  source: "Reuters",
  date: "2026-06-01",
  url: "https://ex.com/a",
};

// QNT-226 AC3 (frontend): the targeted-news narrative-only shape (no card
// payloads, retrieved sources present) is a valid answer surface — so the run
// renders the disclaimer and is NOT flipped to "errored" even if a terminal
// error arrives. This is the regression guard for the chat-panel status logic.
test("targeted-news shape (sources only, no card) is an answer surface", () => {
  assert.equal(hasAnswerSurface(surface({ retrievedSources: [source] })), true);
});

test("narrative alone (voice, no card) is an answer surface", () => {
  assert.equal(hasAnswerSurface(surface({ narrative: "NVDA inked a deal." })), true);
});

test("broad-news shape (focused card present) is an answer surface", () => {
  // Minimal focused payload — only presence matters to the surface check.
  const focused = { focus: "news" } as unknown as AnswerSurface["focused"];
  assert.equal(hasAnswerSurface(surface({ focused })), true);
});

test("lean N-way comparison card present is an answer surface", () => {
  // QNT-224: the 3-4 ticker lean metrics table lands on its own slot; a run
  // carrying only it (degraded narrate) must still count as a real answer.
  const comparisonLean = {
    rows: [{ ticker: "AAPL", pe: "28.4", rsi: "65.2", net_margin: "24.1%", price: "$182.50" }],
  } as unknown as AnswerSurface["comparisonLean"];
  assert.equal(hasAnswerSurface(surface({ comparisonLean })), true);
});

test("empty run (no card, no narrative, no sources) is NOT an answer surface", () => {
  assert.equal(hasAnswerSurface(surface()), false);
});

test("empty sources array does not count as a surface", () => {
  assert.equal(hasAnswerSurface(surface({ retrievedSources: [] })), false);
});

// ─── QNT-229 #2a: composing-phase inference (AC1) ─────────────────────────

function composing(overrides: Partial<ComposingSurface> = {}): ComposingSurface {
  return {
    status: "streaming",
    intent: "thesis",
    toolRows: [{ result: {} }],
    narrative: "",
    ...overrides,
  };
}

test("composing: intent set + tools complete + nothing streamed + live => true", () => {
  assert.equal(isComposing(composing()), true);
});

test("composing: followup short-circuit (no tool rows) infers composing", () => {
  // followup never gathers (it reasons over hydrated state), so an empty
  // toolRows is terminal and the indicator shows the instant intent arrives.
  assert.equal(isComposing(composing({ intent: "followup", toolRows: [] })), true);
});

test("composing: conversational short-circuit (no tool rows) infers composing", () => {
  // conversational also skips plan+gather; the caller hides the indicator once
  // its prose_chunk / card lands, but the timing predicate fires on intent.
  assert.equal(isComposing(composing({ intent: "conversational", toolRows: [] })), true);
});

test("composing: tool intent with no rows yet (plan phase) does NOT flash", () => {
  // thesis/etc gather tools; before the first tool_call lands toolRows is
  // transiently empty — must not show the indicator (regression guard for the
  // pre-tool flicker).
  assert.equal(isComposing(composing({ intent: "thesis", toolRows: [] })), false);
});

test("composing: false while a tool row is still in flight (no result)", () => {
  assert.equal(
    isComposing(composing({ toolRows: [{ result: {} }, {}] })),
    false,
  );
});

test("composing: ends only when narration starts (not when the early card lands)", () => {
  // QNT-229 #2b emits the card EARLY; the shimmer must hold the voice slot
  // above it until narration begins, so card arrival does NOT end composing —
  // only the narrative starting does. (isComposing no longer observes the card;
  // the narrative is the sole stream-side terminator.)
  assert.equal(isComposing(composing({ narrative: "I'd lean" })), false);
});

test("composing: false before the intent event arrives", () => {
  assert.equal(isComposing(composing({ intent: null })), false);
});

test("composing: false once the run is done", () => {
  assert.equal(isComposing(composing({ status: "done" })), false);
});

test("composingLabel names the shape by intent", () => {
  assert.equal(composingLabel("thesis"), "composing thesis…");
  assert.equal(composingLabel("comparison"), "composing comparison…");
  assert.equal(composingLabel("exploration"), "composing scan…");
  // Null intent falls back to the thesis shape (panel default).
  assert.equal(composingLabel(null), "composing thesis…");
});

test("composingLabel covers every Intent value", () => {
  const intents: Intent[] = [
    "thesis",
    "quick_fact",
    "comparison",
    "conversational",
    "fundamental",
    "technical",
    "news",
    "followup",
    "exploration",
  ];
  for (const intent of intents) {
    assert.match(composingLabel(intent), /^composing .+…$/);
  }
});

// ─── QNT-229 #6: one prose surface per turn (AC3) ─────────────────────────
//
// showCardProse drives whether each card renders its own prose field. The
// decision is uniform across the four narrate-streaming shapes (thesis /
// focused / exploration / comparison-rich): when the narrative bubble streamed
// the card prose is hidden; when narrate degraded (empty narrative) it renders
// as the fallback. Per-shape coverage is via the wiring in chat-panel.tsx —
// every card receives this same predicate as its `showProse` prop.

test("card prose hidden when the narrative bubble is present", () => {
  assert.equal(showCardProse("On balance the read is constructive."), false);
});

test("card prose renders as fallback when narrate degraded (empty narrative)", () => {
  assert.equal(showCardProse(""), true);
});

test("whitespace-only narrative is treated as empty (prose renders)", () => {
  assert.equal(showCardProse("   \n  "), true);
});
