import assert from "node:assert/strict";
import { test } from "node:test";

import { annotateUnsupportedNumbers } from "./annotate-unsupported.ts";

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
