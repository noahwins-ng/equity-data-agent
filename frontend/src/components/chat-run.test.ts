// Run with: `npm test` (from `frontend/`) — Node's built-in TS loader + node:test.

import test from "node:test";
import assert from "node:assert/strict";

import type { RetrievedSource } from "@/lib/api";

import { type AnswerSurface, hasAnswerSurface } from "./chat-run.ts";

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
