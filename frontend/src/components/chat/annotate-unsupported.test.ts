import assert from "node:assert/strict";
import { test } from "node:test";

import {
  annotateUnsupportedDeep,
  annotateUnsupportedNumbers,
} from "./annotate-unsupported.ts";

test("annotates the token instead of redacting it (MU incident shape)", () => {
  // Real trace e5bd37e1 (2026-07-11): report "45.4% discount", narrator
  // spoke "45% discount"; the old redaction rendered
  // "a [unsupported number]% discount". The number must stay readable.
  const out = annotateUnsupportedNumbers("sitting at a 45% discount to the sector median", [
    "45",
  ]);
  assert.equal(out, "sitting at a 45%† discount to the sector median");
});

test("dagger lands after a glued magnitude unit", () => {
  const out = annotateUnsupportedNumbers("Free cash flow of $129.2B TTM", ["129.2"]);
  assert.equal(out, "Free cash flow of $129.2B† TTM");
});

test("does not split a longer number the token is a prefix of", () => {
  // "45" must not match inside "45.4%" — the decimal continues the token.
  const out = annotateUnsupportedNumbers("a 45.4% discount", ["45"]);
  assert.equal(out, "a 45.4% discount");
});

test("longest token wins when one is a suffix of another", () => {
  const out = annotateUnsupportedNumbers("values 5 and 45 differ", ["5", "45"]);
  assert.equal(out, "values 5† and 45† differ");
});

test("empty unsupported list leaves text untouched", () => {
  const text = "revenue grew +16.6% YoY";
  assert.equal(annotateUnsupportedNumbers(text, []), text);
  assert.equal(annotateUnsupportedNumbers(text), text);
});

test("annotates every occurrence of the token", () => {
  const out = annotateUnsupportedNumbers("99 here and 99 there", ["99"]);
  assert.equal(out, "99† here and 99† there");
});

test("sentence-final token annotates with the dagger before the period", () => {
  assert.equal(annotateUnsupportedNumbers("targets above $600.", ["600"]), "targets above $600†.");
  assert.equal(annotateUnsupportedNumbers("grew by 45%.", ["45"]), "grew by 45%†.");
});

test("split-chunk fragments must be joined before annotating", () => {
  // QNT-361 follow-up 5: SSE prose chunks split on token boundaries, so a
  // number can straddle two chunks. Annotating a fragment daggers the
  // trailing half mid-number; annotating the join is safe — chat-panel
  // joins proseChunks before calling this.
  const chunks = ["trading at a 45", ".4% discount"];
  assert.equal(
    annotateUnsupportedNumbers(chunks.join(""), ["45"]),
    "trading at a 45.4% discount", // 45 never matches inside 45.4
  );
  // The corruption the join prevents (fragment ends at a chunk boundary):
  assert.equal(annotateUnsupportedNumbers(chunks[0], ["45"]), "trading at a 45†");
});

test("deep-annotates nested card fields (AMD incident shape)", () => {
  // Real AMD turn (trace d59d146f): the "$600" miss lived in the news
  // card's summary — a structured field, not the narrative — and rendered
  // unmarked while the banner claimed "Numbers marked †".
  const card = {
    summary: "upgrades pushed price targets above $600.",
    key_points: ["The stock surged 5.8% after the report."],
    confidence: 0.78,
    label: null,
  };
  const out = annotateUnsupportedDeep(card, ["600"]);
  assert.equal(out.summary, "upgrades pushed price targets above $600†.");
  assert.equal(out.key_points[0], "The stock surged 5.8% after the report.");
  assert.equal(out.confidence, 0.78); // numbers are values, not prose — untouched
  assert.equal(out.label, null);
});

test("deep-annotate with empty list returns the value unchanged", () => {
  const card = { summary: "targets above $600" };
  assert.equal(annotateUnsupportedDeep(card, []), card);
  assert.equal(annotateUnsupportedDeep(null, ["600"]), null);
});
