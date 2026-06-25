// Run with: `npm test` (from `frontend/`) — Node's built-in TS loader + node:test.
//
// QNT-285: the narrate bubble renders a BLUF shape (bold call + synthesis prose
// + optional Watch line). These pin the pure parser that ProseBlock renders
// from: bold extraction, paragraph/line splitting, and that the existing
// `(source:)` → chip transform still fires.

import test from "node:test";
import assert from "node:assert/strict";

import { parseInlineChips, parseProse } from "./prose-parse.ts";

test("empty / whitespace text parses to no blocks", () => {
  assert.deepEqual(parseProse(""), []);
  assert.deepEqual(parseProse("   \n  "), []);
});

test("a bold lead call is a bold segment, not literal asterisks", () => {
  const blocks = parseProse("**Constructive, but priced for it.**");
  assert.equal(blocks.length, 1);
  const seg = blocks[0][0][0];
  assert.equal(seg.type, "bold");
  assert.equal(seg.text, "Constructive, but priced for it.");
});

test("blank line splits the call from the synthesis into two paragraphs", () => {
  const blocks = parseProse("**Constructive.**\n\nThe trend is intact.");
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0][0][0].type, "bold");
  assert.equal(blocks[1][0][0].type, "text");
  assert.equal(blocks[1][0][0].text, "The trend is intact.");
});

test("a single newline is a line break within one paragraph", () => {
  const blocks = parseProse("Driver one.\nWatch: the next print.");
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].length, 2);
  assert.equal(blocks[0][1][0].text, "Watch: the next print.");
});

test("the (source:) chip transform still fires inside synthesis prose", () => {
  const blocks = parseProse("Trend is intact (source: technical).");
  const segs = blocks[0][0];
  const chip = segs.find((s) => s.type === "chip");
  assert.ok(chip, "expected a chip segment");
  assert.equal(chip!.text, "technical");
});

test("a comma-joined source list chips as one token (not swallowed as text)", () => {
  const blocks = parseProse("Rich here (source: technical, fundamental).");
  const chip = blocks[0][0].find((s) => s.type === "chip");
  assert.ok(chip, "expected a chip segment for the comma-joined sources");
  assert.equal(chip!.text, "technical, fundamental");
});

test("an unterminated ** is left as literal text, not a runaway bold", () => {
  const blocks = parseProse("Constructive ** but not closed.");
  const segs = blocks[0][0];
  assert.ok(
    segs.every((s) => s.type !== "bold"),
    "an unbalanced ** must not produce a bold segment",
  );
  assert.ok(segs.some((s) => s.type === "text" && s.text.includes("**")));
});

test("parseInlineChips (legacy path) emits chips only, never bold", () => {
  const segs = parseInlineChips("**not bold here** but cited (source: news).");
  assert.ok(segs.every((s) => s.type !== "bold"), "legacy path must not parse bold");
  const chip = segs.find((s) => s.type === "chip");
  assert.ok(chip);
  assert.equal(chip!.text, "news");
});

test("bold and chip coexist across a realistic BLUF take", () => {
  const text =
    "**Constructive, but priced for it.**\n\n" +
    "The trend is intact (source: technical), but the multiple is at a premium (source: fundamental).\n" +
    "Watch: whether growth holds into the next print.";
  const blocks = parseProse(text);
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0][0][0].type, "bold");
  // second block: synthesis line + Watch line
  assert.equal(blocks[1].length, 2);
  const chipCount = blocks[1][0].filter((s) => s.type === "chip").length;
  assert.equal(chipCount, 2);
});
