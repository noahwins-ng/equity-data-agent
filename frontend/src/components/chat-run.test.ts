// Run with: `npm test` (from `frontend/`) — Node's built-in TS loader + node:test.

import test from "node:test";
import assert from "node:assert/strict";

import type { Intent, RetrievedSource } from "@/lib/api";

import {
  type AnnounceSurface,
  type AnswerSurface,
  type CardProseSurface,
  type ComposingSurface,
  announceableAnswer,
  bindToolResult,
  composingLabel,
  degradedToolsNote,
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
// decision is uniform across the four narrate-streaming shapes. While the run
// streams, card prose stays hidden even before the first narrative token, so an
// early card never shows text that later disappears. When the run is done and
// narrate degraded (empty narrative), prose renders as the fallback.

function cardProse(overrides: Partial<CardProseSurface> = {}): CardProseSurface {
  return {
    status: "streaming",
    narrative: "",
    ...overrides,
  };
}

test("card prose hidden during early-card streaming gap", () => {
  assert.equal(showCardProse(cardProse()), false);
});

test("card prose hidden when the narrative bubble is present", () => {
  assert.equal(
    showCardProse(cardProse({ narrative: "On balance the read is constructive." })),
    false,
  );
});

test("card prose renders as fallback when narrate degraded (empty narrative)", () => {
  assert.equal(showCardProse(cardProse({ status: "done" })), true);
});

test("whitespace-only done narrative is treated as empty (prose renders)", () => {
  assert.equal(showCardProse(cardProse({ status: "done", narrative: "   \n  " })), true);
});

test("card prose stays hidden after done when narrative exists", () => {
  assert.equal(showCardProse(cardProse({ status: "done", narrative: "Narrated." })), false);
});

// ─── QNT-247: screen-reader announcement text ─────────────────────────────
//
// announceableAnswer feeds the debounced aria-live=polite region. It surfaces
// the analyst voice (narrative) when present, else the standalone streamed
// prose — the two token-streamed surfaces the a11y audit (#2) flagged as
// silent. The debounce that throttles announcements lives in chat-panel.tsx
// (timer-based, not unit-tested); this pure helper is the testable boundary.

function announce(overrides: Partial<AnnounceSurface> = {}): AnnounceSurface {
  return { narrative: "", proseChunks: [], quickFact: null, ...overrides };
}

test("announce: narrative is preferred over standalone prose", () => {
  assert.equal(
    announceableAnswer(announce({ narrative: "On balance, constructive.", proseChunks: ["raw"] })),
    "On balance, constructive.",
  );
});

test("announce: falls back to joined prose chunks when no narrative", () => {
  assert.equal(
    announceableAnswer(announce({ proseChunks: ["RSI ", "sits at ", "62."] })),
    "RSI sits at 62.",
  );
});

test("announce: whitespace-only narrative falls through to prose", () => {
  assert.equal(
    announceableAnswer(announce({ narrative: "   \n ", proseChunks: ["Prose answer."] })),
    "Prose answer.",
  );
});

test("announce: quick_fact answer is announced (narrate-skipped card)", () => {
  // quick_fact skips narrate and streams no prose, so its card answer is the
  // only prose surface — without this fallback a quick-fact run is silent to a
  // screen reader (verified live: QuickFactCard rendered, live region empty).
  assert.equal(
    announceableAnswer(announce({ quickFact: { answer: "AAPL's RSI-14 is 44.0, neutral." } })),
    "AAPL's RSI-14 is 44.0, neutral.",
  );
});

test("announce: narrative outranks a quick_fact answer", () => {
  assert.equal(
    announceableAnswer(
      announce({ narrative: "Voice answer.", quickFact: { answer: "card answer" } }),
    ),
    "Voice answer.",
  );
});

test("announce: empty run announces nothing", () => {
  assert.equal(announceableAnswer(announce()), "");
});

// ─── QNT-252: tool_result binds to its tool_call by started_at ─────────────
//
// Two concurrent calls to the SAME tool are the failure case for the old
// first-unmatched-by-name bind: if the SECOND call's result completes first,
// name-only binding would attach it to the first call's row. started_at is the
// exact correlation key, so the result lands on the call it belongs to
// regardless of completion order.

type Row = {
  name: string;
  started_at: number;
  result?: { name: string; started_at: number; summary: string };
};

test("tool_result binds to the call with the matching started_at", () => {
  const rows: Row[] = [
    { name: "technical", started_at: 100 },
    { name: "technical", started_at: 200 },
  ];
  // Second call (started_at 200) completes first.
  const afterSecond = bindToolResult(rows, {
    name: "technical",
    started_at: 200,
    summary: "second",
  });
  assert.equal(afterSecond[0].result, undefined); // first call still open
  assert.equal(afterSecond[1].result?.summary, "second"); // not bound to row[0]

  // First call (started_at 100) completes second.
  const afterBoth = bindToolResult(afterSecond, {
    name: "technical",
    started_at: 100,
    summary: "first",
  });
  assert.equal(afterBoth[0].result?.summary, "first");
  assert.equal(afterBoth[1].result?.summary, "second");
});

test("tool_result with no matching started_at binds nothing", () => {
  const rows: Row[] = [{ name: "technical", started_at: 100 }];
  const after = bindToolResult(rows, { name: "technical", started_at: 999, summary: "x" });
  assert.equal(after[0].result, undefined);
});

// ─── QNT-299: degradedToolsNote ─────────────────────────────────────────────

test("degradedToolsNote is null on a clean turn", () => {
  assert.equal(degradedToolsNote([]), null);
});

test("degradedToolsNote maps a known tool name to its friendly label", () => {
  assert.equal(degradedToolsNote(["news"]), "News unavailable this turn.");
});

test("degradedToolsNote joins multiple degraded tools in one line", () => {
  assert.equal(
    degradedToolsNote(["technical", "news"]),
    "Technicals, News unavailable this turn.",
  );
});

test("degradedToolsNote falls back to the raw name for an unmapped tool", () => {
  assert.equal(degradedToolsNote(["earnings_search"]), "earnings_search unavailable this turn.");
});
