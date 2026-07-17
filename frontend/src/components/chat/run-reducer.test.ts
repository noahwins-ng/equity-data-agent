// Run with: `npm test` (from `frontend/`) — Node's built-in TS loader + node:test.
//
// QNT-379: the startRun event-reducer + terminal settlement. The settle tests
// pin the two paths that used to leave a zombie "streaming" run (composer
// deadlock): abort mid-stream and a clean EOF without a `done` frame.

import test from "node:test";
import assert from "node:assert/strict";

import type { DoneEvent, ToolCallEvent, ToolResultEvent } from "@/lib/api";

import { initialRun, reduceRunEvent, settleRun } from "./run-reducer.ts";

function run() {
  return initialRun({
    id: "run-1",
    ticker: "AAPL",
    prompt: "Give me a balanced thesis on AAPL",
    startedAt: 1_752_000_000_000,
  });
}

const toolCall: ToolCallEvent = {
  name: "technical",
  label: "Technicals",
  args: { ticker: "AAPL" },
  started_at: 100,
};

const toolResult: ToolResultEvent = {
  name: "technical",
  label: "Technicals",
  latency_ms: 400,
  summary: "RSI 55, uptrend",
  ok: true,
  started_at: 100,
};

function doneEvent(overrides: Partial<DoneEvent> = {}): DoneEvent {
  return {
    tools_count: 1,
    citations_count: 2,
    confidence: 0.8,
    ...overrides,
  };
}

// ─── Event reduction ───────────────────────────────────────────────────────

test("initialRun starts streaming with an empty tape entry", () => {
  const r = run();
  assert.equal(r.status, "streaming");
  assert.equal(r.narrative, "");
  assert.deepEqual(r.toolRows, []);
  assert.deepEqual(r.errors, []);
});

test("tool_call appends a row; tool_result binds to it by started_at", () => {
  let r = reduceRunEvent(run(), "tool_call", toolCall);
  assert.equal(r.toolRows.length, 1);
  assert.equal(r.toolRows[0].result, undefined);
  r = reduceRunEvent(r, "tool_result", toolResult);
  assert.equal(r.toolRows[0].result?.summary, "RSI 55, uptrend");
});

test("narrative_chunk accumulates; prose_chunk appends", () => {
  let r = reduceRunEvent(run(), "narrative_chunk", { delta: "The trend " });
  r = reduceRunEvent(r, "narrative_chunk", { delta: "is up." });
  assert.equal(r.narrative, "The trend is up.");
  r = reduceRunEvent(r, "prose_chunk", { delta: "a" });
  r = reduceRunEvent(r, "prose_chunk", { delta: "b" });
  assert.deepEqual(r.proseChunks, ["a", "b"]);
});

test("intent and plan_rationale set their fields", () => {
  let r = reduceRunEvent(run(), "intent", { intent: "thesis" });
  assert.equal(r.intent, "thesis");
  r = reduceRunEvent(r, "plan_rationale", { text: "Reading the reports." });
  assert.equal(r.planRationale, "Reading the reports.");
});

test("unknown events are a no-op", () => {
  const r = run();
  assert.deepEqual(reduceRunEvent(r, "keepalive", {}), r);
});

test("done settles a clean run to done and stores stats", () => {
  let r = reduceRunEvent(run(), "narrative_chunk", { delta: "Up 45% this year." });
  r = reduceRunEvent(r, "done", doneEvent());
  assert.equal(r.status, "done");
  assert.equal(r.stats?.tools_count, 1);
});

test("done annotates unsupported numbers in narrative and joined prose", () => {
  let r = reduceRunEvent(run(), "narrative_chunk", { delta: "Up 45% this year." });
  r = reduceRunEvent(r, "prose_chunk", { delta: "Margin 4" });
  r = reduceRunEvent(r, "prose_chunk", { delta: "5% too." });
  r = reduceRunEvent(r, "done", doneEvent({ grounding_unsupported: ["45"] }));
  assert.equal(r.narrative, "Up 45%† this year.");
  // Chunks are joined before annotation so the number split across two
  // deltas ("4" + "5%") still gets exactly one dagger.
  assert.deepEqual(r.proseChunks, ["Margin 45%† too."]);
});

test("done on an errored surfaceless run settles to errored", () => {
  let r = reduceRunEvent(run(), "error", { detail: "boom", code: "synthesize" });
  r = reduceRunEvent(r, "done", doneEvent({ tools_count: 0, citations_count: 0 }));
  assert.equal(r.status, "errored");
});

test("done on an errored run WITH a surface still settles to done", () => {
  let r = reduceRunEvent(run(), "narrative_chunk", { delta: "Partial answer." });
  r = reduceRunEvent(r, "error", { detail: "boom", code: "grounding" });
  r = reduceRunEvent(r, "done", doneEvent());
  assert.equal(r.status, "done");
});

test("error events accumulate without ending the stream", () => {
  const r = reduceRunEvent(run(), "error", { detail: "boom", code: "x" });
  assert.equal(r.status, "streaming");
  assert.equal(r.errors.length, 1);
});

// ─── Terminal settlement (the QNT-379 deadlock paths) ─────────────────────

test("abort mid-stream with a partial surface settles to done", () => {
  const r = reduceRunEvent(run(), "narrative_chunk", { delta: "Partial thought…" });
  const settled = settleRun(r, "aborted");
  assert.equal(settled.status, "done");
  assert.equal(settled.narrative, "Partial thought…"); // partial content stands
});

test("abort before anything streamed settles to errored with an aborted rail", () => {
  const settled = settleRun(run(), "aborted");
  assert.equal(settled.status, "errored");
  assert.deepEqual(settled.errors, [
    { detail: "Interrupted by a new question.", code: "aborted" },
  ]);
});

test("EOF without a done frame settles a surfaced run to done", () => {
  const r = reduceRunEvent(run(), "narrative_chunk", { delta: "Truncated ans" });
  const settled = settleRun(r, "eof");
  assert.equal(settled.status, "done");
});

test("EOF without a done frame on a surfaceless run settles to errored", () => {
  const r = reduceRunEvent(run(), "tool_call", toolCall);
  const settled = settleRun(r, "eof");
  assert.equal(settled.status, "errored");
  assert.deepEqual(settled.errors, [
    { detail: "The stream ended before an answer arrived.", code: "stream-truncated" },
  ]);
});

test("settleRun is a no-op on already-terminal runs", () => {
  let r = reduceRunEvent(run(), "narrative_chunk", { delta: "Answer." });
  r = reduceRunEvent(r, "done", doneEvent());
  assert.deepEqual(settleRun(r, "eof"), r); // normal completion then finally
  const errored = { ...run(), status: "errored" as const };
  assert.deepEqual(settleRun(errored, "aborted"), errored); // transport catch then finally
});

test("no path leaves a run streaming: settleRun always returns terminal", () => {
  for (const reason of ["aborted", "eof"] as const) {
    for (const r of [run(), reduceRunEvent(run(), "narrative_chunk", { delta: "x" })]) {
      assert.notEqual(settleRun(r, reason).status, "streaming");
    }
  }
});
