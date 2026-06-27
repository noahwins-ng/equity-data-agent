// Run with: `npm test` (from `frontend/`) — Node's built-in TS loader + node:test.
//
// QNT-285: the narrate bubble renders a BLUF shape (bold call + synthesis prose
// + optional Watch line). These pin the pure parser that ProseBlock renders
// from: bold extraction, paragraph/line splitting, and that the existing
// `(source:)` → chip transform still fires.

import test from "node:test";
import assert from "node:assert/strict";

import { type DedupeState, parseInlineChips, parseProse } from "./prose-parse.ts";

const chipTexts = (segs: { type: string; text: string }[]) =>
  segs.filter((s) => s.type === "chip").map((s) => s.text);

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

// ─── QNT-287: consecutive same-source de-duplication ──────────────────────

test("consecutive same-source chips collapse to the first (parseInlineChips)", () => {
  const segs = parseInlineChips(
    "Earnings beat (source: news). Buyback announced (source: news). Guidance raised (source: news).",
  );
  assert.deepEqual(chipTexts(segs), ["news"], "only the first NEWS chip survives");
});

test("a source transition still shows every chip (no over-collapsing)", () => {
  const segs = parseInlineChips(
    "Cheap (source: fundamental), trending up (source: technical), and well covered (source: fundamental).",
  );
  assert.deepEqual(chipTexts(segs), ["fundamental", "technical", "fundamental"]);
});

test("de-dup is case/whitespace-insensitive on the source name", () => {
  const segs = parseInlineChips("a (source: News) b (source: news ) c (source:  NEWS )");
  assert.deepEqual(chipTexts(segs), ["News"]);
});

test("dropping a duplicate chip leaves no orphan space or double space", () => {
  // The 2nd `(source: news)` is dropped; its preceding text (". Again ") must
  // lose the trailing space so the tail doesn't read ". Again ." Rendering
  // chips as their text models the real inline adjacency.
  const segs = parseInlineChips("First (source: news). Again (source: news).");
  const rendered = segs.map((s) => s.text).join("");
  assert.equal(rendered, "First news. Again.");
});

test("back-to-back same-source chips collapse with no intervening text", () => {
  // The 2nd chip's preceding segment is the 1st chip (not text), so the trim is
  // correctly skipped and nothing crashes — the duplicate just drops.
  const segs = parseInlineChips("Cited twice (source: news)(source: news) here.");
  assert.deepEqual(chipTexts(segs), ["news"]);
  assert.equal(
    segs.map((s) => s.text).join(""),
    "Cited twice news here.",
  );
});

test("de-dup spans paragraphs in the synthesis bubble (parseProse)", () => {
  const text =
    "Valuation is rich (source: fundamental).\n\nMargins are contracting (source: fundamental).";
  const blocks = parseProse(text);
  const allChips = blocks.flatMap((b) => b.flatMap((line) => chipTexts(line)));
  assert.deepEqual(allChips, ["fundamental"], "the second-paragraph repeat collapses");
});

test("a shared carrier de-dups across calls (the AspectBlock summary+bullets case)", () => {
  // AspectBlock threads ONE carrier through its summary then each bullet, which
  // render in order — so a single-source aspect shows one chip total.
  const carrier: DedupeState = { last: null };
  const summary = parseInlineChips("Mixed sentiment in the headlines (source: news).", carrier);
  const bullet1 = parseInlineChips("Wedbush sees opportunity (source: news).", carrier);
  const bullet2 = parseInlineChips("Stock down 25% YTD (source: news).", carrier);
  assert.deepEqual(chipTexts(summary), ["news"], "summary keeps the first chip");
  assert.deepEqual(chipTexts(bullet1), [], "same-source bullet drops its chip");
  assert.deepEqual(chipTexts(bullet2), [], "and the next one too");
});
